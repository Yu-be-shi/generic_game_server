# =============================================================================
# example.tfvars - 変数設定ファイルの記入例
# =============================================================================
# このファイルをコピーして、ゲームごとの .tfvars ファイルを作成してください。
#
# 【使い方（game-stack ディレクトリから実行）】
#
#   # 初回
#   cd generic_game_server/game-stack
#   terraform init
#   cp ../games/example.tfvars ../games/palworld.tfvars
#   # ../games/palworld.tfvars を編集する
#   terraform workspace new palworld
#   terraform apply -var-file=../games/palworld.tfvars
#
#   # 別ゲームを追加（別 state・別リソース・別スペック）
#   cp ../games/example.tfvars ../games/minecraft.tfvars
#   # ../games/minecraft.tfvars を編集する（task_cpu=1024 など）
#   terraform workspace new minecraft
#   terraform apply -var-file=../games/minecraft.tfvars
#
#   # 起動後 → Discord で /start game:palworld と送信するだけ！
#
# 【注意】*.tfvars ファイルは .gitignore に含まれています。
#          discord_webhook_url 等の機密情報が Git にコミットされません。
# =============================================================================

# =============================================================================
# 前提: control-plane を先に apply して共有 VPC を作成すること
# =============================================================================
#   cd ../control-plane && terraform apply
#
# game-stack は VPC を自前で作成せず、control-plane が作成した共有 VPC を
# "ggs-shared-vpc" タグで自動参照します。
# =============================================================================

# ゲーム識別名（小文字英字で始まり、英小文字・数字・ハイフンのみ）
# ECS クラスターに Game タグとして付与される（Discord ボットがこれでゲームを発見する）
game_name = "palworld"

# 使用する Docker イメージ
docker_image = "thijsvanloef/palworld-server-docker:latest"

# =============================================================================
# Fargate リソース割り当て（高額誤デプロイ防止バリデーションあり）
# =============================================================================
# 許容値（AWS の CPU/Memory 組み合わせ制約あり）:
#   256  CPU → 512〜2048 MB のみ有効
#   512  CPU → 1024〜4096 MB のみ有効
#   1024 CPU → 2048〜4096 MB のみ有効
#   2048 CPU → 4096 MB のみ有効（本構成の上限）
#
# 参考コスト（東京リージョン・起動中のみ課金）:
#   1024 CPU / 2048 MB → 約 $0.026/h（≒ 4 円/h）
#   2048 CPU / 4096 MB → 約 $0.052/h（≒ 8 円/h）
task_cpu    = 2048
task_memory = 4096

# ゲームポート（protocol は "tcp" または "udp"）
# 先頭のポートが「クライアントの接続先」として Discord の起動通知（IP:ポート）に使われる
game_ports = [
  {
    port        = 8211
    protocol    = "udp"
    description = "Palworld ゲームポート"
  },
  {
    port        = 27015
    protocol    = "udp"
    description = "Palworld クエリポート"
  },
]

# コンテナ内のゲームデータをマウントするパス（EFS がここにマウントされる）
efs_mount_path = "/palworld"

# セーブデータの切り替え用識別子（省略可・既定は空文字列）
# 同じ game_name のまま、EFS 上の保存先ディレクトリと S3 バックアッププレフィックスだけを
# 切り替えたい場合に設定する（例: "world2"）。切り替え手順は CLAUDE.md 参照。
# save_slot = "world2"

# ゲーム固有の環境変数（不要な場合は {} を指定）
environment_variables = {
  PLAYERS         = "16"
  SERVER_PASSWORD = "your_server_password_here"
  ADMIN_PASSWORD  = "your_admin_password_here"
  SERVER_NAME     = "My Palworld Server"
  MULTITHREADING  = "true"
  COMMUNITY       = "false"
}

# =============================================================================
# 無人検知・自動シャットダウン設定
# =============================================================================
# monitor_method: ゲームタイプに合わせて必ず設定する（重要）
#   "tcp"  — Minecraft Java 等 TCP ゲーム（ss コマンドで接続数カウント）
#   "a2s"  — Valheim・CS2 等 A2S_INFO 対応の Steam ゲーム（Steam クエリプロトコル）
#   "rest" — Palworld（REST API GET /v1/api/players でプレイヤー数取得）
monitor_method       = "rest"
monitor_port         = 8211
monitor_protocol     = "udp" # monitor_method 設定時は参考値（fallback 用）
idle_timeout_minutes = 10
desired_count        = 1

