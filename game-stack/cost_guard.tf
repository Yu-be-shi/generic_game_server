# ============================================================
# cost_guard.tf - 長時間稼働タスクの強制停止バックストップ
# ============================================================
#
# 目的:
#   監視サイドカー（auto_shutdown.sh, essential=false）が落ちると
#   自動停止が静かに止まり、Fargate タスクが 24 時間動き続けるリスクがある。
#   このバックストップは監視サイドカーとは完全に独立した別系統で、
#   MAX_RUNTIME_HOURS を超えて RUNNING なタスクを強制停止する。
#
# フロー:
#   EventBridge (rate(1 hour)) → Lambda(cost_guard)
#     → RUNNING タスクの経過時間を確認
#     → 閾値超過タスクを stop_task + update-service desiredCount=0
#     → Discord/Slack に通知
#
# 注意:
#   通常のアイドル自動停止（10分無人→サイドカーが停止）の代替ではなく最終安全網。
#   MAX_RUNTIME_HOURS のデフォルト 12 時間は通常プレイ中に誤発動しない余裕を持った値。

# ---------------------------------------------------------------
# Lambda ソースコード ZIP
# ---------------------------------------------------------------

data "archive_file" "cost_guard" {
  type        = "zip"
  output_path = "${path.module}/functions/cost_guard/cost_guard.zip"

  source {
    content  = file("${path.module}/functions/cost_guard/cost_guard.py")
    filename = "cost_guard.py"
  }
  source {
    content  = file("${path.module}/functions/_shared/notifier.py")
    filename = "notifier.py"
  }
}

# ---------------------------------------------------------------
# IAM ロール
# ---------------------------------------------------------------

resource "aws_iam_role" "cost_guard" {
  name = "${local.name_prefix}-cost-guard"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-cost-guard"
  }
}

resource "aws_iam_role_policy" "cost_guard" {
  name = "${local.name_prefix}-cost-guard"
  role = aws_iam_role.cost_guard.id

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
        # 実行中タスクの一覧取得（リソースレベル制限が効かないため * を使用）
        Sid      = "EcsListDescribeTasks"
        Effect   = "Allow"
        Action   = ["ecs:ListTasks", "ecs:DescribeTasks"]
        Resource = "*"
      },
      {
        # 長時間タスクの強制停止（クラスター内のタスクに限定）
        Sid      = "EcsStopTask"
        Effect   = "Allow"
        Action   = ["ecs:StopTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.cluster_name}/*"
      },
      {
        # サービスタスク停止後の desiredCount=0（再起動防止）
        Sid      = "EcsUpdateService"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.cluster_name}/${local.service_name}"
      }
    ]
  })
}

# ---------------------------------------------------------------
# CloudWatch Logs グループ
# ---------------------------------------------------------------

resource "aws_cloudwatch_log_group" "cost_guard" {
  name              = "/aws/lambda/${local.name_prefix}-cost-guard"
  retention_in_days = 7

  tags = {
    Name = "${local.name_prefix}-cost-guard-logs"
  }
}

# ---------------------------------------------------------------
# Lambda 関数
# ---------------------------------------------------------------

resource "aws_lambda_function" "cost_guard" {
  function_name    = "${local.name_prefix}-cost-guard"
  role             = aws_iam_role.cost_guard.arn
  runtime          = "python3.12"
  handler          = "cost_guard.lambda_handler"
  filename         = data.archive_file.cost_guard.output_path
  source_code_hash = data.archive_file.cost_guard.output_base64sha256
  timeout          = 60

  environment {
    variables = {
      CLUSTER_ARN       = aws_ecs_cluster.game.arn
      SERVICE_NAME      = local.service_name
      MAX_RUNTIME_HOURS = tostring(var.max_task_runtime_hours)
      GAME_NAME         = var.game_name
      # メッセージング設定
      MESSAGING_PROVIDER    = var.messaging_provider
      MESSAGING_WEBHOOK_URL = var.discord_webhook_url
    }
  }

  depends_on = [aws_cloudwatch_log_group.cost_guard]

  tags = {
    Name = "${local.name_prefix}-cost-guard"
  }
}

# ---------------------------------------------------------------
# EventBridge スケジュール（1時間ごとに発火）
# ---------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "cost_guard" {
  name                = "${local.name_prefix}-cost-guard"
  description         = "${var.game_name} cost guard - force-stop tasks running > ${var.max_task_runtime_hours}h (sidecar failure backstop)"
  schedule_expression = "rate(1 hour)"

  tags = {
    Name = "${local.name_prefix}-cost-guard"
  }
}

resource "aws_cloudwatch_event_target" "cost_guard" {
  rule      = aws_cloudwatch_event_rule.cost_guard.name
  target_id = "CostGuardLambda"
  arn       = aws_lambda_function.cost_guard.arn
}

resource "aws_lambda_permission" "cost_guard" {
  statement_id  = "AllowExecutionFromEventBridgeCostGuard"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_guard.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cost_guard.arn
}
