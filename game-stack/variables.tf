# ============================================================
# variables.tf - 変数定義（全てのゲームで共通）
# ============================================================
# 使い方:
#   ゲームごとに ../games/<game>.tfvars を作成し、workspace で state を分離する
#   例:
#     terraform workspace new palworld
#     terraform apply -var-file=../games/palworld.tfvars

# -----------------------------------------------------------
# ゲーム識別・コンテナ設定
# -----------------------------------------------------------

variable "game_name" {
  description = "ゲームの識別名。AWS リソース名の prefix として使用される（小文字英字で始まり、英小文字・数字・ハイフンのみ使用可能）"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,29}$", var.game_name))
    error_message = "game_name は小文字英字で始まり、英小文字・数字・ハイフンのみ使用可能で 2〜30 文字にしてください（例: palworld, minecraft-java）。"
  }
}

variable "docker_image" {
  description = "ゲームサーバーの Docker イメージ（Docker Hub / ECR 等）。例: thijsvanloef/palworld-server-docker:latest"
  type        = string
}

variable "task_cpu" {
  description = <<-EOT
    Fargate タスクの CPU ユニット数（vCPU x 256）。
    許容値: 256 (0.25vCPU) / 512 (0.5vCPU) / 1024 (1vCPU) / 2048 (2vCPU)
    !! 高額誤デプロイ防止のため 2048 (2vCPU) を上限とする !!
    CPU/Memory の有効な組み合わせ:
      256  CPU → 512〜2048 MB
      512  CPU → 1024〜4096 MB
      1024 CPU → 2048〜4096 MB
      2048 CPU → 4096 MB (本構成上限)
  EOT
  type        = number

  validation {
    condition     = contains([256, 512, 1024, 2048], var.task_cpu)
    error_message = "task_cpu は 256, 512, 1024, 2048 のいずれかを指定してください。高額誤デプロイ防止のため 4096 以上は設定不可。"
  }
}

variable "task_memory" {
  description = <<-EOT
    Fargate タスクのメモリ (MiB)。
    許容値: 512 / 1024 / 2048 / 3072 / 4096
    !! 高額誤デプロイ防止のため 4096 MB (4GB) を上限とする !!
    ※ task_cpu との組み合わせ制約に注意（詳細は task_cpu の説明を参照）
  EOT
  type        = number

  validation {
    condition     = contains([512, 1024, 2048, 3072, 4096, 8192, 16384], var.task_memory)
    error_message = "task_memory は 512, 1024, 2048, 3072, 4096 のいずれかを指定してください。高額誤デプロイ防止のため 4096 MB 以上は設定不可。"
  }
}

variable "game_ports" {
  description = "ゲームサーバーが使用するポートのリスト。protocol は 'tcp' または 'udp'"
  type = list(object({
    port        = number
    protocol    = string
    description = optional(string, "")
  }))

  validation {
    condition = alltrue([
      for p in var.game_ports : contains(["tcp", "udp", "TCP", "UDP"], p.protocol)
    ])
    error_message = "各ポートの protocol は 'tcp' または 'udp' を指定してください。"
  }

  validation {
    condition = alltrue([
      for p in var.game_ports : p.port >= 1 && p.port <= 65535
    ])
    error_message = "各ポートの port は 1〜65535 の範囲で指定してください。"
  }
}

variable "efs_mount_path" {
  description = "EFS ボリュームをコンテナにマウントするパス。例: /palworld, /data, /minecraft"
  type        = string

  validation {
    condition     = can(regex("^/", var.efs_mount_path))
    error_message = "efs_mount_path は / で始まる絶対パスを指定してください（例: /palworld）。"
  }
}

variable "environment_variables" {
  description = "ゲームコンテナに渡す環境変数。例: { SERVER_NAME = \"MyServer\", PLAYERS = \"16\" }"
  type        = map(string)
  default     = {}
}

# Steam バージョンチェック設定（非 Steam 系ゲームは空のまま）
variable "steam_app_id" {
  description = <<-EOT
    Steam Dedicated Server のアプリID（数字のみ）。
    設定すると /update 実行時に steamcmd.net の公開 API でバージョンを事前照合し、
    既に最新の場合はコンテナを起動せず数秒で完了します。
    空文字列（デフォルト）の場合、チェックを行わず常にアップデートを実行します。
    例: Palworld Dedicated Server = "2394010"
        非 Steam 系（Minecraft 等）= "" のまま
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[0-9]*$", var.steam_app_id))
    error_message = "steam_app_id は数字のみで構成される文字列（または空文字列）を指定してください。"
  }
}

variable "steam_branch" {
  description = "steamcmd.net でバージョン確認する際のブランチ名（通常は \"public\"）。"
  type        = string
  default     = "public"
}

# -----------------------------------------------------------
# 自動シャットダウン（サイドカー監視）設定
# -----------------------------------------------------------

variable "monitor_port" {
  description = "サイドカーが無人検知に使用する監視ポート番号（通常はメインゲームポートと同一）"
  type        = number

  validation {
    condition     = var.monitor_port >= 1 && var.monitor_port <= 65535
    error_message = "monitor_port は 1〜65535 の範囲で指定してください。"
  }
}

