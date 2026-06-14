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
  EOT
  type        = string
  default     = "tcp"

  validation {
    condition     = contains(["tcp", "udp"], var.monitor_protocol)
    error_message = "monitor_protocol は 'tcp' または 'udp' を指定してください。"
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
  description = "Discord の Webhook URL。IP 通知およびコストアラートの送信先。Discordサーバー設定 → 連携サービス → ウェブフック から取得"
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^https://discord\\.com/api/webhooks/", var.discord_webhook_url))
    error_message = "discord_webhook_url は https://discord.com/api/webhooks/ で始まる URL を指定してください。"
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

# -----------------------------------------------------------
# 補助変数（デフォルト値あり）
# -----------------------------------------------------------

variable "aws_region" {
  description = "デプロイ先の AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}
