"""
clients.py - boto3 クライアントの単一インスタンスを保持する

index.py と commands/ 配下の各モジュールがここから import して共有する。
"""
import boto3

from aws_clients import client as _aws_client

ecs = _aws_client("ecs")
ec2 = _aws_client("ec2")
ssm = _aws_client("ssm")

# Cost Explorer / Budgets はグローバルサービスのため us-east-1 固定
# AWS_REGION（ap-northeast-1）を流用すると EndpointResolutionError になる
ce = boto3.client("ce", region_name="us-east-1")
budgets = boto3.client("budgets", region_name="us-east-1")
sts_client = boto3.client("sts")

# /update コマンド: game-stack の auto_update Worker Lambda を非同期 invoke するために使用
# lambda_handler の自己非同期 invoke（deferred ワーカー起動）にも使用
lambda_client = _aws_client("lambda")
