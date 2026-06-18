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

data "archive_file" "auto_update" {
  type        = "zip"
  output_path = "${path.module}/functions/auto_update/auto_update.zip"

  # ハンドラ本体 + 共有 notifier モジュールを同梱（notify_ip と同じパターン）
  source {
    content  = file("${path.module}/functions/auto_update/auto_update.py")
    filename = "auto_update.py"
  }
  source {
    content  = file("${path.module}/functions/_shared/notifier.py")
    filename = "notifier.py"
  }
}

# ---------------------------------------------------------------
# IAM ロール
# ---------------------------------------------------------------

resource "aws_iam_role" "auto_update" {
  name = "${local.name_prefix}-auto-update"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-auto-update"
  }
}

resource "aws_iam_role_policy" "auto_update" {
  name = "${local.name_prefix}-auto-update"
  role = aws_iam_role.auto_update.id

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
        # ワンオフ更新タスクの起動
        # タスク定義ファミリーのリビジョン全体に限定し、クラスター条件でさらに絞る
        Sid      = "EcsRunTask"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name_prefix}:*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.game.arn }
        }
      },
      {
        # アップデート完了後の強制停止（ハング時の唯一の停止経路）
        Sid      = "EcsStopTask"
        Effect   = "Allow"
        Action   = ["ecs:StopTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.cluster_name}/*"
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
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.cluster_name}/${local.service_name}"
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
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/ggs/${local.name_prefix}/*"
      }
    ]
  })
}

# ---------------------------------------------------------------
# CloudWatch Logs グループ
# ---------------------------------------------------------------

resource "aws_cloudwatch_log_group" "auto_update" {
  name              = "/aws/lambda/${local.name_prefix}-auto-update"
  retention_in_days = 7

  tags = {
    Name = "${local.name_prefix}-auto-update-logs"
  }
}

# ---------------------------------------------------------------
# Lambda 関数
# ---------------------------------------------------------------

resource "aws_lambda_function" "auto_update" {
  function_name    = "${local.name_prefix}-auto-update"
  role             = aws_iam_role.auto_update.arn
  runtime          = "python3.12"
  handler          = "auto_update.lambda_handler"
  filename         = data.archive_file.auto_update.output_path
  source_code_hash = data.archive_file.auto_update.output_base64sha256

  # SteamCMD アップデート + ポーリング（最大12分）+ stop_task の余裕を見て 15 分
  timeout     = 900
  memory_size = 256
  # VPC 不要: ECS/SSM 制御 API のみ使用（EFS をマウントしない）

  # Graviton (arm64) で実行（純 Python のため無改修で約 20% コスト削減）
  architectures = ["arm64"]

  environment {
    variables = {
      CLUSTER_ARN       = aws_ecs_cluster.game.arn
      SERVICE_NAME      = local.service_name
      TASK_DEF_FAMILY   = local.name_prefix
      SUBNET_IDS        = join(",", data.aws_subnets.public.ids)
      SECURITY_GROUP_ID = aws_security_group.game.id
      NAME_PREFIX       = local.name_prefix
      GAME_NAME         = var.game_name
      # Steam バージョン事前チェック設定（非 Steam 系ゲームは空文字列のまま）
      STEAM_APP_ID = var.steam_app_id
      STEAM_BRANCH = var.steam_branch
      # メッセージング設定（notifier.py 共有モジュールが参照）
      MESSAGING_PROVIDER    = var.messaging_provider
      MESSAGING_WEBHOOK_URL = var.discord_webhook_url
    }
  }

  depends_on = [aws_cloudwatch_log_group.auto_update]

  tags = {
    Name = "${local.name_prefix}-auto-update"
    Game = var.game_name
  }
}
