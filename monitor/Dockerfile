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

# ベースイメージをダイジェストで固定（再現性確保・サプライチェーン保護）
# マルチアーキテクチャ対応: linux/amd64 + linux/arm64 の両方を含むインデックス digest
# 更新手順:
#   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/amazonlinux:pull" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
#   curl -s -H "Authorization: Bearer ${TOKEN}" \
#     -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json" \
#     -I "https://registry-1.docker.io/v2/library/amazonlinux/manifests/2023" | grep -i docker-content-digest
FROM amazonlinux:2023@sha256:d09f1353c3c0844852cff7bf103498a205532f5bca444e1ed6406e3c5402bb26

# 依存パッケージのみ事前インストール
# --setopt=install_weak_deps=False で推奨パッケージを省きイメージを軽量化
# dnf clean all でキャッシュを削除してイメージサイズを削減
RUN dnf install -y --setopt=install_weak_deps=False \
      iproute \
      python3 \
      awscli-2 \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# 【非 root 化の検討メモ】
# 事前ビルドイメージでは dnf install はスキップされるため USER 変更は技術的に可能。
# ただし aws s3 sync（EFS → S3 バックアップ）がゲームコンテナ書き込みファイル（root 所有）を
# 読む必要があるため、EFS アクセスポイントの uid/gid 設定と合わせて検討が必要。
# ゲームコンテナが特定 uid で書き込むことを保証できる場合は以下で非 root 化可能:
#   RUN useradd -r -u <uid> -s /sbin/nologin monitor
#   USER monitor
