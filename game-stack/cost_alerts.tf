# ============================================================
# cost_alerts.tf - 月間コスト超過段階通知（Budgets → SNS → Lambda → Discord）
# ============================================================
# 通知フロー:
#   [コスト超過通知] AWS Budgets → SNS → Lambda(notify_cost) → Discord
#
# IP起動通知（EventBridge → Lambda(notify_ip)）は notify_ip.tf を参照。
# 元は notifications.tf に同居していたが、無関係な2機能のため分離した。

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

module "notify_cost_lambda" {
  source = "../modules/lambda_function"

  function_name    = "${local.name_prefix}-notify-cost"
  filename         = data.archive_file.notify_cost.output_path
  source_code_hash = data.archive_file.notify_cost.output_base64sha256
  handler          = "notify_cost.lambda_handler"
  timeout          = 10

  # SNS 非同期 invoke がリトライ後も失敗した場合（webhook 403 等）のメッセージを保存
  dead_letter_target_arn = aws_sqs_queue.notify_cost_dlq.arn

  environment_variables = local.messaging_env

  extra_iam_statements = [
    {
      # DLQ（配信失敗キュー）への書き込み
      Sid      = "SqsDlqSend"
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.notify_cost_dlq.arn
    }
  ]
}

moved {
  from = aws_iam_role.notify_cost
  to   = module.notify_cost_lambda.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.notify_cost
  to   = module.notify_cost_lambda.aws_iam_role_policy.this
}

moved {
  from = aws_cloudwatch_log_group.notify_cost
  to   = module.notify_cost_lambda.aws_cloudwatch_log_group.this
}

moved {
  from = aws_lambda_function.notify_cost
  to   = module.notify_cost_lambda.aws_lambda_function.this
}

resource "aws_sns_topic_subscription" "cost_alert_to_lambda" {
  topic_arn = aws_sns_topic.cost_alert.arn
  protocol  = "lambda"
  endpoint  = module.notify_cost_lambda.function_arn
}

resource "aws_lambda_permission" "allow_sns" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = module.notify_cost_lambda.function_name
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

# ============================================================
# 注: コスト通知失敗の監視アラームは削除済み
# ============================================================
# かつて以下の 2 アラームをゲームごとに作成していたが、ゲーム数に比例して
# CloudWatch アラーム課金（$0.10/個/月、アカウントで 10 個超から）が積み上がるため削除:
#   - notify-cost-dlq            (DLQ へのメッセージ到着を検知)
#   - cost-alert-delivery-failed (SNS→Lambda 配信失敗を検知)
#
# これらは「通知パイプラインが壊れたことの見張り役」であり、通知本体ではない。
# 削除後も Discord/メール通知および暴走コスト防止は以下が担保する:
#   - AWS Budgets → SNS → notify_cost Lambda → Discord (コスト通知本体)
#   - cost_guard Lambda のハード停止 (暴走コスト防止)
#   - alert_email 購読 (メール独立経路・設定時のみ)
#   - aws_sqs_queue.notify_cost_dlq は残存（手動での配信失敗確認が可能）
# ============================================================

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
