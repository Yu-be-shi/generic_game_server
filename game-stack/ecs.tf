# ============================================================
# ecs.tf - CloudWatch Logs / ECS Cluster / TaskDef / Service
# ============================================================

# -----------------------------------------------------------
# CloudWatch Logs（ECS タスクのログ保存先）
# -----------------------------------------------------------

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = 7 # 7 日で自動削除してログコストを抑制

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
    # Game タグ: control-plane の Discord ボットがこのタグでゲームを発見する
    Game = var.game_name
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
    {
      name      = "monitor"
      image     = "amazonlinux:2023"
      essential = false

      # scripts/auto_shutdown.sh の内容をインラインで埋め込む（ECR / Dockerfile 不要）
      command = ["sh", "-c", file("${path.module}/scripts/auto_shutdown.sh")]

      # Terraform から監視設定を環境変数として注入
      environment = [
        { name = "CLUSTER_NAME", value = local.cluster_name },
        { name = "SERVICE_NAME", value = local.service_name },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "MONITOR_PORT", value = tostring(var.monitor_port) },
        { name = "MONITOR_PROTOCOL", value = var.monitor_protocol },
        { name = "IDLE_MINUTES", value = tostring(var.idle_timeout_minutes) },
        { name = "CHECK_INTERVAL", value = "60" },
      ]

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
    Game = var.game_name
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
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.game.id]
    assign_public_ip = true # NAT/ALB なしでインターネット通信（固定費ゼロ）
  }

  # EFS マウントターゲットが完全に準備できてからサービスを起動する
  depends_on = [aws_efs_mount_target.main]

  lifecycle {
    # サイドカーが desired_count=0 にした後、次の terraform apply で
    # 自動で 1 に戻ってしまうことを防ぐ
    ignore_changes = [desired_count]
  }

  tags = {
    Name = local.service_name
    Game = var.game_name
  }
}
