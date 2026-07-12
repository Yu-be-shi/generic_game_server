# ============================================================
# ecs.tf - CloudWatch Logs / ECS Cluster / TaskDef / Service
# ============================================================

# -----------------------------------------------------------
# CloudWatch Logs（ECS タスクのログ保存先）
# -----------------------------------------------------------

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${local.name_prefix}-ecs-logs"
  }
}

# -----------------------------------------------------------
# ECS Cluster
# -----------------------------------------------------------

resource "aws_ecs_cluster" "game" {
  name = local.cluster_name

  setting {
    # Container Insights は追加料金が発生するため個人運用では無効化
    name  = "containerInsights"
    value = "disabled"
  }

  tags = {
    Name = local.cluster_name
    # Game タグ: control-plane の Discord ボットがこのタグでゲームを発見する。
    # ここでは明示指定していないが versions.tf の provider "aws" { default_tags } が
    # 全リソースに Game = var.game_name を自動付与するため、最終的なタグは変わらない。
    # StatusParamPrefix タグ: /status コマンドが SSM パラメータを読む際のプレフィックス
    # monitor サイドカーが "${local.ssm_prefix}/ready" 等に書き込む
    StatusParamPrefix = local.ssm_prefix
    # AutoUpdateFunction タグ: /update コマンドが Worker Lambda 名を取得するために使用する
    # discord_control Lambda が cluster タグ経由でこの関数を非同期 invoke する
    AutoUpdateFunction = "${local.name_prefix}-auto-update"
    # BackupFunction タグ: /backup, /restore コマンドが Worker Lambda 名を取得するために使用する
    BackupFunction = "${local.name_prefix}-backup-efs"
  }
}

# -----------------------------------------------------------
# ECS タスク定義
# -----------------------------------------------------------

