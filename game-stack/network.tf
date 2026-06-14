# ============================================================
# network.tf - データソース、ローカル変数、VPC/サブネット/SG
# ============================================================
# 設計方針（固定費ゼロ）:
#   NAT Gateway / ALB / NLB / Route53 は使用しない。
#   ECS タスクに直接パブリック IP を付与し、インターネット通信を実現する。

# -----------------------------------------------------------
# データソース
# -----------------------------------------------------------

data "aws_availability_zones" "available" {
  state = "available"
}

# IAM ポリシーの Resource ARN 構築に使用（iam.tf / notifications.tf から参照）
data "aws_caller_identity" "current" {}

# -----------------------------------------------------------
# ローカル変数
# -----------------------------------------------------------

locals {
  # terraform workspace でゲームごとに state を分離（A方式の核心）
  # 例: game_name=palworld, workspace=palworld → prefix=palworld-palworld
  name_prefix  = "${var.game_name}-${terraform.workspace}"
  cluster_name = "${local.name_prefix}-cluster"
  service_name = "${local.name_prefix}-service"
}

# -----------------------------------------------------------
# VPC
# -----------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true # EFS の DNS 名前解決に必要

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

# インターネットゲートウェイ（ALB/NAT は使わず IGW 直結で固定費ゼロを実現）
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

# -----------------------------------------------------------
# パブリックサブネット（2 AZ）
# 2 AZ にするのは EFS マウントターゲットの冗長化と
# Fargate のタスク配置安定性のため（サブネット自体は無料）
# -----------------------------------------------------------

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-public-${count.index}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-rt"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# -----------------------------------------------------------
# セキュリティグループ
# -----------------------------------------------------------

# ゲームサーバー用 SG（game_ports に基づいてポートを動的に開放）
resource "aws_security_group" "game" {
  name        = "${local.name_prefix}-game-sg"
  description = "${var.game_name} game server security group"
  vpc_id      = aws_vpc.main.id

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
}

# EFS 用 SG（ゲーム SG からの NFS:2049 のみ許可）
resource "aws_security_group" "efs" {
  name        = "${local.name_prefix}-efs-sg"
  description = "EFS security group - allow NFS from game containers only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "NFS from game containers"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.game.id]
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
}
