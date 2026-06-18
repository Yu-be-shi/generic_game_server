# ============================================================
# ecr.tf - モニターサイドカー用 ECR リポジトリ（オプション）
# ============================================================
# 目的:
#   game-stack のモニターサイドカーは既定では amazonlinux:2023 を使い
#   起動毎に dnf install を実行する。このリポジトリに事前ビルドイメージを
#   プッシュして game-stack の monitor_image 変数に URI を設定すると
#   dnf install をスキップし起動時間を短縮できる。
#
# 使い方（初回のみ）:
#   # 1. この control-plane を apply してリポジトリを作成
#   terraform apply
#
#   # 2. リポジトリ URI を取得（outputs に表示される）
#   terraform output monitor_ecr_repository_uri
#
#   # 3. イメージをビルド & プッシュ
#   REPO=$(terraform output -raw monitor_ecr_repository_uri)
#   aws ecr get-login-password --region ap-northeast-1 | \
#     docker login --username AWS --password-stdin "${REPO%%:*}"
#
#   # ※ arm64 と x86_64 を両方サポートする場合はマルチアーキビルド:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t "${REPO}:latest" --push ../monitor/
#   # x86_64 のみの場合:
#   docker build -t "${REPO}:latest" ../monitor/
#   docker push "${REPO}:latest"
#
#   # 4. game-stack の tfvars に以下を追加:
#   #   monitor_image = "<上記 URI>:latest"
# ============================================================

resource "aws_ecr_repository" "monitor" {
  name                 = "ggs-monitor"
  image_tag_mutability = "MUTABLE" # :latest タグを上書きして常に最新を参照

  image_scanning_configuration {
    scan_on_push = true # プッシュ時に脆弱性スキャン（無料）
  }

  tags = {
    Name    = "ggs-monitor"
    Purpose = "game-stack monitor sidecar prebuilt image"
  }
}

resource "aws_ecr_lifecycle_policy" "monitor" {
  repository = aws_ecr_repository.monitor.name

  # 最新 5 世代のみ保持して ECR ストレージ代を抑制する
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "最新 5 イメージのみ保持"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = {
        type = "expire"
      }
    }]
  })
}
