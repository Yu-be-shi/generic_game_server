variable "source_dir" {
  type        = string
  description = "Lambda ハンドラのソースディレクトリ。呼び出し元の path.module 起点で渡す（例: \"$${path.module}/functions/notify_ip\"）"
}

variable "source_pattern" {
  type        = string
  description = "source_dir から zip に含めるファイルの fileset パターン。サブディレクトリも含める場合は \"**/*.py\""
  default     = "*.py"
}

variable "shared_dir" {
  type        = string
  description = "共有モジュール（_shared）のディレクトリ。呼び出し元の path.module 起点で渡す"
  default     = null
}

variable "shared_files" {
  type        = list(string)
  description = "shared_dir から zip 直下に同梱するファイル名のリスト"
  default     = []
}

variable "output_path" {
  type        = string
  description = "zip の出力先パス。既存の出力先を変えないため呼び出し元で明示する"
}