resource "aws_ecs_task_definition" "game" {
  family                   = local.name_prefix
  requires_compatibilities = ["FARGATE"] # Spot は使わず通常 Fargate で安定稼働
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # CPU アーキテクチャ設定（既定: X86_64、Graviton 化は task_cpu_architecture="ARM64" で opt-in）
  runtime_platform {
    cpu_architecture        = var.task_cpu_architecture
    operating_system_family = "LINUX"
  }

  # EFS ボリューム（セーブデータ永続化）
  # transit_encryption=ENABLED でネットワーク転送を暗号化
  # iam=ENABLED でタスクロールを使ってアクセス制御
  volume {
    name = "efs-data"

    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.main.id
      root_directory          = "/" # アクセスポイントが root を上書きする
      transit_encryption      = "ENABLED"
      transit_encryption_port = 2049

      authorization_config {
        access_point_id = aws_efs_access_point.main.id
        iam             = "ENABLED"
      }
    }
  }

  # ----------------------------------------------------------------
  # コンテナ定義（2コンテナ構成）
  # [1] ゲームコンテナ（メイン）
  # [2] 監視サイドカー（自動シャットダウン用・追加コストゼロ）
  # ----------------------------------------------------------------
  container_definitions = jsonencode([

    # [1] ゲームコンテナ
    {
      name      = "game"
      image     = var.docker_image
      essential = true

      # awsvpc モードでは containerPort = hostPort
      portMappings = [
        for p in var.game_ports : {
          containerPort = p.port
          protocol      = lower(p.protocol)
        }
      ]

      # EFS マウント（セーブデータの永続化）
      mountPoints = [{
        sourceVolume  = "efs-data"
        containerPath = var.efs_mount_path
        readOnly      = false
      }]

      # ゲーム固有の環境変数
      environment = [
        for k, v in var.environment_variables : { name = k, value = v }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "game"
        }
      }
    },

    # [2] 監視サイドカーコンテナ
    #
    # awsvpc モードでゲームコンテナとネットワーク名前空間を共有するため、
    # ss コマンドでゲームポートへの接続数を透過的に監視できる。
    # essential=false のため、サイドカーが停止してもゲームコンテナは継続する。
    # Fargate はタスク単位課金のため追加コストはゼロ。
    #
    # EFS もマウントし、サーバー停止直前に S3 へセーブデータを同期する。
    {
      name      = "monitor"
      image     = var.monitor_image # 既定: "amazonlinux:2023"（事前ビルドイメージで置き換え可）
      essential = false

      # scripts/auto_shutdown.sh の内容をインラインで埋め込む（イメージに依存しない注入方式）
      # monitor_image に依存パッケージ入りの事前ビルドイメージを使う場合も dnf はスキップされる
      command = ["sh", "-c", file("${path.module}/scripts/auto_shutdown.sh")]

      # Terraform から監視設定・バックアップ設定を環境変数として注入
      environment = [
        { name = "CLUSTER_NAME", value = local.cluster_name },
        { name = "SERVICE_NAME", value = local.service_name },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "MONITOR_PORT", value = tostring(var.monitor_port) },
        { name = "MONITOR_PROTOCOL", value = var.monitor_protocol },
        { name = "MONITOR_METHOD", value = var.monitor_method },
        { name = "REST_API_PORT", value = tostring(var.rest_api_port) },
        { name = "REST_API_PASSWORD", value = lookup(var.environment_variables, "ADMIN_PASSWORD", "") },
        { name = "IDLE_MINUTES", value = tostring(var.idle_timeout_minutes) },
        { name = "CHECK_INTERVAL", value = "60" },
        { name = "READY_POLL_INTERVAL", value = "5" }, # フェーズA: ポート待ち受け検知の粒度（秒）
        # 停止前バックアップ用（auto_shutdown.sh が参照する）
        { name = "BACKUP_BUCKET", value = aws_s3_bucket.backup.id },
        { name = "BACKUP_PREFIX", value = local.backup_prefix },
        { name = "EFS_MOUNT_PATH", value = var.efs_mount_path },
        # SSM ステータス連携（Discord 通知・/status コマンド用）
        { name = "READY_PARAM", value = "${local.ssm_prefix}/ready" },
        { name = "PLAYERS_PARAM", value = "${local.ssm_prefix}/players" },
      ]

      # セーブデータを読み取るために EFS をマウント（読み取り専用）
      mountPoints = [{
        sourceVolume  = "efs-data"
        containerPath = var.efs_mount_path
        readOnly      = true
      }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "monitor"
        }
      }
    }
  ])

  tags = {
    Name = local.name_prefix
  }
}

# -----------------------------------------------------------
# ECS Service
# -----------------------------------------------------------

resource "aws_ecs_service" "game" {
  name            = local.service_name
  cluster         = aws_ecs_cluster.game.id
  task_definition = aws_ecs_task_definition.game.arn
  launch_type     = "FARGATE" # Spot は使わず安定稼働を優先
  desired_count   = var.desired_count

  # EFS を Fargate で使うにはプラットフォームバージョン 1.4.0 以上が必要
  platform_version = "LATEST"

  network_configuration {
    # one_zone 選択時は EFS と同一 AZ のサブネットに固定（異 AZ だとマウント不可）
    # regional（既定）は全パブリックサブネット（複数 AZ）= 従来どおり
    subnets          = local.efs_subnets
    security_groups  = [aws_security_group.game.id]
    assign_public_ip = true # NAT/ALB なしでインターネット通信（固定費ゼロ）
  }

  # EFS マウントターゲットが完全に準備できてからサービスを起動する
  depends_on = [aws_efs_mount_target.main]

  lifecycle {
    ignore_changes = [
      # サイドカーが desired_count=0 にした後、次の terraform apply で
      # 自動で 1 に戻ってしまうことを防ぐ
      desired_count,
      # タスク定義が更新されても稼働中タスクを強制停止しない
      # コンテナ定義の変更を反映する場合は停止後に手動で
      # aws ecs update-service --force-new-deployment を実行すること
      task_definition,
    ]
  }

  tags = {
    Name = local.service_name
  }
}
