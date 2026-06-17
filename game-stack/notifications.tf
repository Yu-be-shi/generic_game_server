# ============================================================
# notifications.tf - Lambda 通知・SNS・予算アラートの定義
# ============================================================
# 通知フロー:
#   [起動時 IP 通知] ECS Task RUNNING → EventBridge → Lambda(notify_ip) → Discord
#   [コスト超過通知] AWS Budgets → SNS → Lambda(notify_cost) → Discord

# ============================================================
# ① サーバー起動時の IP アドレス通知
# ============================================================

data "archive_file" "notify_ip" {
  type        = "zip"
  output_path = "${path.module}/functions/notify_ip/notify_ip.zip"

  # ハンドラ本体 + 共有 notifier モジュールを同梱
  source {
    content  = file("${path.module}/functions/notify_ip/notify_ip.py")
    filename = "notify_ip.py"
  }
  source {
    content  = file("${path.module}/functions/_shared/notifier.py")
    filename = "notifier.py"
  }
}

resource "aws_iam_role" "notify_ip" {
  name = "${local.name_prefix}-notify-ip"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-notify-ip"
  }
}

resource "aws_iam_role_policy" "notify_ip" {
  name = "${local.name_prefix}-notify-ip"
  role = aws_iam_role.notify_ip.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # ENI からパブリック IP を取得するために必要
        Sid      = "DescribeNetworkInterfaces"
        Effect   = "Allow"
        Action   = ["ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      },
      {
        # 実行中タスクのパブリック IP 取得（SSM イベント受信時）
        Sid    = "EcsDescribeTasks"
        Effect = "Allow"
        Action = [
          "ecs:ListTasks",
          "ecs:DescribeTasks"
        ]
        Resource = "*"
      },
      {
        # SSM ready の現在値確認 + notified_task への通知済みタスク ARN 記録（重複排除）
        Sid      = "SsmStatus"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/ggs/${local.name_prefix}/*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "notify_ip" {
  name              = "/aws/lambda/${local.name_prefix}-notify-ip"
  retention_in_days = 7

  tags = {
    Name = "${local.name_prefix}-notify-ip-logs"
  }
}

resource "aws_lambda_function" "notify_ip" {
  function_name    = "${local.name_prefix}-notify-ip"
  role             = aws_iam_role.notify_ip.arn
  runtime          = "python3.12"
  handler          = "notify_ip.lambda_handler"
  filename         = data.archive_file.notify_ip.output_path
  source_code_hash = data.archive_file.notify_ip.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      # メッセージング設定（ツール非依存）
      MESSAGING_PROVIDER    = var.messaging_provider
      MESSAGING_WEBHOOK_URL = var.discord_webhook_url # 変数名は互換維持（実 URL は sensitive）
      # ゲームサーバー情報
      GAME_NAME   = var.game_name
      CLUSTER_ARN = aws_ecs_cluster.game.arn
      READY_PARAM = "/ggs/${local.name_prefix}/ready"
    }
  }

  depends_on = [aws_cloudwatch_log_group.notify_ip]

  tags = {
    Name = "${local.name_prefix}-notify-ip"
  }
}

# EventBridge ルール（monitor サイドカーが SSM の /ready パラメータを "1" にした時に発火）
# ECS RUNNING（コンテナ起動）ではなく、ゲームが実際に接続受付を開始したタイミングで通知する
resource "aws_cloudwatch_event_rule" "server_ready" {
  name        = "${local.name_prefix}-server-ready"
  description = "${var.game_name} server ready - trigger IP notification when game accepts connections"

  event_pattern = jsonencode({
    source        = ["aws.ssm"]
    "detail-type" = ["Parameter Store Change"]
    detail = {
      name      = ["/ggs/${local.name_prefix}/ready"]
      operation = ["Create", "Update"]
    }
  })

  tags = {
    Name = "${local.name_prefix}-server-ready"
  }
}

resource "aws_cloudwatch_event_target" "server_ready" {
  rule      = aws_cloudwatch_event_rule.server_ready.name
  target_id = "NotifyIpLambda"
  arn       = aws_lambda_function.notify_ip.arn
}

resource "aws_lambda_permission" "allow_eventbridge_server_ready" {
  statement_id  = "AllowExecutionFromEventBridgeServerReady"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify_ip.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.server_ready.arn
}

# EventBridge ルール（ECS タスクが STOPPED になった時に発火）
# desiredStatus=STOPPED でフィルタすることで、タスク置換/クラッシュ再起動時の
# 誤「停止」通知を防ぐ。/stop コマンドとアイドル自動停止の両方を捕捉する。
resource "aws_cloudwatch_event_rule" "ecs_stopped" {
  name        = "${local.name_prefix}-ecs-stopped"
  description = "${var.game_name} ECS task STOPPED state change - trigger stop notification"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      lastStatus    = ["STOPPED"]
      desiredStatus = ["STOPPED"]
      clusterArn    = [aws_ecs_cluster.game.arn]
    }
  })

  tags = {
    Name = "${local.name_prefix}-ecs-stopped"
  }
}

resource "aws_cloudwatch_event_target" "notify_stopped" {
  rule      = aws_cloudwatch_event_rule.ecs_stopped.name
  target_id = "NotifyStoppedLambda"
  arn       = aws_lambda_function.notify_ip.arn
}

resource "aws_lambda_permission" "allow_eventbridge_stopped" {
  statement_id  = "AllowExecutionFromEventBridgeStopped"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify_ip.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ecs_stopped.arn
}

# ============================================================
# ② 月間コスト超過段階通知（Budgets → SNS → Lambda → Discord）
# ============================================================

resource "aws_sns_topic" "cost_alert" {
  name = "${local.name_prefix}-cost-alert"

  tags = {
    Name = "${local.name_prefix}-cost-alert"
  }
}

# AWS Budgets がこのトピックにパブリッシュできるよう許可
resource "aws_sns_topic_policy" "cost_alert" {
  arn = aws_sns_topic.cost_alert.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "AllowBudgetsPublish"
      Effect = "Allow"
      Principal = {
        Service = "budgets.amazonaws.com"
      }
      Action   = "SNS:Publish"
      Resource = aws_sns_topic.cost_alert.arn
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })
}

data "archive_file" "notify_cost" {
  type        = "zip"
  output_path = "${path.module}/functions/notify_cost/notify_cost.zip"

  # ハンドラ本体 + 共有 notifier モジュールを同梱
  source {
    content  = file("${path.module}/functions/notify_cost/notify_cost.py")
    filename = "notify_cost.py"
  }
  source {
    content  = file("${path.module}/functions/_shared/notifier.py")
    filename = "notifier.py"
  }
}

resource "aws_iam_role" "notify_cost" {
  name = "${local.name_prefix}-notify-cost"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-notify-cost"
  }
}

resource "aws_iam_role_policy" "notify_cost" {
  name = "${local.name_prefix}-notify-cost"
  role = aws_iam_role.notify_cost.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # DLQ（配信失敗キュー）への書き込み
        Sid      = "SqsDlqSend"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.notify_cost_dlq.arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "notify_cost" {
  name              = "/aws/lambda/${local.name_prefix}-notify-cost"
  retention_in_days = 7

  tags = {
    Name = "${local.name_prefix}-notify-cost-logs"
  }
}

resource "aws_lambda_function" "notify_cost" {
  function_name    = "${local.name_prefix}-notify-cost"
  role             = aws_iam_role.notify_cost.arn
  runtime          = "python3.12"
  handler          = "notify_cost.lambda_handler"
  filename         = data.archive_file.notify_cost.output_path
  source_code_hash = data.archive_file.notify_cost.output_base64sha256
  timeout          = 10

  # SNS 非同期 invoke がリトライ後も失敗した場合（webhook 403 等）のメッセージを保存
  dead_letter_config {
    target_arn = aws_sqs_queue.notify_cost_dlq.arn
  }

  environment {
    variables = {
      # メッセージング設定（ツール非依存）
      MESSAGING_PROVIDER    = var.messaging_provider
      MESSAGING_WEBHOOK_URL = var.discord_webhook_url # 変数名は互換維持（実 URL は sensitive）
    }
  }

  depends_on = [aws_cloudwatch_log_group.notify_cost]

  tags = {
    Name = "${local.name_prefix}-notify-cost"
  }
}

resource "aws_sns_topic_subscription" "cost_alert_to_lambda" {
  topic_arn = aws_sns_topic.cost_alert.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.notify_cost.arn
}

resource "aws_lambda_permission" "allow_sns" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify_cost.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_alert.arn
}

# ============================================================
# コスト通知の冗長化・可視化
# ============================================================

# DLQ: notify_cost Lambda が webhook 配信に失敗した場合のメッセージ保存
# SNS 非同期 invoke はリトライ（最大 3 回）後も失敗した invocation をここへ送る
resource "aws_sqs_queue" "notify_cost_dlq" {
  name                       = "${local.name_prefix}-notify-cost-dlq"
  message_retention_seconds  = 1209600 # 14 日間保持（デフォルト 4 日より長く確認余裕を確保）
  visibility_timeout_seconds = 30

  tags = {
    Name = "${local.name_prefix}-notify-cost-dlq"
  }
}

# CloudWatch アラーム: DLQ にメッセージが届いたら通知（webhook 障害の検知）
# alarm_actions に cost_alert SNS トピックを指定: email 購読があればメールでも届く
resource "aws_cloudwatch_metric_alarm" "notify_cost_dlq" {
  alarm_name          = "${local.name_prefix}-notify-cost-dlq"
  alarm_description   = "notify_cost Lambda の配信失敗（DLQ にメッセージ到着）"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.notify_cost_dlq.name }
  statistic           = "Sum"
  period              = 300 # 5 分
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alert.arn]
  ok_actions          = [aws_sns_topic.cost_alert.arn]

  tags = {
    Name = "${local.name_prefix}-notify-cost-dlq-alarm"
  }
}

# CloudWatch アラーム: SNS → Lambda 配信失敗数の監視
# NumberOfNotificationsFailed は SNS が Lambda へのデリバリーに失敗した回数
resource "aws_cloudwatch_metric_alarm" "cost_alert_delivery_failed" {
  alarm_name          = "${local.name_prefix}-cost-alert-delivery-failed"
  alarm_description   = "cost_alert SNS トピックの Lambda 配信失敗"
  namespace           = "AWS/SNS"
  metric_name         = "NumberOfNotificationsFailed"
  dimensions          = { TopicName = aws_sns_topic.cost_alert.name }
  statistic           = "Sum"
  period              = 300 # 5 分
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alert.arn]

  tags = {
    Name = "${local.name_prefix}-cost-alert-delivery-failed-alarm"
  }
}

# メール冗長チャネル: Discord/Lambda が壊れても通知を受け取るための独立経路
# alert_email が空の場合は作成しない（count による条件付き）
resource "aws_sns_topic_subscription" "cost_alert_to_email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.cost_alert.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ============================================================
# AWS Budgets（月間コスト予算・4段階アラート）
# ============================================================

resource "aws_budgets_budget" "monthly" {
  account_id        = data.aws_caller_identity.current.account_id
  name              = "${local.name_prefix}-monthly-budget"
  budget_type       = "COST"
  limit_amount      = tostring(var.budget_limit_usd)
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2024-01-01_00:00"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 20
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alert.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 50
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alert.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alert.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alert.arn]
  }
}
