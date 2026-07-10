# ============================================================
# modules/eventbridge_lambda_trigger - EventBridge → Lambda トリガー共通定義
# ============================================================
# aws_cloudwatch_event_rule → aws_cloudwatch_event_target → aws_lambda_permission
# という定型パターンを共通化する。schedule_expression（rate/cron）と
# event_pattern（イベントパターンマッチ）のどちらのルールにも対応する。

resource "aws_cloudwatch_event_rule" "this" {
  name        = var.rule_name
  description = var.rule_description

  schedule_expression = var.schedule_expression
  event_pattern       = var.event_pattern != null ? jsonencode(var.event_pattern) : null

  tags = { Name = var.rule_name }
}

resource "aws_cloudwatch_event_target" "this" {
  rule      = aws_cloudwatch_event_rule.this.name
  target_id = var.target_id
  arn       = var.function_arn
}

resource "aws_lambda_permission" "this" {
  statement_id  = var.statement_id
  action        = "lambda:InvokeFunction"
  function_name = var.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.this.arn
}
