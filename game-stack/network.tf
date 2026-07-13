# ============================================================
# network.tf - データソース、ローカル変数、セキュリティグループ
# ============================================================
# 設計方針（固定費ゼロ）:
#   NAT Gateway / ALB / NLB / Route53 は使用しない。
#   ECS タスクに直接パブリック IP を付与し、インターネット通信を実現する。
#
# VPC・サブネット・IGW・ルートテーブル・S3 VPC Endpoint は
# control-plane/network.tf で一元管理する共有リソース。
# ゲームごとのセキュリティグループのみここで定義し、分離を維持する。
# control-plane を先に apply してから game-stack を apply すること。

# -----------------------------------------------------------
# データソース
# -----------------------------------------------------------

# IAM ポリシーの Resource ARN 構築に使用（iam.tf / cost_alerts.tf / notify_ip.tf から参照）
data "aws_caller_identity" "current" {}

# control-plane が作成した共有 VPC を "ggs-shared-vpc" タグで検索して参照する。
# remote_state ではなくタグルックアップを使うことで、game-stack は
# control-plane の backend 設定に依存せず疎結合を保つ。
data "aws_vpc" "shared" {
  tags = {
    Name = "ggs-shared-vpc"
  }
}

# 共有 VPC 内のパブリックサブネット（"ggs-shared=true" タグで識別）
data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.shared.id]
  }
  tags = {
    ggs-shared = "true"
  }
}

# One Zone EFS 用: パブリックサブネットのうち最初の 1 つ（sort で決定論的）の AZ を取得。
# efs_storage_class="regional" のときも data source は評価されるが、追加 API コールは軽微。
data "aws_subnet" "efs_primary" {
  id = sort(data.aws_subnets.public.ids)[0]
}

# -----------------------------------------------------------
# ローカル変数
# -----------------------------------------------------------

locals {
  # terraform workspace でゲームごとに state を分離（A方式の核心）
  # game_name と workspace が同名の場合は重複を避けて縮約する
  # 例: game_name=palworld, workspace=palworld  → prefix=palworld
  #     game_name=palworld, workspace=palworld2 → prefix=palworld-palworld2
  name_prefix = var.game_name == terraform.workspace ? var.game_name : "${var.game_name}-${terraform.workspace}"
  cluster_name = "${local.name_prefix}-cluster"
  service_name = "${local.name_prefix}-service"

  # SSM パラメータ名前空間の共通 prefix（末尾スラッシュなし）。
  # auto_update.tf / backup.tf / notify_ip.tf / iam.tf / ecs.tf から参照する。
  ssm_prefix = "/ggs/${local.name_prefix}"

  # IAM ポリシー・SSM/ECS ARN 構築で使う AWS アカウント ID
  account_id = data.aws_caller_identity.current.account_id

  # save_slot によるセーブデータ切り替え（B方式）:
  # save_slot が空なら従来どおり game_name 直下、指定時のみサブディレクトリを切る（既存デプロイ非破壊）
  save_dir      = var.save_slot == "" ? "/${var.game_name}" : "/${var.game_name}/${var.save_slot}"
  backup_prefix = var.save_slot == "" ? local.name_prefix : "${local.name_prefix}/${var.save_slot}"

  # EFS・ECS・バックアップ Lambda の配置先サブネット
  # regional（既定）: 全パブリックサブネット（複数 AZ）
  # one_zone: sort した先頭サブネット 1 つに固定（EFS マウントターゲットと同一 AZ が必須）
  efs_subnets = var.efs_storage_class == "one_zone" ? [sort(data.aws_subnets.public.ids)[0]] : data.aws_subnets.public.ids

  # 一般向け通知 Lambda（notify_ip, notify_backup, auto_update）共通のメッセージング設定
  messaging_env = {
    MESSAGING_PROVIDER    = var.messaging_provider
    MESSAGING_WEBHOOK_URL = var.discord_webhook_url
  }

  # 運用者向け通知 Lambda（notify_cost, cost_guard）用のメッセージング設定。
  # コスト通知には AWS アカウント ID 等が含まれるため管理者専用チャンネルへ分離できる。
  # admin_webhook_url 未設定時は一般チャンネルへフォールバック（従来どおり）
  admin_messaging_env = {
    MESSAGING_PROVIDER    = var.messaging_provider
    MESSAGING_WEBHOOK_URL = var.admin_webhook_url != "" ? var.admin_webhook_url : var.discord_webhook_url
  }
}

# -----------------------------------------------------------
# セキュリティグループ（ゲームごとに個別作成・共有 VPC 内に配置）
# -----------------------------------------------------------

# ゲームサーバー用 SG（game_ports に基づいてポートを動的に開放）
resource "aws_security_group" "game" {
  name        = "${local.name_prefix}-game-sg"
  description = "${var.game_name} game server security group"
  vpc_id      = data.aws_vpc.shared.id

  dynamic "ingress" {
    for_each = var.game_ports
    content {
      description = ingress.value.description != "" ? ingress.value.description : "${ingress.value.protocol}/${ingress.value.port}"
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = lower(ingress.value.protocol)
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # アウトバウンド全許可（dnf install・Discord 通知・ECS API 呼び出しに必要）
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-game-sg"
  }

  lifecycle {
    # ECS タスクの長寿命 ENI に紐づくため、無いと置き換え時に旧ENI解放待ちでデッドロックする
    # （backup_lambda SG で実際に45分タイムアウトが発生したのと同じ危険な形）
    create_before_destroy = true
  }
}

# EFS 用 SG（ゲームコンテナおよびバックアップ Lambda からの NFS:2049 を許可）
resource "aws_security_group" "efs" {
  name        = "${local.name_prefix}-efs-sg"
  description = "EFS security group - allow NFS from game containers and backup Lambda"
  vpc_id      = data.aws_vpc.shared.id

  ingress {
    description = "NFS from game containers and backup Lambda"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    security_groups = [
      aws_security_group.game.id,
      aws_security_group.backup_lambda.id, # バックアップ Lambda からの EFS マウントに必要
    ]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-efs-sg"
  }

  lifecycle {
    # EFS マウントターゲットの長寿命 ENI に紐づくため、無いと置き換え時に旧ENI解放待ちでデッドロックする
    # （backup_lambda SG で実際に45分タイムアウトが発生したのと同じ危険な形）
    create_before_destroy = true
  }
}
