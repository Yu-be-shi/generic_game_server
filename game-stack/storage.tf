# ============================================================
# storage.tf - EFS（セーブデータの永続化）
# ============================================================
# EFS アクセスポイントを使い uid/gid を固定することで
# コンテナ起動時の「Permission denied」エラーを防止する。

# EFS ファイルシステム
resource "aws_efs_file_system" "main" {
  encrypted = true # 保存データを暗号化

  tags = {
    Name = "${local.name_prefix}-efs"
    Game = var.game_name
  }
}

# マウントターゲット（各パブリックサブネットに1つずつ）
# ECS タスクはこのターゲット経由で EFS に接続する
resource "aws_efs_mount_target" "main" {
  count = 2

  file_system_id  = aws_efs_file_system.main.id
  subnet_id       = aws_subnet.public[count.index].id
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
    path = "/${var.game_name}"
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
