"""
aws_clients.py - リージョン解決・boto3 クライアント生成ユーティリティ（control-plane 用）

game-stack/functions/_shared/aws_clients.py に並行実装がある
（GAME_AWS_REGION を優先する点がこちらの特徴。修正時は両方を確認すること）。
"""

import os

import boto3

DEFAULT_REGION = "ap-northeast-1"


def region() -> str:
    """Lambda が動いているリージョンより GAME_AWS_REGION を優先する。"""
    return os.environ.get("GAME_AWS_REGION") or os.environ.get("AWS_REGION", DEFAULT_REGION)


def client(service_name: str, region_name: str = None):
    return boto3.client(service_name, region_name=region_name or region())
