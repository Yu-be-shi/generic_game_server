# ============================================================
# variables.tf - control-plane 変数定義
# ============================================================

variable "discord_public_key" {
  description = "Discord アプリの公開鍵。Discord Developer Portal → アプリ → General Information → PUBLIC KEY"
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9a-fA-F]{64}$", var.discord_public_key))
    error_message = "discord_public_key は 64 文字の16進数文字列を指定してください。"
  }
}

variable "aws_region" {
  description = "デプロイ先の AWS リージョン（game-stack と同じリージョンを指定）"
  type        = string
  default     = "ap-northeast-1"
}

variable "discord_allowed_user_ids" {
  description = <<-EOT
    操作を許可する Discord ユーザー ID のリスト。
    空リスト [] の場合は制限なし（サーバーメンバー全員が操作可）。
    自分専用にしたい場合は自分の ID を設定する。
    取得方法: Discord の「設定 → 詳細設定 → 開発者モードをON」→ 自分のアイコンを右クリック → 「IDをコピー」
  EOT
  type        = list(string)
  default     = []
}