# REST API 方式（monitor_method = "rest"）のポート番号（Palworld デフォルト: 8212）
# rest_api_port = 8212

# =============================================================================
# Steam バージョン事前チェック（/update 高速化）
# =============================================================================
# 設定すると /update 実行時にコンテナを起動する前に steamcmd.net の公開 API で
# 最新バージョンを照合し、既に最新の場合は数秒で完了します（コンテナ起動不要）。
#
# steam_app_id: Steam Dedicated Server の数字 ID（ゲームクライアント ID と異なる場合があります）
#   Palworld Dedicated Server: "2394010"
#   非 Steam 系ゲーム（Minecraft 等）: "" のまま（チェックをスキップ）
#
# steam_branch: バージョン確認に使うブランチ（通常は "public" のまま）
#
steam_app_id = "2394010" # Palworld Dedicated Server App ID
steam_branch = "public"  # 通常は変更不要

# =============================================================================
# Discord 通知設定（Webhook URL）
# =============================================================================
# Discordサーバー設定 → 連携サービス → ウェブフック から取得
discord_webhook_url = "https://discord.com/api/webhooks/XXXXXXXXXX/YYYYYYYYYYY"

# 運用者向け通知（コストアラート・コストガード）の分離先 Webhook URL（オプション）
# コスト通知には AWS アカウント ID が含まれるため、プレイヤーも見る一般チャンネルと
# 分けたい場合に管理者専用チャンネルの Webhook を指定する。未設定なら上記 URL に送信
# admin_webhook_url = "https://discord.com/api/webhooks/XXXXXXXXXX/ZZZZZZZZZZZ"

# メッセージングプロバイダー（"discord" または "slack"。既定: "discord"）
# messaging_provider = "discord"

# =============================================================================
# 予算・コスト管理
# =============================================================================
budget_limit_usd = 13.0 # 月間上限（USD）。デフォルト: 13ドル ≒ 2000円

# コスト超過アラートのメール送信先（オプション。Discord が壊れても通知を受け取る独立経路）
# 設定後に AWS から確認メールが届くので「Confirm subscription」リンクを踏むこと。
# alert_email = "me@example.com"

# cost_guard: アイドル検知が機能しない場合の最終安全網（この時間を超えると強制停止）
# max_task_runtime_hours = 24  # デフォルト: 24時間（通常プレイ中に誤発動しない余裕を持った値）

# =============================================================================
# AWS リージョン（変更が必要な場合のみ記載）
# =============================================================================
aws_region = "ap-northeast-1"

# =============================================================================
# コスト最適化オプション（既定値のままでも動作する）
# =============================================================================

# Fargate タスクの CPU アーキテクチャ。
# "ARM64" (Graviton) にすると同一性能あたり約 20% コスト削減。
# !! ゲーム本体の Docker イメージが arm64 に「ネイティブ」対応している場合のみ変更すること !!
# 確認方法: docker manifest inspect <image> | grep -A2 linux/arm64
# 適用 OK: Minecraft Java 版（JVM はマルチアーキ対応）
# 非推奨  : Palworld 等 x86 専用 Steam ゲーム（box64/FEX エミュレーションは効果相殺・不安定）
# 既定: "X86_64"（従来どおりの動作）
# task_cpu_architecture = "ARM64"

# EFS ファイルシステムのストレージクラス（作成時に決定・後から変更不可）。
# "one_zone": 単一 AZ に配置。Standard の約 45% 安。
#   ECS タスク・バックアップ Lambda も同一 AZ に固定される。
#   EFS Archive 階層（AFTER_90_DAYS）は Regional 専用のため one_zone 選択時は IA 止まり。
#   !! 後から変更する場合は terraform destroy → S3 復元が必要 !!
# "regional"（既定）: 複数 AZ に冗長配置。Archive 自動階層化も有効。
# efs_storage_class = "one_zone"

# モニターサイドカーのイメージ。既定の "amazonlinux:2023" は起動毎に dnf install を実行する。
# control-plane を apply 後に terraform output monitor_ecr_repository_uri で URI を確認し、
# 事前ビルドイメージをプッシュしてから URI を指定すると起動時間を短縮できる。
# 既定: "amazonlinux:2023"（追加準備不要）
# monitor_image = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/ggs-monitor:latest"
