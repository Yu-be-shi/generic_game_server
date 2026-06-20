"""
docs/architecture/diagram.py — Generic Game Server 構成図 (diagram-as-code)

再生成コマンド（プロジェクトルートから）:
    docs/architecture/.venv/bin/python docs/architecture/diagram.py

→ docs/architecture/architecture.svg / .png が上書き生成される。

Terraform リソースを追加・変更したときはこのスクリプトも合わせて更新すること。
依存: graphviz (dot) + venv 内の diagrams==0.23.4
セットアップ: docs/architecture/README.md を参照。
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR, ECS, Fargate, Lambda
from diagrams.aws.cost import Budgets, CostExplorer
from diagrams.aws.integration import SNS, SQS, Eventbridge
from diagrams.aws.management import SystemsManagerParameterStore
from diagrams.aws.network import (
    APIGateway,
    Endpoint,
    InternetGateway,
    PublicSubnet,
)
from diagrams.aws.storage import EFS, S3
from diagrams.onprem.client import Client, Users

# グラフ全体のレイアウト調整
graph_attr = {
    "fontsize": "13",
    "splines": "ortho",
    "pad": "0.8",
    "nodesep": "0.7",
    "ranksep": "1.2",
    "bgcolor": "white",
}

node_attr = {
    "fontsize": "10",
}

with Diagram(
    "Generic Game Server",
    filename="docs/architecture/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
    node_attr=node_attr,
):
    # ────────────────────────────────────────────────────────────
    # 外部（AWS 外）
    # ────────────────────────────────────────────────────────────
    discord = Users("Discord\n(slash commands)")
    players = Client("Players\n(game clients)")

    # ────────────────────────────────────────────────────────────
    # control-plane スタック（アカウントに 1 度だけデプロイ）
    # ────────────────────────────────────────────────────────────
    with Cluster("control-plane  (deploy once)"):
        apigw = APIGateway("API Gateway v2\nHTTP API  POST /")
        ctrl_lambda = Lambda(
            "discord_control\npython3.12 / arm64\ned25519 verify"
        )
        ecr = ECR("ECR\nggs-monitor image")
        tf_state = S3("S3\ntfstate")

        with Cluster("Shared VPC  ggs-shared-vpc  10.0.0.0/16"):
            igw = InternetGateway("Internet\nGateway")
            subnet_a = PublicSubnet("Public Subnet\nAZ-a  10.0.0.0/24")
            subnet_b = PublicSubnet("Public Subnet\nAZ-b  10.0.1.0/24")
            s3_ep = Endpoint("S3 Gateway\nEndpoint\n(no NAT)")

    # ────────────────────────────────────────────────────────────
    # game-stack スタック（ゲームごと: terraform workspace）
    # ────────────────────────────────────────────────────────────
    with Cluster("game-stack  (per workspace, e.g. palworld)"):

        ssm = SystemsManagerParameterStore(
            "/ggs/<prefix>/*\nready / players\nmaintenance / buildid"
        )

        # ECS タスク（2 コンテナ）
        with Cluster("ECS Fargate Task"):
            ecs_svc = ECS("ECS Service\ndesired_count 0 ↔ 1")
            game_c = Fargate("game\n(essential=true)")
            monitor_c = Fargate("monitor sidecar\n(essential=false)\nauto_shutdown.sh")

        efs = EFS("EFS\nセーブデータ\n(prevent_destroy)")
        s3_bk = S3("S3 Backup\n(Glacier IR 階層化)")

        # Lambda 群（全て arm64 / python3.12）
        notify_ip_fn = Lambda("notify_ip")
        auto_update_fn = Lambda("auto_update")
        cost_guard_fn = Lambda("cost_guard")
        backup_efs_fn = Lambda("backup_efs")

        # EventBridge ルール（4 本）
        with Cluster("EventBridge Rules"):
            eb_ready = Eventbridge("server_ready\nSSM /ready 変化")
            eb_stopped = Eventbridge("ecs_stopped\nECS STOPPED")
            eb_cg = Eventbridge("cost_guard\nrate(1 hour)")
            eb_bk = Eventbridge("backup_schedule\nrate(24 hours)")

        # コストアラート系
        with Cluster("Cost Alerting"):
            budgets = Budgets("AWS Budgets\n月額 4 段階\n(20/50/80/100%)")
            cost_exp = CostExplorer("Cost Explorer")
            sns_cost = SNS("SNS\ncost_alert")
            notify_cost_fn = Lambda("notify_cost")
            dlq = SQS("SQS DLQ")

    # ════════════════════════════════════════════════════════════
    # エッジ（7 主要データフロー）
    # ════════════════════════════════════════════════════════════

    # ① 制御フロー: Discord → API GW → discord_control → game-stack 各種
    discord >> apigw >> ctrl_lambda
    ctrl_lambda >> Edge(label="/start /stop") >> ecs_svc
    ctrl_lambda >> Edge(label="/status") >> ssm
    ctrl_lambda >> Edge(label="/cost") >> cost_exp
    ctrl_lambda >> Edge(label="/update") >> auto_update_fn

    # ② 起動通知: monitor → SSM ready=1 → EventBridge → notify_ip → Discord
    monitor_c >> Edge(label="ready=1\nbuildid 書込") >> ssm
    ssm >> eb_ready >> notify_ip_fn
    notify_ip_fn >> Edge(label="IP 通知") >> discord

    # ③ 停止通知: ECS STOPPED → EventBridge → notify_ip → Discord
    ecs_svc >> eb_stopped >> notify_ip_fn

    # ④ アイドル自動停止（モニターサイドカー）
    monitor_c >> Edge(label="idle → desired=0\n+ EFS→S3 同期前") >> ecs_svc
    monitor_c >> s3_bk

    # ⑤ ECS タスク内ストレージアクセス
    game_c >> Edge(label="read/write") >> efs
    monitor_c >> Edge(label="read-only") >> efs

    # ⑥ 定期バックアップ（EventBridge → backup_efs Lambda → EFS→S3）
    eb_bk >> backup_efs_fn
    backup_efs_fn >> Edge(label="EFS→S3 sync") >> s3_bk

    # ⑦ 暴走停止（コストガード）
    eb_cg >> cost_guard_fn
    cost_guard_fn >> Edge(label="StopTask\ndesired=0") >> ecs_svc

    # ⑧ コスト通知: Budgets → SNS → notify_cost → Discord（失敗→DLQ）
    budgets >> sns_cost >> notify_cost_fn
    notify_cost_fn >> Edge(label="コスト通知") >> discord
    notify_cost_fn >> Edge(label="失敗時") >> dlq

    # ⑨ プレイヤーはゲームポートへ直接（パブリック IP・ALB なし）
    players >> Edge(label="game port\n(direct public IP)") >> game_c

    # ⑩ IGW / S3 Endpoint（VPC ネットワーク）
    game_c >> Edge(style="dashed") >> igw
    s3_ep >> Edge(style="dashed", label="S3 traffic\n(NAT なし)") >> s3_bk

    # ⑪ サイドカーイメージ（ECR pull）
    ecr >> Edge(style="dashed", label="pull") >> monitor_c

    # ⑫ auto_update: RunTask でワンオフ更新タスク
    auto_update_fn >> Edge(label="RunTask\nUPDATE_ON_BOOT") >> ecs_svc

    # ⑬ notify_ip: IP 取得のため ECS DescribeTasks / EC2 ENI
    notify_ip_fn >> Edge(label="DescribeTasks\nENI→public IP") >> ecs_svc
