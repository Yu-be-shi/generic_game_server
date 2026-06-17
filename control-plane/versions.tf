# ============================================================
# versions.tf - control-plane プロバイダー定義
# ============================================================

terraform {
  required_version = ">= 1.5.0"

  # backend の詳細設定は backend.hcl で渡す（アカウント固有情報をコードから分離）
  # 初回セットアップ: backend.hcl.example をコピーして bucket 名を記入し
  # terraform init -migrate-state -backend-config=backend.hcl を実行する
  backend "s3" {
    bucket = "yubeshi-game-server-terraform-state"
    key    = "control-plane/terraform.tfstate"
    region = "ap-northeast-1"
  }

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

  # 全リソースに GameOps 識別タグを自動付与（個人アカウントでの分離・コスト追跡用）
  # Cost Explorer で Project=GameOps フィルタを使うには AWS Billing コンソールで
  # "Project" をコスト配分タグとして有効化すること。
  default_tags {
    tags = {
      Project   = "GameOps"
      ManagedBy = "Terraform"
    }
  }
}
