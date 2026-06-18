# ============================================================
# network.tf - 全ゲームで共有する VPC リソース
# ============================================================
# 設計方針（固定費ゼロ）:
#   NAT Gateway / ALB / NLB / Route53 は使用しない。
#   ECS タスクに直接パブリック IP を付与し、インターネット通信を実現する。
#
# game-stack はこの VPC を「ggs-shared-vpc」タグで動的に参照する。
# control-plane を先に apply してからゲームスタックを apply すること。
#
# スケーリング:
#   かつてはゲームごとに 1 VPC を作成していたが、1 リージョンあたりの
#   デフォルト VPC 上限（5 個）に当たるため、ここに集約した。
#   ゲームごとのセキュリティグループは各 game-stack で管理し、
#   ネットワーク的な分離は維持している。
# ============================================================

data "aws_availability_zones" "available" {
  state = "available"
}

# -----------------------------------------------------------
# 共有 VPC
# -----------------------------------------------------------

resource "aws_vpc" "shared" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true # EFS の DNS 名前解決に必要

  tags = {
    # game-stack が data "aws_vpc" のタグルックアップで参照するキー名
    Name = "ggs-shared-vpc"
  }
}

# インターネットゲートウェイ（ALB/NAT は使わず IGW 直結で固定費ゼロを実現）
resource "aws_internet_gateway" "shared" {
  vpc_id = aws_vpc.shared.id

  tags = {
    Name = "ggs-shared-igw"
  }
}

# -----------------------------------------------------------
# パブリックサブネット（2 AZ）
# 2 AZ にするのは EFS マウントターゲットの冗長化と
# Fargate のタスク配置安定性のため（サブネット自体は無料）
# -----------------------------------------------------------

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.shared.id
  cidr_block              = "10.0.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true # ECS タスクに直接パブリック IP を付与

  tags = {
    Name = "ggs-shared-public-${count.index}"
    # game-stack が data "aws_subnets" のタグフィルタで参照するキー
    ggs-shared = "true"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.shared.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.shared.id
  }

  tags = {
    Name = "ggs-shared-rt"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# -----------------------------------------------------------
# S3 Gateway VPC Endpoint（無料）
# VPC 内の backup Lambda が NAT Gateway なしで S3 に直接アクセスするために必要。
# かつては各 game-stack で作成していたが、VPC が 1 つになったためここへ移設。
# -----------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.shared.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"

  # 共有パブリックルートテーブルに S3 エントリを追加する
  route_table_ids = [aws_route_table.public.id]

  tags = {
    Name = "ggs-shared-s3-endpoint"
  }
}
