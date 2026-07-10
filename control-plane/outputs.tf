# ============================================================
# outputs.tf - control-plane の出力
# ============================================================

output "tf_state_bucket_name" {
  description = "Terraform state 保存先の S3 バケット名。backend.hcl に記載して terraform init -migrate-state を実行する"
  value       = aws_s3_bucket.tf_state.id
}

output "interactions_endpoint_url" {
  description = "Discord Developer Portal の「Interactions Endpoint URL」に設定する URL。apply 直後にここに表示される。"
  value       = aws_apigatewayv2_stage.discord_control.invoke_url
}

output "lambda_function_name" {
  description = "Discord コントロール Lambda 関数名"
  value       = module.discord_control_lambda.function_name
}

output "shared_vpc_id" {
  description = "全ゲームで共有する VPC の ID。game-stack は 'ggs-shared-vpc' タグで自動参照するため、この値の直接設定は不要。"
  value       = aws_vpc.shared.id
}

output "shared_public_subnet_ids" {
  description = "共有 VPC のパブリックサブネット ID 一覧（2 AZ）。game-stack は 'ggs-shared=true' タグで自動参照する。"
  value       = aws_subnet.public[*].id
}

output "monitor_ecr_repository_uri" {
  description = "モニターサイドカー用 ECR リポジトリ URI。game-stack の monitor_image 変数に設定して起動時の dnf install を省略できる（詳細: control-plane/ecr.tf のコメント参照）"
  value       = aws_ecr_repository.monitor.repository_url
}

output "next_steps" {
  description = "デプロイ後の次のステップ"
  value       = <<-EOT

    ======================================================
    デプロイ完了！次のステップ:
    ======================================================

    1. Discord Developer Portal にアクセス:
       https://discord.com/developers/applications

    2. アプリを選択 → General Information 画面を開く

    3. "INTERACTIONS ENDPOINT URL" に以下を貼り付けて保存:
       ${aws_apigatewayv2_stage.discord_control.invoke_url}
       （保存できれば署名検証成功）

    4. スラッシュコマンドを登録（1回だけ実行）:
       export DISCORD_APP_ID=<あなたの App ID>
       export DISCORD_BOT_TOKEN=<あなたの Bot Token>
       bash scripts/register_commands.sh

    5. Discord でコマンドを試す:
       /games              → 全ゲームの稼働状態を確認
       /start game:palworld → サーバー起動（IPが自動通知される）
       /stop game:palworld  → サーバー停止
       /status game:palworld → 現在のIP確認
    ======================================================
  EOT
}
