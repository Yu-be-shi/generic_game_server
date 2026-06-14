# ============================================================
# state.tf - Terraform state 管理用 S3 バケット
# ============================================================
# このバケットが control-plane / game-stack 両モジュールの
# tfstate の保存先になる。
#
# bootstrap の順序:
#   1. このファイルを追加して terraform apply（state はまだローカル）
#   2. output の tf_state_bucket_name をコピーして backend.hcl を作成
#   3. terraform init -migrate-state でローカル state を S3 へ移行
#
# 以降は S3 が state の正とな り、ローカルファイルは不要になる。

resource "aws_s3_bucket" "tf_state" {
  bucket = "tf-state-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    # state バケットを誤削除すると全リソースの管理が失われる
    prevent_destroy = true
  }

  tags = {
    Name    = "tf-state"
    Purpose = "Terraform state backend"
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# state の過去バージョンを保持することでファイル破損時に復元可能にする
resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  versioning_configuration {
    status = "Enabled"
  }
}
