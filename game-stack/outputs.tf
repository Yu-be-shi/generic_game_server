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

# 手動操作・コスト通知疎通テストの AWS CLI コマンド集は CLAUDE.md
# 「手動サーバー操作（Discord が使えない場合）」節を参照（ドリフトしやすい heredoc として
# terraform output に埋め込むのではなく、ドキュメントとして一元管理する）。