variable "monitor_protocol" {
  description = <<-EOT
    監視プロトコル。'tcp' または 'udp'。
    tcp: ss コマンドで ESTABLISHED 接続数をカウント（精度高）
    udp: UDP は接続の概念がなく ss では正確にカウントできないため暫定措置。
         Steam ベースのゲームには A2S_INFO クエリ実装を推奨（scripts/auto_shutdown.sh 参照）
    monitor_method を設定した場合はそちらが優先される。
  EOT
  type        = string
  default     = "tcp"

  validation {
    condition     = contains(["tcp", "udp"], var.monitor_protocol)
    error_message = "monitor_protocol は 'tcp' または 'udp' を指定してください。"
  }
}

variable "monitor_method" {
  description = <<-EOT
    無人検知の方式（monitor_protocol より優先）。
    tcp:  ss コマンドで TCP ESTABLISHED 接続数をカウント
    a2s:  Steam A2S_INFO クエリでプレイヤー数を取得（Steam ゲーム汎用）
    rest: REST API GET /v1/api/players でプレイヤー数を取得（Palworld 推奨）
    空文字 / 未設定の場合は monitor_protocol から自動判定（後方互換）
  EOT
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "tcp", "a2s", "rest"], var.monitor_method)
    error_message = "monitor_method は tcp / a2s / rest のいずれか（または空文字）を指定してください。"
  }
}

variable "rest_api_port" {
  description = "REST API 方式（monitor_method=rest）で問い合わせるポート番号。Palworld デフォルトは 8212。"
  type        = number
  default     = 8212

  validation {
    condition     = var.rest_api_port >= 1 && var.rest_api_port <= 65535
    error_message = "rest_api_port は 1〜65535 の範囲で指定してください。"
  }
}

variable "idle_timeout_minutes" {
  description = "プレイヤー接続がゼロの状態が何分続いたら無人と判断してタスクを停止するか"
  type        = number
  default     = 10

  validation {
    condition     = var.idle_timeout_minutes >= 1 && var.idle_timeout_minutes <= 120
    error_message = "idle_timeout_minutes は 1〜120 の範囲で指定してください。"
  }
}

variable "desired_count" {
  description = "ECS Service の希望タスク数。通常は 1（サーバー1台）。0 で停止状態"
  type        = number
  default     = 1

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count は 0（停止）または 1（起動）を指定してください。"
  }
}

# -----------------------------------------------------------
# Discord 通知設定
# -----------------------------------------------------------

variable "discord_webhook_url" {
  description = "通知先 Webhook URL。Discord の場合は Discordサーバー設定 → 連携サービス → ウェブフック から取得。Slack 等に切り替える場合はその Incoming Webhook URL を指定"
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^https://", var.discord_webhook_url))
    error_message = "discord_webhook_url は https:// で始まる URL を指定してください。"
  }
}

variable "messaging_provider" {
  description = "メッセージングプロバイダー。'discord' または 'slack'（既定: discord）。切り替え時は discord_webhook_url に対応ツールの Webhook URL を設定し、control-plane/functions/discord_control/provider.py も更新すること"
  type        = string
  default     = "discord"

  validation {
    condition     = contains(["discord", "slack"], var.messaging_provider)
    error_message = "messaging_provider は 'discord' または 'slack' を指定してください。"
  }
}

# -----------------------------------------------------------
# 予算・コスト管理
# -----------------------------------------------------------

variable "budget_limit_usd" {
  description = "月間予算の上限（USD）。20%/50%/80%/100% 到達時に Discord へ通知。デフォルト 13ドル≒2000円"
  type        = number
  default     = 13.0

  validation {
    condition     = var.budget_limit_usd > 0 && var.budget_limit_usd <= 100
    error_message = "budget_limit_usd は 0 より大きく 100 以下の値を指定してください。"
  }
}

variable "alert_email" {
  description = <<-EOT
    コストアラートのメール送信先（オプション）。
    設定するとコスト通知 SNS トピックにメール購読が追加され、
    Discord/Lambda が障害を起こしても独立したチャネルで通知が届く。
    空文字列（デフォルト）の場合はメール通知を作成しない。
    初回 apply 後に AWS から確認メールが届くので「Confirm subscription」リンクを踏むこと。
    例: "me@example.com"
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.alert_email == "" || can(regex("^[^@]+@[^@]+\\.[^@]+$", var.alert_email))
    error_message = "alert_email は空文字列または有効なメールアドレス形式で指定してください。"
  }
}

variable "max_task_runtime_hours" {
  description = <<-EOT
    コストガードバックストップの閾値（時間）。
    この時間を超えて RUNNING なタスクを強制停止する。
    アイドル自動停止（monitor サイドカー）の代替ではなく最終安全網。
    通常プレイ中に誤発動しないよう余裕を持った値（デフォルト 12 時間）を推奨。
    max_runtime_hours > idle_timeout_minutes/60 となるよう設定すること。
  EOT
  type        = number
  default     = 24

  validation {
    condition     = var.max_task_runtime_hours >= 1 && var.max_task_runtime_hours <= 72
    error_message = "max_task_runtime_hours は 1〜72 の範囲で指定してください。"
  }
}

# -----------------------------------------------------------
# 補助変数（デフォルト値あり）
# -----------------------------------------------------------

variable "aws_region" {
  description = "デプロイ先の AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}
