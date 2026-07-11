# ============================================================
# auto_update.tf - サーバー手動アップデート Worker Lambda
# ============================================================
#
# Discord /update <game> → discord_control Lambda → この Lambda（非同期 invoke）
#
# 通常の /start が使う ECS Service（UPDATE_ON_BOOT=false）には触れず、
# ecs.run_task でワンオフタスクを起動し UPDATE_ON_BOOT=true を上書きして
# SteamCMD アップデートを実行する。完了後に stop_task で停止。
#
# スケジュール実行はしない（手動コマンドのみ）。

# ---------------------------------------------------------------
# Lambda ソースコード ZIP（handler + 共有 notifier モジュール）
# ---------------------------------------------------------------

module "auto_update_package" {
  source = "../modules/lambda_package"

  source_dir   = "${path.module}/functions/auto_update"
  shared_dir   = "${path.module}/functions/_shared"
  shared_files = ["notifier.py", "aws_clients.py", "ssm_params.py"]
  output_path  = "${path.module}/functions/auto_update/auto_update.zip"
}

# ---------------------------------------------------------------
# Lambda 関数（IAM ロール込み、modules/lambda_function で作成）
# ---------------------------------------------------------------

module "auto_update_lambda" {
  source = "../modules/lambda_function"

  function_name    = "${local.name_prefix}-auto-update"
  filename         = module.auto_update_package.output_path
  source_code_hash = module.auto_update_package.output_base64sha256
  handler          = "auto_update.lambda_handler"

  # SteamCMD アップデート + ポーリング（最大12分）+ stop_task の余裕を見て 15 分
  timeout     = 900
  memory_size = 256
  # VPC 不要: ECS/SSM 制御 API のみ使用（EFS をマウントしない）

  environment_variables = merge(local.messaging_env, {
    CLUSTER_ARN       = aws_ecs_cluster.game.arn
    SERVICE_NAME      = local.service_name
    TASK_DEF_FAMILY   = local.name_prefix
    SUBNET_IDS        = join(",", local.efs_subnets)
    SECURITY_GROUP_ID = aws_security_group.game.id
    NAME_PREFIX       = local.name_prefix
    GAME_NAME         = var.game_name
    # Steam バージョン事前チェック設定（非 Steam 系ゲームは空文字列のまま）
    STEAM_APP_ID = var.steam_app_id
    STEAM_BRANCH = var.steam_branch
  })

  extra_iam_statements = [
    {
      # ワンオフ更新タスクの起動
      # タスク定義ファミリーのリビジョン全体に限定し、クラスター条件でさらに絞る
      Sid      = "EcsRunTask"
      Effect   = "Allow"
      Action   = ["ecs:RunTask"]
      Resource = "arn:aws:ecs:${var.aws_region}:${local.account_id}:task-definition/${local.name_prefix}:*"
      Condition = {
        ArnEquals = { "ecs:cluster" = aws_ecs_cluster.game.arn }
      }
    },
    {
      # アップデート完了後の強制停止（ハング時の唯一の停止経路）
      Sid      = "EcsStopTask"
      Effect   = "Allow"
      Action   = ["ecs:StopTask"]
      Resource = "arn:aws:ecs:${var.aws_region}:${local.account_id}:task/${local.cluster_name}/*"
    },
    {
      # 起動中タスクの確認（二重起動ガード）
      # ecs:ListTasks / ecs:DescribeTasks はリソースレベル制限が効かないため * を使用
      Sid      = "EcsDescribeTasks"
      Effect   = "Allow"
      Action   = ["ecs:ListTasks", "ecs:DescribeTasks"]
      Resource = "*"
    },
    {
      # サービスの desiredCount 確認（二重起動ガード）
      Sid      = "EcsDescribeService"
      Effect   = "Allow"
      Action   = ["ecs:DescribeServices"]
      Resource = "arn:aws:ecs:${var.aws_region}:${local.account_id}:service/${local.cluster_name}/${local.service_name}"
    },
    {
      # run_task はタスクロール（task）と実行ロール（task_execution）両方の PassRole が必要
      # 片方だけでは AccessDenied になるため必ず両方指定する
      Sid    = "PassEcsRoles"
      Effect = "Allow"
      Action = ["iam:PassRole"]
      Resource = [
        aws_iam_role.task.arn,
        aws_iam_role.task_execution.arn,
      ]
      Condition = {
        StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
      }
    },
    {
      # SSM: maintenance フラグ / update_ready / update_players の読み書き
      Sid      = "SsmStatus"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:PutParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_prefix}/*"
    }
  ]
}

moved {
  from = aws_iam_role.auto_update
  to   = module.auto_update_lambda.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.auto_update
  to   = module.auto_update_lambda.aws_iam_role_policy.this
}

moved {
  from = aws_cloudwatch_log_group.auto_update
  to   = module.auto_update_lambda.aws_cloudwatch_log_group.this
}

moved {
  from = aws_lambda_function.auto_update
  to   = module.auto_update_lambda.aws_lambda_function.this
}
