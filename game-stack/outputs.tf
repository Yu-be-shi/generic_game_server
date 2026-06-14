# ============================================================
# outputs.tf - terraform apply 後に表示される情報
# ============================================================
# ※ パブリック IP は動的なため出力対象外。Discord 通知で確認してください。

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "パブリックサブネットの ID 一覧（2 AZ）"
  value       = aws_subnet.public[*].id
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
