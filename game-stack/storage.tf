# ============================================================
# storage.tf - EFS（セーブデータの永続化）
# ============================================================
# EFS アクセスポイントを使い uid/gid を固定することで
# コンテナ起動時の「Permission denied」エラーを防止する。

# EFS ファイルシステム
resource "aws_efs_file_system" "main" {
  encrypted       = true      # 保存データを暗号化
  throughput_mode = "elastic" # regional の Archive 移行（lifecycle_policy）が Elastic 必須のため

  # One Zone 選択時: 指定 AZ の単一ストレージに配置（約 45% 安）
  # !! 作成後の変更不可 !! 変更時は prevent_destroy を外して destroy → apply → S3 復元が必要
  # !! efs_storage_class="regional"（既定）では null = 複数 AZ のリージョン冗長を維持 !!
  availability_zone_name = var.efs_storage_class == "one_zone" ? data.aws_subnet.efs_primary.availability_zone : null

  # 30 日間アクセスのないファイルを EFS-IA（低頻度アクセス）ストレージに移行（約 90% 安い）。
  # 次回 ECS タスク起動時に読み出されると自動で標準ストレージへ戻る（AFTER_1_ACCESS）ため
  # 稼働中のゲームプレイへの影響はない。データが小さい（数MB）場合はコスト差も小さい。
  # AWS API 上、transition_to_ia と transition_to_primary_storage_class は
  # 同一 lifecycle_policy ブロックに混在させると "malformed" エラーになるため分離する。
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  lifecycle_policy {
    transition_to_primary_storage_class = "AFTER_1_ACCESS"
  }

  # Regional ファイルシステムのみ: 90 日アクセスのないファイルを EFS Archive へ移行
  # IA（~$0.027/GB月）よりさらに安い Archive（~$0.008/GB月）。長期間遊ばないゲームのコスト逓減に有効。
  # !! One Zone では Archive 非対応のため efs_storage_class="one_zone" 時はスキップ !!
  # !! apply 時に "InvalidParameter" が出た場合はスループットモードを Elastic に変更すること !!
  dynamic "lifecycle_policy" {
    for_each = var.efs_storage_class == "regional" ? [1] : []
    content {
      transition_to_archive = "AFTER_90_DAYS"
    }
  }

  lifecycle {
    # terraform destroy / apply による誤削除を防止する
    # EFS を削除したい場合は一時的にこのブロックを外して apply すること
    prevent_destroy = true
  }

  tags = {
    Name = "${local.name_prefix}-efs"
  }
}

# マウントターゲット（各サブネットに1つずつ）
# ECS タスクはこのターゲット経由で EFS に接続する
# regional: 全パブリックサブネット（通常 2 AZ = 2 個）/ one_zone: 単一サブネット（1 個）
resource "aws_efs_mount_target" "main" {
  count = length(local.efs_subnets)

  file_system_id  = aws_efs_file_system.main.id
  subnet_id       = local.efs_subnets[count.index]
  security_groups = [aws_security_group.efs.id]
}

# EFS アクセスポイント
# posix_user で uid/gid を強制し、root_directory で専用ディレクトリを自動作成する
resource "aws_efs_access_point" "main" {
  file_system_id = aws_efs_file_system.main.id

  # コンテナプロセスが使う uid/gid を 1000:1000 に固定
  posix_user {
    uid = 1000
    gid = 1000
  }

  # EFS 内にゲーム専用ディレクトリを作成（存在しない場合は自動生成）
  root_directory {
    path = local.save_dir
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = {
    Name = "${local.name_prefix}-ap"
  }
}
