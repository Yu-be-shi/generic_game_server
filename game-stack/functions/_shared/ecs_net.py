"""
ecs_net.py - ECS RUNNING タスクのパブリック IP 取得ヘルパー（game-stack 共有）

control-plane/functions/discord_control/ecs_net.py に同一内容の複製がある
（Terraform root module が別のため import 不可。修正時は両方を同期させること）。
"""


def get_running_task_public_ip(ecs_client, ec2_client, cluster_arn: str):
    """
    実行中タスクの (public_ip, task_def_arn, task_arn) を返す。
    RUNNING タスクが無い/ENIが無い/パブリックIPが無い場合は該当要素を None にする。
    describe_tasks/describe_network_interfaces 呼び出し自体の例外はそのまま送出する
    （呼び出し元で try/except すること）。
    """
    task_arns = ecs_client.list_tasks(cluster=cluster_arn, desiredStatus="RUNNING")["taskArns"]
    if not task_arns:
        return None, None, None

    tasks = ecs_client.describe_tasks(cluster=cluster_arn, tasks=task_arns[:1])["tasks"]
    if not tasks:
        return None, None, None

    task = tasks[0]
    task_def_arn = task.get("taskDefinitionArn")
    task_arn = task.get("taskArn")

    # attachments から ENI ID を探す。
    # type は "ElasticNetworkInterface" または "eni"（API バージョンにより異なる）
    eni_id = None
    for attachment in task.get("attachments", []):
        if attachment.get("type") not in ("ElasticNetworkInterface", "eni"):
            continue
        for detail in attachment.get("details", []):
            if detail.get("name") == "networkInterfaceId":
                eni_id = detail.get("value")
                break
        if eni_id:
            break

    if not eni_id:
        return None, task_def_arn, task_arn

    interfaces = ec2_client.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"]
    if not interfaces:
        return None, task_def_arn, task_arn

    return interfaces[0].get("Association", {}).get("PublicIp"), task_def_arn, task_arn
