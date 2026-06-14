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
  source_file = "${path.module}/functions/notify_ip/notify_ip.py"
  output_path = "${path.module}/functions/notify_ip/notify_ip.zip"
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
      DISCORD_WEBHOOK_URL = var.discord_webhook_url
      GAME_NAME           = var.game_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.notify_ip]

  tags = {
    Name = "${local.name_prefix}-notify-ip"
  }
}

# EventBridge ルール（ECS タスクが RUNNING になった時に発火）
resource "aws_cloudwatch_event_rule" "ecs_running" {
  name        = "${local.name_prefix}-ecs-running"
  description = "${var.game_name} ECS task RUNNING state change - trigger IP notification"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      lastStatus = ["RUNNING"]
      clusterArn = [aws_ecs_cluster.game.arn]
    }
  })

  tags = {
    Name = "${local.name_prefix}-ecs-running"
  }
}

resource "aws_cloudwatch_event_target" "notify_ip" {
  rule      = aws_cloudwatch_event_rule.ecs_running.name
  target_id = "NotifyIpLambda"
  arn       = aws_lambda_function.notify_ip.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify_ip.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ecs_running.arn
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
  source_file = "${path.module}/functions/notify_cost/notify_cost.py"
  output_path = "${path.module}/functions/notify_cost/notify_cost.zip"
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
    Statement = [{
      Sid      = "CloudWatchLogs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:*:*:*"
    }]
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

  environment {
    variables = {
      DISCORD_WEBHOOK_URL = var.discord_webhook_url
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
