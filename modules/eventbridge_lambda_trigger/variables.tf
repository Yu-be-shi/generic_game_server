variable "rule_name" {
  type        = string
  description = "EventBridge ルール名"
}

variable "rule_description" {
  type        = string
  description = "EventBridge ルールの説明"
}

variable "schedule_expression" {
  type        = string
  description = "rate()/cron() 式。event_pattern と排他（どちらか一方のみ指定する）"
  default     = null
}

variable "event_pattern" {
  # 呼び出し元ごとにスキーマが異なる（SSM/ECS 等）ため any にし、モジュール側で jsonencode する
  type        = any
  description = "イベントパターン（HCL オブジェクト）。schedule_expression と排他（どちらか一方のみ指定する）"
  default     = null

  validation {
    condition     = (var.schedule_expression != null) != (var.event_pattern != null)
    error_message = "schedule_expression と event_pattern はどちらか一方だけを指定してください。"
  }
}

variable "function_name" {
  type        = string
  description = "ターゲット Lambda の function_name（aws_lambda_permission 用）"
}

variable "function_arn" {
  type        = string
  description = "ターゲット Lambda の ARN（aws_cloudwatch_event_target 用）"
}

variable "target_id" {
  type        = string
  description = "aws_cloudwatch_event_target の target_id"
}

variable "statement_id" {
  type        = string
  description = "aws_lambda_permission の statement_id（ForceNew 属性。既存リソースを移行する場合は元の値をそのまま渡すこと）"
}
