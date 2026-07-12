# ============================================================
# notify_backup.tf - backup_efs 実行結果の Discord 通知
# ============================================================
# 通知フロー:
#   backup_efs（VPC 内・webhook 到達不可）
#     → S3 <backup_prefix>/_events/*.json 書き込み
#     → S3 イベント通知 → Lambda(notify_backup・VPC 外) → Discord
#
# backup_efs Lambda が VPC 内（NAT なし・S3 Gateway エンドポイントのみ）で
# 動作するため webhook へ直接 POST できず、S3 経由で VPC 外へ委譲する。
# 詳細: docs/troubleshooting/vpc-lambda-cannot-reach-ssm.md / Issue #3

module "notify_backup_package" {
  source = "../modules/lambda_package"

  source_dir   = "${path.module}/functions/notify_backup"
  shared_dir   = "${path.module}/functions/_shared"
  shared_files = ["notifier.py"]
  output_path  = "${path.module}/functions/notify_backup/notify_backup.zip"
}

module "notify_backup_lambda" {
  source = "../modules/lambda_function"

  function_name    = "${local.name_prefix}-notify-backup"
  filename         = module.notify_backup_package.output_path
  source_code_hash = module.notify_backup_package.output_base64sha256
  handler          = "notify_backup.lambda_handler"
  timeout          = 30

  environment_variables = local.messaging_env

  extra_iam_statements = [
    {
      # 結果イベント JSON の読み取りのみ（_events/ 配下に限定）
      Sid      = "ReadBackupEvents"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.backup.arn}/${local.backup_prefix}/_events/*"
    }
  ]
}

resource "aws_lambda_permission" "notify_backup_from_s3" {
  statement_id  = "AllowExecutionFromS3BackupEvents"
  action        = "lambda:InvokeFunction"
  function_name = module.notify_backup_lambda.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.backup.arn
}

# S3 バケット通知はバケットにつき 1 リソースのみ定義可能（上書き注意）
resource "aws_s3_bucket_notification" "backup_events" {
  bucket = aws_s3_bucket.backup.id

  lambda_function {
    lambda_function_arn = module.notify_backup_lambda.function_arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "${local.backup_prefix}/_events/"
    filter_suffix       = ".json"
  }

  depends_on = [aws_lambda_permission.notify_backup_from_s3]
}
