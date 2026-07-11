# ============================================================
# notify_ip.tf - サーバー起動時の IP アドレス通知
# ============================================================
# 通知フロー:
#   [起動時 IP 通知] ECS Task RUNNING → EventBridge → Lambda(notify_ip) → Discord
#
# コスト通知（Budgets → SNS → Lambda）は cost_alerts.tf を参照。
# 元は notifications.tf に同居していたが、無関係な2機能のため分離した。

module "notify_ip_package" {
  source = "../modules/lambda_package"

  source_dir   = "${path.module}/functions/notify_ip"
  shared_dir   = "${path.module}/functions/_shared"
  shared_files = ["notifier.py", "aws_clients.py", "ssm_params.py", "ecs_net.py"]
  output_path  = "${path.module}/functions/notify_ip/notify_ip.zip"
}

module "notify_ip_lambda" {
  source = "../modules/lambda_function"

  function_name    = "${local.name_prefix}-notify-ip"
  filename         = module.notify_ip_package.output_path
  source_code_hash = module.notify_ip_package.output_base64sha256
  handler          = "notify_ip.lambda_handler"
  timeout          = 30

  environment_variables = merge(local.messaging_env, {
    # ゲームサーバー情報
    GAME_NAME      = var.game_name
    CLUSTER_ARN    = aws_ecs_cluster.game.arn
    READY_PARAM    = "${local.ssm_prefix}/ready"
    NOTIFIED_PARAM = "${local.ssm_prefix}/notified_task"
  })

  extra_iam_statements = [
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
      Resource = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_prefix}/*"
    }
  ]
}

moved {
  from = aws_iam_role.notify_ip
  to   = module.notify_ip_lambda.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.notify_ip
  to   = module.notify_ip_lambda.aws_iam_role_policy.this
}

moved {
  from = aws_cloudwatch_log_group.notify_ip
  to   = module.notify_ip_lambda.aws_cloudwatch_log_group.this
}

moved {
  from = aws_lambda_function.notify_ip
  to   = module.notify_ip_lambda.aws_lambda_function.this
}

# EventBridge ルール（monitor サイドカーが SSM の /ready パラメータを "1" にした時に発火）
# ECS RUNNING（コンテナ起動）ではなく、ゲームが実際に接続受付を開始したタイミングで通知する
module "server_ready_trigger" {
  source = "../modules/eventbridge_lambda_trigger"

  rule_name        = "${local.name_prefix}-server-ready"
  rule_description = "${var.game_name} server ready - trigger IP notification when game accepts connections"
  event_pattern = {
    source        = ["aws.ssm"]
    "detail-type" = ["Parameter Store Change"]
    detail = {
      name      = ["${local.ssm_prefix}/ready"]
      operation = ["Create", "Update"]
    }
  }

  function_name = module.notify_ip_lambda.function_name
  function_arn  = module.notify_ip_lambda.function_arn
  target_id     = "NotifyIpLambda"
  statement_id  = "AllowExecutionFromEventBridgeServerReady"
}

moved {
  from = aws_cloudwatch_event_rule.server_ready
  to   = module.server_ready_trigger.aws_cloudwatch_event_rule.this
}

moved {
  from = aws_cloudwatch_event_target.server_ready
  to   = module.server_ready_trigger.aws_cloudwatch_event_target.this
}

moved {
  from = aws_lambda_permission.allow_eventbridge_server_ready
  to   = module.server_ready_trigger.aws_lambda_permission.this
}

# EventBridge ルール（ECS タスクが STOPPED になった時に発火）
# desiredStatus=STOPPED でフィルタすることで、タスク置換/クラッシュ再起動時の
# 誤「停止」通知を防ぐ。/stop コマンドとアイドル自動停止の両方を捕捉する。
module "ecs_stopped_trigger" {
  source = "../modules/eventbridge_lambda_trigger"

  rule_name        = "${local.name_prefix}-ecs-stopped"
  rule_description = "${var.game_name} ECS task STOPPED state change - trigger stop notification"
  event_pattern = {
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      lastStatus    = ["STOPPED"]
      desiredStatus = ["STOPPED"]
      clusterArn    = [aws_ecs_cluster.game.arn]
    }
  }

  function_name = module.notify_ip_lambda.function_name
  function_arn  = module.notify_ip_lambda.function_arn
  target_id     = "NotifyStoppedLambda"
  statement_id  = "AllowExecutionFromEventBridgeStopped"
}

moved {
  from = aws_cloudwatch_event_rule.ecs_stopped
  to   = module.ecs_stopped_trigger.aws_cloudwatch_event_rule.this
}

moved {
  from = aws_cloudwatch_event_target.notify_stopped
  to   = module.ecs_stopped_trigger.aws_cloudwatch_event_target.this
}

moved {
  from = aws_lambda_permission.allow_eventbridge_stopped
  to   = module.ecs_stopped_trigger.aws_lambda_permission.this
}
