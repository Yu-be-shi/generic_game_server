variable "function_name" {
  type        = string
  description = "Lambda 関数名（IAM ロール名のデフォルト値としても使う）"
}

variable "role_name" {
  type        = string
  description = "IAM ロール名。省略時は function_name を使う（歴史的事情で名前が不一致な場合のみ明示指定する）"
  default     = null
}

variable "filename" {
  type        = string
  description = "デプロイパッケージ（zip）のパス。呼び出し元の archive_file.output_path を渡す"
}

variable "source_code_hash" {
  type        = string
  description = "呼び出し元の archive_file.output_base64sha256 を渡す"
}

variable "handler" {
  type        = string
  description = "Lambda ハンドラ（例: notify_ip.lambda_handler）"
}

variable "runtime" {
  type    = string
  default = "python3.12"
}

variable "architectures" {
  type    = list(string)
  default = ["arm64"]
}

variable "timeout" {
  type        = number
  description = "Lambda タイムアウト秒数"
}

variable "memory_size" {
  type        = number
  description = "メモリサイズ(MB)。省略時は AWS のデフォルト値（128MB）"
  default     = null
}

variable "environment_variables" {
  type        = map(string)
  description = "Lambda の環境変数"
  default     = {}
}

variable "extra_iam_statements" {
  # 各ステートメントが Condition の有無等で属性の異なるオブジェクトになるため、
  # list(any) では型統一エラーになる。any にして jsonencode() にそのまま渡す。
  type        = any
  description = "CloudWatch Logs 権限に追加する IAM ポリシーステートメント"
  default     = []
}

variable "vpc_config" {
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  description = "VPC 内配置が必要な Lambda のみ指定する（EFS マウント等）。指定時は AWSLambdaVPCAccessExecutionRole を自動アタッチする"
  default     = null
}

variable "file_system_config" {
  type = object({
    arn              = string
    local_mount_path = string
  })
  description = "EFS アクセスポイントをマウントする場合のみ指定する"
  default     = null
}

variable "dead_letter_target_arn" {
  type        = string
  description = "非同期 invoke 失敗時の送信先（SQS/SNS の ARN）。使わない場合は null"
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 7
}
