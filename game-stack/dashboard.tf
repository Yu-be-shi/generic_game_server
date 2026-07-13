# =============================================================================
# CloudWatch ダッシュボード（ゲームごと）
#
# コスト（1〜2 ゲーム運用なら $0）:
#   - ダッシュボード: アカウントあたり 3 個まで常時無料（以降 $3.00/個/月）
#   - PlayersOnline カスタムメトリクス: 10 個まで常時無料（以降 $0.30/個/月を
#     データが存在した時間で按分 = 停止中はゼロ。ログメトリクスフィルタ由来のため
#     タスクロールへの cloudwatch:PutMetricData 権限追加も不要）
#   - ログウィジェット: 表示のたび Logs Insights スキャン $0.005/GB（ログは数 MB 規模で実質 $0）
#   - AWS/ECS・AWS/Lambda の標準メトリクスは無料（Container Insights は使わない）
# =============================================================================

# monitor サイドカーの定型行 "[monitor] PLAYERS <数値>"（auto_shutdown.sh）から
# プレイヤー数をメトリクス化する。default_value を設定しない = サーバー停止中は
# データポイントなし（ダッシュボード上で「データなし = 停止中」と読める）
resource "aws_cloudwatch_log_metric_filter" "players" {
  name           = "${local.name_prefix}-players"
  log_group_name = aws_cloudwatch_log_group.ecs.name
  # 注: prefix="[monitor]" のような角括弧の完全一致指定はパターン構文上マッチしないため
  # ワイルドカード形式で照合する（aws logs test-metric-filter で検証済み）
  pattern = "[prefix=\"*monitor*\", label=PLAYERS, value]"

  metric_transformation {
    name      = "PlayersOnline"
    namespace = "GGS/${local.name_prefix}"
    value     = "$value"
  }
}

locals {
  # ゲームスタックの全 Lambda（エラー監視ウィジェット用）
  dashboard_lambda_names = [
    "${local.name_prefix}-notify-ip",
    "${local.name_prefix}-auto-update",
    "${local.name_prefix}-backup-efs",
    "${local.name_prefix}-cost-guard",
    "${local.name_prefix}-notify-cost",
    "${local.name_prefix}-notify-backup",
  ]
}

resource "aws_cloudwatch_dashboard" "game" {
  dashboard_name = local.name_prefix

  dashboard_body = jsonencode({
    widgets = [
      # --- Row 1: プレイヤー数（現在値 + 推移）。データなし = サーバー停止中 ---
      {
        type = "metric", x = 0, y = 0, width = 6, height = 6
        properties = {
          title   = "プレイヤー数（データなし = 停止中）"
          view    = "singleValue"
          region  = var.aws_region
          metrics = [["GGS/${local.name_prefix}", "PlayersOnline"]]
          stat    = "Maximum"
          period  = 300
        }
      },
      {
        type = "metric", x = 6, y = 0, width = 18, height = 6
        properties = {
          title   = "プレイヤー数の推移"
          view    = "timeSeries"
          region  = var.aws_region
          metrics = [["GGS/${local.name_prefix}", "PlayersOnline"]]
          stat    = "Maximum"
          period  = 300
        }
      },
      # --- Row 2: ECS リソース使用率（標準メトリクス・無料）と Lambda エラー ---
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "ECS CPU / メモリ使用率 (%)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", local.cluster_name, "ServiceName", local.service_name],
            ["AWS/ECS", "MemoryUtilization", "ClusterName", local.cluster_name, "ServiceName", local.service_name],
          ]
          stat   = "Average"
          period = 60
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Lambda エラー数"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [
            for fn in local.dashboard_lambda_names :
            ["AWS/Lambda", "Errors", "FunctionName", fn]
          ]
          stat   = "Sum"
          period = 300
        }
      },
      # --- Row 3: 直近ログ（ゲーム / モニターサイドカー）---
      {
        type = "log", x = 0, y = 12, width = 12, height = 8
        properties = {
          title  = "ゲームサーバーログ（直近 100 件）"
          region = var.aws_region
          view   = "table"
          query  = "SOURCE '${aws_cloudwatch_log_group.ecs.name}' | fields @timestamp, @message | filter @logStream like 'game/' | sort @timestamp desc | limit 100"
        }
      },
      {
        type = "log", x = 12, y = 12, width = 12, height = 8
        properties = {
          title  = "モニターサイドカーログ（直近 100 件）"
          region = var.aws_region
          view   = "table"
          query  = "SOURCE '${aws_cloudwatch_log_group.ecs.name}' | fields @timestamp, @message | filter @logStream like 'monitor/' | sort @timestamp desc | limit 100"
        }
      },
    ]
  })
}
