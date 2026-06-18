# ============================================================
# monitor/Dockerfile - モニターサイドカー事前ビルドイメージ
# ============================================================
# 目的:
#   game-stack のモニターサイドカーは既定で amazonlinux:2023 を使い
#   起動のたびに dnf install を実行する。このイメージを ECR にプッシュして
#   game-stack の monitor_image 変数に URI を設定することで
#   起動時の dnf install をスキップし起動時間（≒課金時間）を短縮できる。
#
# ビルド & プッシュ（control-plane/ecr.tf でリポジトリを apply 済みの前提）:
#
#   # リポジトリ URI 取得
#   cd control-plane
#   REPO=$(terraform output -raw monitor_ecr_repository_uri)
#
#   # ECR ログイン
#   aws ecr get-login-password --region ap-northeast-1 | \
#     docker login --username AWS --password-stdin "${REPO%%:*}"
#
#   # arm64 + x86_64 マルチアーキビルド（task_cpu_architecture="ARM64" を使う場合）:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t "${REPO}:latest" --push ../monitor/
#
#   # x86_64 のみ（シンプルな場合）:
#   docker build -t "${REPO}:latest" ../monitor/
#   docker push "${REPO}:latest"
#
# スクリプトはイメージに焼き込まない。
# game-stack/scripts/auto_shutdown.sh を Terraform がコンテナ定義に注入するため
# イメージ更新なしでスクリプトのみ変更可能（従来の設計を維持）。
# ============================================================

FROM amazonlinux:2023

# 依存パッケージのみ事前インストール
# --setopt=install_weak_deps=False で推奨パッケージを省きイメージを軽量化
# dnf clean all でキャッシュを削除してイメージサイズを削減
RUN dnf install -y --setopt=install_weak_deps=False \
      iproute \
      python3 \
      awscli-2 \
    && dnf clean all \
    && rm -rf /var/cache/dnf
