# ============================================================
# modules/lambda_function - Lambda 関数の共通定義
# ============================================================
# archive_file → IAM ロール → CloudWatch ロググループ → lambda_function という
# 定型パターンを共通化する。control-plane・game-stack 双方から呼び出される。
# archive_file はデータソース（state 追跡なし）のため呼び出し元に残し、
# filename/source_code_hash のみをこのモジュールに渡す。

resource "aws_iam_role" "this" {
  name = coalesce(var.role_name, var.function_name)

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = coalesce(var.role_name, var.function_name) }
}

resource "aws_iam_role_policy" "this" {
  name = coalesce(var.role_name, var.function_name)
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ], var.extra_iam_statements)
  })
}

# VPC 内 Lambda（EFS マウント等）に必要な ENI 作成権限
resource "aws_iam_role_policy_attachment" "vpc" {
  count = var.vpc_config != null ? 1 : 0

  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.function_name}-logs" }
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = aws_iam_role.this.arn
  runtime          = var.runtime
  handler          = var.handler
  filename         = var.filename
  source_code_hash = var.source_code_hash
  timeout          = var.timeout
  memory_size      = var.memory_size
  architectures    = var.architectures

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  dynamic "file_system_config" {
    for_each = var.file_system_config != null ? [var.file_system_config] : []
    content {
      arn              = file_system_config.value.arn
      local_mount_path = file_system_config.value.local_mount_path
    }
  }

  dynamic "dead_letter_config" {
    for_each = var.dead_letter_target_arn != null ? [var.dead_letter_target_arn] : []
    content {
      target_arn = dead_letter_config.value
    }
  }

  environment {
    variables = var.environment_variables
  }

  # depends_on は静的なリストのみ許容されるため concat() 等は使えない。
  # aws_iam_role_policy_attachment.vpc は count=0/1 のためインデックスなしで参照し、
  # インスタンスが無い（vpc_config 未指定）場合は依存なしとして扱われる。
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
  ]

  tags = { Name = var.function_name }
}
