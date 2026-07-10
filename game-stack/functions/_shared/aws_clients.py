"""
aws_clients.py - リージョン解決・boto3 クライアント生成ユーティリティ（game-stack 共有）

control-plane/functions/discord_control/aws_clients.py に並行実装がある
（region 解決ロジックが異なる独立ファイルのため、修正時は両方を確認すること）。
"""

import os

import boto3

DEFAULT_REGION = "ap-northeast-1"


def region() -> str:
    """Lambda ランタイムが注入する AWS_REGION を返す（未設定時は DEFAULT_REGION）。"""
    return os.environ.get("AWS_REGION", DEFAULT_REGION)


def client(service_name: str, region_name: str = None):
    """region_name 省略時は region() の値で boto3 クライアントを生成する。"""
    return boto3.client(service_name, region_name=region_name or region())
