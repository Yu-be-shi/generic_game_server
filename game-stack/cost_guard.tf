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
#   MAX_RUNTIME_HOURS のデフォルト 24 時間は通常プレイ中に誤発動しない余裕を持った値。

# ---------------------------------------------------------------
# Lambda ソースコード ZIP
# ---------------------------------------------------------------

data "archive_file" "cost_guard" {
  type        = "zip"
  output_path = "${path.module}/functions/cost_guard/cost_guard.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/functions/cost_guard", "*.py")
    content {
      content  = file("${path.module}/functions/cost_guard/${source.value}")
      filename = source.value
    }
  }
  dynamic "source" {
    for_each = toset(["notifier.py", "aws_clients.py"])
    content {
      content  = file("${path.module}/functions/_shared/${source.value}")
      filename = source.value
    }
  }
}

# ---------------------------------------------------------------
# Lambda 関数（IAM ロール込み、modules/lambda_function で作成）
# ---------------------------------------------------------------

module "cost_guard_lambda" {
  source = "../modules/lambda_function"

  function_name    = "${local.name_prefix}-cost-guard"
  filename         = data.archive_file.cost_guard.output_path
  source_code_hash = data.archive_file.cost_guard.output_base64sha256
  handler          = "cost_guard.lambda_handler"
  timeout          = 60

  environment_variables = merge(local.messaging_env, {
    CLUSTER_ARN       = aws_ecs_cluster.game.arn
    SERVICE_NAME      = local.service_name
    MAX_RUNTIME_HOURS = tostring(var.max_task_runtime_hours)
    GAME_NAME         = var.game_name
  })

  extra_iam_statements = [
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
      Resource = "arn:aws:ecs:${var.aws_region}:${local.account_id}:task/${local.cluster_name}/*"
    },
    {
      # サービスタスク停止後の desiredCount=0（再起動防止）
      Sid      = "EcsUpdateService"
      Effect   = "Allow"
      Action   = ["ecs:UpdateService"]
      Resource = "arn:aws:ecs:${var.aws_region}:${local.account_id}:service/${local.cluster_name}/${local.service_name}"
    }
  ]
}

moved {
  from = aws_iam_role.cost_guard
  to   = module.cost_guard_lambda.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.cost_guard
  to   = module.cost_guard_lambda.aws_iam_role_policy.this
}

moved {
  from = aws_cloudwatch_log_group.cost_guard
  to   = module.cost_guard_lambda.aws_cloudwatch_log_group.this
}

moved {
  from = aws_lambda_function.cost_guard
  to   = module.cost_guard_lambda.aws_lambda_function.this
}

# ---------------------------------------------------------------
# EventBridge スケジュール（1時間ごとに発火）
# ---------------------------------------------------------------

module "cost_guard_trigger" {
  source = "../modules/eventbridge_lambda_trigger"

  rule_name           = "${local.name_prefix}-cost-guard"
  rule_description    = "${var.game_name} cost guard - force-stop tasks running > ${var.max_task_runtime_hours}h (sidecar failure backstop)"
  schedule_expression = "rate(1 hour)"

  function_name = module.cost_guard_lambda.function_name
  function_arn  = module.cost_guard_lambda.function_arn
  target_id     = "CostGuardLambda"
  statement_id  = "AllowExecutionFromEventBridgeCostGuard"
}

moved {
  from = aws_cloudwatch_event_rule.cost_guard
  to   = module.cost_guard_trigger.aws_cloudwatch_event_rule.this
}

moved {
  from = aws_cloudwatch_event_target.cost_guard
  to   = module.cost_guard_trigger.aws_cloudwatch_event_target.this
}

moved {
  from = aws_lambda_permission.cost_guard
  to   = module.cost_guard_trigger.aws_lambda_permission.this
}
