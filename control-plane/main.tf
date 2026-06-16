# ============================================================
# main.tf - Discord コントロールプレーン
# ============================================================
# 構成（固定費ゼロ）:
#   Lambda Function URL ← Discord がスラッシュコマンドを POST
#   Lambda → ECS API でゲームサーバーを制御（/start /stop /status /games）
#
# Function URL は API Gateway 不要で追加コストゼロ。
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
# IAM ロール
# -----------------------------------------------------------

resource "aws_iam_role" "discord_control" {
  name = local.function_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = local.function_name
  }
}

resource "aws_iam_role_policy" "discord_control" {
  name = local.function_name
  role = aws_iam_role.discord_control.id

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
        # ECS クラスター/サービス/タスクの情報取得（/games, /status）
        Sid    = "EcsRead"
        Effect = "Allow"
        Action = [
          "ecs:ListClusters",
          "ecs:DescribeClusters",
          "ecs:ListServices",
          "ecs:DescribeServices",
          "ecs:ListTasks",
          "ecs:DescribeTasks"
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
      }
    ]
  })
}

# -----------------------------------------------------------
# CloudWatch Logs グループ
# -----------------------------------------------------------

resource "aws_cloudwatch_log_group" "discord_control" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 7

  tags = {
    Name = "${local.function_name}-logs"
  }
}

# -----------------------------------------------------------
# Lambda 関数
# -----------------------------------------------------------

resource "aws_lambda_function" "discord_control" {
  function_name    = local.function_name
  role             = aws_iam_role.discord_control.arn
  runtime          = "python3.12"
  handler          = "index.lambda_handler"
  filename         = data.archive_file.discord_control.output_path
  source_code_hash = data.archive_file.discord_control.output_base64sha256

  # Discord の3秒制限に十分な余裕を持たせる
  timeout = 10

  environment {
    variables = {
      DISCORD_PUBLIC_KEY       = var.discord_public_key
      GAME_AWS_REGION          = var.aws_region
      DISCORD_ALLOWED_USER_IDS = join(",", var.discord_allowed_user_ids)
    }
  }

  depends_on = [aws_cloudwatch_log_group.discord_control]

  tags = {
    Name = local.function_name
  }
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
  integration_uri        = aws_lambda_function.discord_control.invoke_arn
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
  function_name = aws_lambda_function.discord_control.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.discord_control.execution_arn}/*/*"
}
