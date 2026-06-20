"""
docs/architecture/diagram.py -- Generic Game Server architecture diagram (diagram-as-code)

Regenerate (from project root):
    docs/architecture/.venv/bin/python docs/architecture/diagram.py

-> docs/architecture/architecture.png is overwritten.
Update this file whenever Terraform resources change.
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

graph_attr = {
    "fontsize": "13",
    "splines": "curved",
    "pad": "1.0",
    "nodesep": "0.8",
    "ranksep": "1.4",
    "bgcolor": "white",
    "size": "24,18",
    "ratio": "fill",
}

node_attr = {
    "fontsize": "10",
    "fontname": "Helvetica",
}

with Diagram(
    "Generic Game Server",
    filename="docs/architecture/architecture",
    outformat=["svg", "png"],
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    node_attr=node_attr,
):
    # ── External (outside AWS) ──────────────────────────────────
    discord = Users("Discord\n(slash commands)")
    players = Client("Players\n(game clients)")

    # ── control-plane (deployed once per AWS account) ───────────
    with Cluster("control-plane  [deploy once]"):
        apigw = APIGateway("API Gateway v2\nHTTP API  POST /")
        ctrl_lambda = Lambda(
            "discord_control\npython3.12 / arm64\ned25519 verify"
        )
        ecr = ECR("ECR\nggs-monitor image")
        tf_state = S3("S3\ntfstate")

        with Cluster("Shared VPC  10.0.0.0/16"):
            igw = InternetGateway("Internet\nGateway")
            subnet_a = PublicSubnet("Public Subnet\nAZ-a  /24")
            subnet_b = PublicSubnet("Public Subnet\nAZ-b  /24")
            s3_ep = Endpoint("S3 Gateway\nEndpoint (no NAT)")

    # ── game-stack (one Terraform workspace per game) ───────────
    with Cluster("game-stack  [per workspace, e.g. palworld]"):

        ssm = SystemsManagerParameterStore(
            "/ggs/<prefix>/*\nready / players\nmaintenance / buildid"
        )

        with Cluster("ECS Fargate Task"):
            ecs_svc = ECS("ECS Service\ndesired_count  0 <-> 1")
            game_c = Fargate("game container\n[essential=true]")
            monitor_c = Fargate("monitor sidecar\n[essential=false]\nauto_shutdown.sh")

        efs = EFS("EFS\nSave data\n[prevent_destroy]")
        s3_bk = S3("S3 Backup\n[Glacier IR tiering]")

        # Lambda functions (all arm64 / python3.12)
        notify_ip_fn = Lambda("notify_ip")
        auto_update_fn = Lambda("auto_update")
        cost_guard_fn = Lambda("cost_guard")
        backup_efs_fn = Lambda("backup_efs")

        with Cluster("EventBridge Rules"):
            eb_ready = Eventbridge("server_ready\nSSM /ready change")
            eb_stopped = Eventbridge("ecs_stopped\nECS task STOPPED")
            eb_cg = Eventbridge("cost_guard\nrate(1 hour)")
            eb_bk = Eventbridge("backup_schedule\nrate(24 hours)")

        with Cluster("Cost Alerting"):
            budgets = Budgets("AWS Budgets\nmonthly 20/50/80/100%")
            cost_exp = CostExplorer("Cost Explorer")
            sns_cost = SNS("SNS\ncost_alert")
            notify_cost_fn = Lambda("notify_cost")
            dlq = SQS("SQS DLQ")

    # ── Data flows ──────────────────────────────────────────────

    # 1. Control: Discord -> API GW -> discord_control -> game-stack
    discord >> apigw >> ctrl_lambda
    ctrl_lambda >> Edge(label="/start /stop") >> ecs_svc
    ctrl_lambda >> Edge(label="/status") >> ssm
    ctrl_lambda >> Edge(label="/cost") >> cost_exp
    ctrl_lambda >> Edge(label="/update") >> auto_update_fn

    # 2. Ready notification: monitor -> SSM -> EventBridge -> notify_ip -> Discord
    monitor_c >> Edge(label="ready=1 / buildid") >> ssm
    ssm >> eb_ready >> notify_ip_fn
    notify_ip_fn >> Edge(label="IP notify") >> discord

    # 3. Stop notification: ECS STOPPED -> EventBridge -> notify_ip -> Discord
    ecs_svc >> eb_stopped >> notify_ip_fn

    # 4. Idle auto-stop (monitor sidecar)
    monitor_c >> Edge(label="idle -> desired=0\n+ EFS->S3 sync") >> ecs_svc
    monitor_c >> s3_bk

    # 5. Storage access inside ECS task
    game_c >> Edge(label="read/write") >> efs
    monitor_c >> Edge(label="read-only") >> efs

    # 6. Scheduled backup: EventBridge -> backup_efs -> S3
    eb_bk >> backup_efs_fn
    backup_efs_fn >> Edge(label="EFS->S3 sync") >> s3_bk

    # 7. Cost guard (hard stop): EventBridge -> cost_guard -> ECS
    eb_cg >> cost_guard_fn
    cost_guard_fn >> Edge(label="StopTask / desired=0") >> ecs_svc

    # 8. Cost alert: Budgets -> SNS -> notify_cost -> Discord (fail -> DLQ)
    budgets >> sns_cost >> notify_cost_fn
    notify_cost_fn >> Edge(label="cost alert") >> discord
    notify_cost_fn >> Edge(label="on failure") >> dlq

    # 9. Players connect directly (no ALB / no NAT)
    players >> Edge(label="game port\n[direct public IP]") >> game_c

    # 10. VPC network paths (dashed = infrastructure, not data flow)
    game_c >> Edge(style="dashed") >> igw
    s3_ep >> Edge(style="dashed", label="S3 traffic\n[no NAT]") >> s3_bk

    # 11. Monitor image pulled from ECR
    ecr >> Edge(style="dashed", label="image pull") >> monitor_c

    # 12. auto_update: run one-shot update task
    auto_update_fn >> Edge(label="RunTask\nUPDATE_ON_BOOT") >> ecs_svc

    # 13. notify_ip: resolve public IP via ECS DescribeTasks / EC2 ENI
    notify_ip_fn >> Edge(label="DescribeTasks\nENI -> public IP") >> ecs_svc
