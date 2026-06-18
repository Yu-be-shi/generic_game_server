# ============================================================
# outputs.tf - terraform apply 後に表示される情報
# ============================================================
# ※ パブリック IP は動的なため出力対象外。Discord 通知で確認してください。

output "vpc_id" {
  description = "共有 VPC の ID（control-plane で管理・全ゲームで共有）"
  value       = data.aws_vpc.shared.id
}

output "public_subnet_ids" {
  description = "共有 VPC のパブリックサブネット ID 一覧（2 AZ）"
  value       = data.aws_subnets.public.ids
}

output "ecs_cluster_name" {
  description = "ECS クラスター名（Discord ボットがゲーム発見に Game タグを使用）"
  value       = aws_ecs_cluster.game.name
}

output "ecs_service_name" {
  description = "ECS サービス名"
  value       = aws_ecs_service.game.name
}

output "task_definition_arn" {
  description = "ECS タスク定義の ARN（最新リビジョン）"
  value       = aws_ecs_task_definition.game.arn
}

output "efs_id" {
  description = "EFS ファイルシステム ID"
  value       = aws_efs_file_system.main.id
}

output "efs_access_point_id" {
  description = "EFS アクセスポイント ID"
  value       = aws_efs_access_point.main.id
}

output "game_security_group_id" {
  description = "ゲームサーバー用セキュリティグループ ID"
  value       = aws_security_group.game.id
}

output "cost_sns_topic_arn" {
  description = "コストアラート用 SNS トピックの ARN"
  value       = aws_sns_topic.cost_alert.arn
}

output "cloudwatch_log_group" {
  description = "ECS タスクのログが届く CloudWatch Logs グループ名"
  value       = aws_cloudwatch_log_group.ecs.name
}

output "aws_region" {
  description = "デプロイリージョン"
  value       = var.aws_region
}

output "backup_bucket_name" {
  description = "セーブデータのバックアップ先 S3 バケット名。aws s3 sync または S3 コンソールから閲覧・ダウンロード可能"
  value       = aws_s3_bucket.backup.id
}

output "server_management_commands" {
  description = "サーバー管理 AWS CLI コマンド集（Discord ボット未使用時の手動操作）"
  value       = <<-EOT

    # === Discord コマンドが使えない場合の手動操作 ===

    # 起動:
    aws ecs update-service \
      --cluster ${aws_ecs_cluster.game.name} \
      --service ${aws_ecs_service.game.name} \
      --desired-count 1 \
      --region ${var.aws_region}

    # 停止:
    aws ecs update-service \
      --cluster ${aws_ecs_cluster.game.name} \
      --service ${aws_ecs_service.game.name} \
      --desired-count 0 \
      --region ${var.aws_region}

    # 状態確認:
    aws ecs describe-services \
      --cluster ${aws_ecs_cluster.game.name} \
      --services ${aws_ecs_service.game.name} \
      --region ${var.aws_region} \
      --query "services[0].{Status:status,Running:runningCount,Desired:desiredCount}"
  EOT
}

output "cost_notification_test_command" {
  description = <<-EOT
    コスト通知の疎通テスト用コマンド。
    実行すると SNS → Lambda(notify_cost) → Discord/Slack の経路を即検証できる。
    AWS Budgets の実際のしきい値到達を待たずに通知経路を確認可能。
  EOT
  value       = <<-EOT

    # === コスト通知の疎通テスト ===
    # 下記コマンドを実行し、Discord/Slack にテストメッセージが届けば
    # SNS → Lambda → webhook の経路が正常です。

    aws sns publish \
      --topic-arn ${aws_sns_topic.cost_alert.arn} \
      --subject "コスト通知 疎通テスト" \
      --message "これはテストです。Discord に届けば SNS→Lambda→webhook は正常です。" \
      --region ${var.aws_region}

    # DLQ 確認（Lambda 障害時のメッセージ蓄積を確認）:
    aws sqs get-queue-attributes \
      --queue-url $(aws sqs get-queue-url --queue-name ${aws_sqs_queue.notify_cost_dlq.name} --region ${var.aws_region} --query QueueUrl --output text) \
      --attribute-names ApproximateNumberOfMessages \
      --region ${var.aws_region}
  EOT
}
