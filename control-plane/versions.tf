# ============================================================
# versions.tf - control-plane プロバイダー定義
# ============================================================

terraform {
  required_version = ">= 1.5.0"

  # backend の詳細設定は backend.hcl で渡す（アカウント固有情報をコードから分離）
  # 初回セットアップ: backend.hcl.example をコピーして bucket 名を記入し
  # terraform init -migrate-state -backend-config=backend.hcl を実行する
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}
