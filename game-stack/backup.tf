# ============================================================
# backup.tf - EFS セーブデータの S3 バックアップ
# ============================================================
# バックアップ戦略（二段構え）:
#   [1] 停止前同期: 監視サイドカー（auto_shutdown.sh）がサーバー停止直前に
#       EFS → S3 へ同期し、セッションごとの最新状態を保存する。
#   [2] 定期バックアップ: EventBridge cron（24時間ごと）が Lambda を起動し、
#       EFS → S3 へ差分同期する（長時間稼働・クラッシュへの保険）。
#
# コスト:
#   Lambda・EventBridge は無料枠内。S3 Gateway VPC Endpoint は無料。
#   実質コストは S3 ストレージ代のみ（セーブ数 MB 〜数 GB で数円/月）。

# ============================================================
# S3 バックアップバケット
# ============================================================

resource "aws_s3_bucket" "backup" {
  # S3 バケット名はグローバル一意のため account_id を末尾に付与する
  bucket = "${local.name_prefix}-backup-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    # terraform destroy / apply による誤削除を防止する
    prevent_destroy = true
  }

  tags = {
    Name = "${local.name_prefix}-backup"
    Game = var.game_name
  }
}

# 公開アクセスを完全にブロック
resource "aws_s3_bucket_public_access_block" "backup" {
  bucket = aws_s3_bucket.backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# サーバーサイド暗号化（AES256）
resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# バージョニング有効化（破損データで上書きされても過去版に戻せる）
resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id

  versioning_configuration {
    status = "Enabled"
  }
}

# ライフサイクル設定（古い版を自動削除してコストを抑制）
resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  # バージョニングが有効になってから適用するため、depends_on で順序を保証する
  depends_on = [aws_s3_bucket_versioning.backup]

  rule {
    id     = "cleanup-old-versions"
    status = "Enabled"

    filter {} # バケット全体に適用

    # 30 日前の非カレントバージョンを削除（コスト抑制）
    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    # マルチパートアップロードの未完了データを自動削除
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "tiering"
    status = "Enabled"

    filter {} # バケット全体に適用

    # カレントバージョン: 30 日後に STANDARD_IA へ移行（標準の約 56% 安い）
    # ※ S3 の最小課金オブジェクトサイズは 128KB、最小保存期間は 30 日。
    #   セーブデータが非常に小さい（< 128KB）場合は効果がないことがある。
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    # 非カレントバージョン: 7 日後に GLACIER_IR へ移行（IA よりさらに約 68% 安い）
    # ロールバック用の旧バージョンは高頻度アクセス不要のため Glacier に適している
    noncurrent_version_transition {
      noncurrent_days = 7
      storage_class   = "GLACIER_IR"
    }
  }
}

# ============================================================
# S3 Gateway VPC Endpoint は control-plane/network.tf に移設済み
# ============================================================
# VPC が共有化されたため、S3 VPC エンドポイントは control-plane で一元管理する。
# game-stack での作成は不要（共有ルートテーブルにすでにエントリが存在する）。

# ============================================================
# バックアップ Lambda 用セキュリティグループ
# （network.tf の EFS SG が NFS:2049 をこの SG から許可する）
# ============================================================

resource "aws_security_group" "backup_lambda" {
  name        = "${local.name_prefix}-backup-lambda-sg"
  description = "Backup Lambda - outbound to EFS NFS and S3"
  vpc_id      = data.aws_vpc.shared.id

  # アウトバウンド全許可（EFS:2049 + S3 Gateway Endpoint 経由 HTTPS）
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-backup-lambda-sg"
  }
}

# ============================================================
# バックアップ Lambda IAM ロール
# ============================================================

resource "aws_iam_role" "backup_lambda" {
  name = "${local.name_prefix}-backup-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-backup-lambda"
  }
}

# VPC 内 Lambda に必要な ENI 作成権限（AWSLambdaVPCAccessExecutionRole）
resource "aws_iam_role_policy_attachment" "backup_lambda_vpc" {
  role       = aws_iam_role.backup_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "backup_lambda" {
  name = "${local.name_prefix}-backup-lambda"
  role = aws_iam_role.backup_lambda.id

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
        # EFS アクセスポイント経由でのマウントに必要
        Sid    = "EfsMount"
        Effect = "Allow"
        Action = [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess"
        ]
        Resource = aws_efs_file_system.main.arn
      },
      {
        # バックアップバケットへの読み書きに必要
        Sid    = "S3Backup"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.backup.arn,
          "${aws_s3_bucket.backup.arn}/*"
        ]
      }
    ]
  })
}

# ============================================================
# バックアップ Lambda 関数
# ============================================================

resource "aws_cloudwatch_log_group" "backup_lambda" {
  name              = "/aws/lambda/${local.name_prefix}-backup-efs"
  retention_in_days = 7

  tags = {
    Name = "${local.name_prefix}-backup-efs-logs"
  }
}

data "archive_file" "backup_efs" {
  type        = "zip"
  source_file = "${path.module}/functions/backup_efs/backup_efs.py"
  output_path = "${path.module}/functions/backup_efs/backup_efs.zip"
}

resource "aws_lambda_function" "backup_efs" {
  function_name    = "${local.name_prefix}-backup-efs"
  role             = aws_iam_role.backup_lambda.arn
  runtime          = "python3.12"
  handler          = "backup_efs.lambda_handler"
  filename         = data.archive_file.backup_efs.output_path
  source_code_hash = data.archive_file.backup_efs.output_base64sha256

  # セーブデータが大きい場合も完走できるよう最大タイムアウト（15分）に設定
  timeout     = 900
  memory_size = 512

  # Graviton (arm64) で実行（純 Python のため無改修で約 20% コスト削減）
  architectures = ["arm64"]

  # EFS をマウントする（既存のアクセスポイントを流用）
  file_system_config {
    arn              = aws_efs_access_point.main.arn
    local_mount_path = "/mnt/efs"
  }

  # VPC 内に配置（EFS と S3 Gateway Endpoint に到達するため）
  vpc_config {
    subnet_ids         = data.aws_subnets.public.ids
    security_group_ids = [aws_security_group.backup_lambda.id]
  }

  environment {
    variables = {
      BACKUP_BUCKET = aws_s3_bucket.backup.id
      BACKUP_PREFIX = local.name_prefix
    }
  }

  # EFS マウントターゲットと VPC Endpoint が準備できてから作成する
  depends_on = [
    aws_efs_mount_target.main,
    aws_vpc_endpoint.s3,
    aws_cloudwatch_log_group.backup_lambda,
    aws_iam_role_policy_attachment.backup_lambda_vpc,
  ]

  tags = {
    Name = "${local.name_prefix}-backup-efs"
    Game = var.game_name
  }
}

# ============================================================
# EventBridge スケジュール（24時間ごとにバックアップ Lambda を起動）
# ============================================================

resource "aws_cloudwatch_event_rule" "backup_schedule" {
  name                = "${local.name_prefix}-backup-schedule"
  description         = "${var.game_name} daily EFS backup to S3"
  schedule_expression = "rate(24 hours)"

  tags = {
    Name = "${local.name_prefix}-backup-schedule"
  }
}

resource "aws_cloudwatch_event_target" "backup_lambda" {
  rule      = aws_cloudwatch_event_rule.backup_schedule.name
  target_id = "BackupEfsLambda"
  arn       = aws_lambda_function.backup_efs.arn
}

resource "aws_lambda_permission" "backup_from_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup_efs.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.backup_schedule.arn
}
