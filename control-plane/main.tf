# ============================================================
# main.tf - Discord コントロールプレーン
# ============================================================
# 構成（固定費ゼロ）:
#   API Gateway v2 HTTP API ← Discord がスラッシュコマンドを POST
#   API Gateway → Lambda → ECS API でゲームサーバーを制御
#   （/games /start /stop /status /cost /update /backup /restore /switch-slot の 9 コマンド）
#
# Lambda Function URL はアカウントレベルのパブリックアクセスブロックの無効化が
# 必要で S3 に影響するため不使用。API Gateway v2 HTTP API は同じコストプロファイル
#（固定費なし・リクエスト従量課金）で Lambda Function URL と実質同等。
# Ed25519 署名検証（index.py + ed25519.py）で Discord 以外の呼び出しを弾く。

data "aws_caller_identity" "current" {}

locals {
  function_name = "game-server-discord-control"
}

# -----------------------------------------------------------
# Lambda ソースコード ZIP（index.py + ed25519.py をまとめて zip 化）
# -----------------------------------------------------------

data "archive_file" "discord_control" {
  type        = "zip"
  source_dir  = "${path.module}/functions/discord_control"
  output_path = "${path.module}/functions/discord_control.zip"
}

# -----------------------------------------------------------
# Lambda 関数（IAM ロール込み、modules/lambda_function で作成）
# -----------------------------------------------------------

module "discord_control_lambda" {
  source = "../modules/lambda_function"

  function_name    = local.function_name
  filename         = data.archive_file.discord_control.output_path
  source_code_hash = data.archive_file.discord_control.output_base64sha256
  handler          = "index.lambda_handler"

  # Discord の3秒制限に十分な余裕を持たせる
  # deferred ワーカー（自己 invoke）は重い AWS API 呼び出しを担うため余裕を持たせる
  timeout = 10

  # 実測 Max Memory Used: 110 MB / 128 MB（残り 18 MB）。OOM 余裕確保と
  # コールドスタート短縮（CPU 割当はメモリに比例）のため 256 MB に引き上げる。
  memory_size = 256

  environment_variables = {
    DISCORD_PUBLIC_KEY       = var.discord_public_key
    GAME_AWS_REGION          = var.aws_region
    DISCORD_ALLOWED_USER_IDS = join(",", var.discord_allowed_user_ids)
    # ツール非依存化: MESSAGING_PROVIDER で provider.py の実装を選択する
    # "discord"（既定）の場合は DISCORD_PUBLIC_KEY を使用し挙動変化なし
    MESSAGING_PROVIDER = "discord"
  }

  extra_iam_statements = [
    {
      # ECS クラスター/サービス/タスクの情報取得（/games, /status）
      Sid    = "EcsRead"
      Effect = "Allow"
      Action = [
        "ecs:ListClusters",
        "ecs:DescribeClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecs:DescribeTaskDefinition"
      ]
      Resource = "*"
    },
    {
      # ECS サービスの desired_count 変更（/start, /stop）
      # ワイルドカードで全サービスを対象にすることでゲーム追加時の再デプロイが不要
      Sid      = "EcsControl"
      Effect   = "Allow"
      Action   = ["ecs:UpdateService"]
      Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/*/*"
    },
    {
      # タスクのパブリック IP 取得（/status）
      Sid      = "DescribeENI"
      Effect   = "Allow"
      Action   = ["ec2:DescribeNetworkInterfaces"]
      Resource = "*"
    },
    {
      # SSM からゲームサーバーの受付状態・プレイヤー数を読み取る（/status）
      # monitor サイドカーが /ggs/<prefix>/ready と /ggs/<prefix>/players に書き込む
      Sid      = "SsmStatusRead"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/ggs/*"
    },
    {
      # /start で update_service に taskDefinition を指定する際、ECS が
      # タスクの execution/task ロールを service に渡すために必要。
      # ecs-tasks.amazonaws.com へのパスのみに限定することでゲーム追加時の再デプロイも不要。
      Sid      = "PassEcsTaskRoles"
      Effect   = "Allow"
      Action   = ["iam:PassRole"]
      Resource = "*"
      Condition = {
        StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
      }
    },
    {
      # /start 時に SSM ready を 0 にリセットする（古い ready=1 残留による誤「稼働中」防止）
      # monitor サイドカーが起動直後に 0 を書くまでの空白を埋め、
      # monitor 起動失敗時も /status が「起動処理中」を返すようにする。
      Sid      = "SsmStatusReset"
      Effect   = "Allow"
      Action   = ["ssm:PutParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/ggs/*"
    },
    {
      # /cost コマンド: 今月合計・月末予測・予算残を表示
      # ce / budgets はグローバルサービスのため Resource は * 必須
      # sts:GetCallerIdentity は IAM ポリシー不要（常に許可）
      Sid    = "CostRead"
      Effect = "Allow"
      Action = [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "budgets:ViewBudget",
        "budgets:DescribeBudgets"
      ]
      Resource = "*"
    },
    {
      # /update コマンド: game-stack の auto_update Worker Lambda を非同期 invoke する
      # ゲーム追加時の再デプロイを避けるためワイルドカード（*-auto-update）で全ゲームを対象にする
      Sid      = "InvokeAutoUpdate"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:*-auto-update"
    },
    {
      # /backup, /restore コマンド: game-stack の backup_efs Worker Lambda を非同期 invoke する
      # ゲーム追加時の再デプロイを避けるためワイルドカード（*-backup-efs）で全ゲームを対象にする
      Sid      = "InvokeBackupEfs"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:*-backup-efs"
    },
    {
      # deferred response: 自分自身を非同期 invoke してコマンドをワーカー実行する。
      # 関数名は local.function_name で固定し、ワイルドカードを使わない。
      # 循環依存を避けるため aws_lambda_function リソース参照ではなく ARN を直接構成する。
      Sid      = "InvokeSelf"
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.function_name}"
    }
  ]
}

moved {
  from = aws_iam_role.discord_control
  to   = module.discord_control_lambda.aws_iam_role.this
}

moved {
  from = aws_iam_role_policy.discord_control
  to   = module.discord_control_lambda.aws_iam_role_policy.this
}

moved {
  from = aws_cloudwatch_log_group.discord_control
  to   = module.discord_control_lambda.aws_cloudwatch_log_group.this
}

moved {
  from = aws_lambda_function.discord_control
  to   = module.discord_control_lambda.aws_lambda_function.this
}

# -----------------------------------------------------------
# API Gateway HTTP API（固定費ゼロ・リクエスト従量課金）
# -----------------------------------------------------------
# Lambda Function URL はアカウントレベルでパブリックアクセスがブロックされるため
# API Gateway 経由で公開する。
# index.py で Ed25519 署名を検証するため Discord 以外の呼び出しは 401 で弾かれる。

resource "aws_apigatewayv2_api" "discord_control" {
  name          = local.function_name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "discord_control" {
  api_id                 = aws_apigatewayv2_api.discord_control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.discord_control_lambda.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "discord_control" {
  api_id    = aws_apigatewayv2_api.discord_control.id
  route_key = "POST /"
  target    = "integrations/${aws_apigatewayv2_integration.discord_control.id}"
}

resource "aws_apigatewayv2_stage" "discord_control" {
  api_id      = aws_apigatewayv2_api.discord_control.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "discord_control_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.discord_control_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.discord_control.execution_arn}/*/*"
}
