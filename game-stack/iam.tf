# ============================================================
# iam.tf - ECS タスク用 IAM ロール
# ============================================================

# -----------------------------------------------------------
# ① タスク実行ロール
#    Fargate がコンテナイメージ pull・CloudWatch Logs への書き込みに使用する。
#    コンテナコードからは使えない（タスクロールと別物）。
# -----------------------------------------------------------

resource "aws_iam_role" "task_execution" {
  name = "${local.name_prefix}-task-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-task-exec"
  }
}

# ECR pull と CloudWatch Logs への書き込みを許可する AWS 管理ポリシー
resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# -----------------------------------------------------------
# ② タスクロール（コンテナ内のアプリケーションが使用する）
#    ゲームコンテナ・監視サイドカーが AWS API を呼び出す際に使う。
# -----------------------------------------------------------

resource "aws_iam_role" "task" {
  name = "${local.name_prefix}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${local.name_prefix}-task"
  }
}

resource "aws_iam_role_policy" "task_permissions" {
  name = "${local.name_prefix}-task-permissions"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # 自動シャットダウン用
        # サイドカーコンテナ(auto_shutdown.sh)がこの権限で
        # `aws ecs update-service --desired-count 0` を実行してタスクを自己停止する。
        #
        # !! 循環依存の回避 !!
        # aws_ecs_service → aws_ecs_task_definition → aws_iam_role という依存を
        # 作ると循環するため、サービス ARN を locals から文字列で直接構築する
        Sid    = "EcsSelfStop"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices"
        ]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.cluster_name}/${local.service_name}"
      },
      {
        # EFS マウント権限（authorization_config.iam = "ENABLED" の場合に必須）
        Sid    = "EfsAccess"
        Effect = "Allow"
        Action = [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess"
        ]
        Resource = aws_efs_file_system.main.arn
      }
    ]
  })
}
