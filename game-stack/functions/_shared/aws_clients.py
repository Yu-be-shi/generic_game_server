"""
aws_clients.py - リージョン解決・boto3 クライアント生成ユーティリティ（全 Lambda 共有・単一ソース）

control-plane/main.tf は archive_file の dynamic "source" でこのファイルを
そのまま discord_control Lambda の zip に取り込む（コピーは存在しない）。
"""

import os

import boto3

DEFAULT_REGION = "ap-northeast-1"


def region() -> str:
    """
    Lambda ランタイムが注入する AWS_REGION を返す。
    GAME_AWS_REGION が設定されていればそちらを優先する
    （discord_control Lambda は control-plane/main.tf で明示的にこれを注入する）。
    """
    return os.environ.get("GAME_AWS_REGION") or os.environ.get("AWS_REGION", DEFAULT_REGION)


def client(service_name: str, region_name: str = None):
    """region_name 省略時は region() の値で boto3 クライアントを生成する。"""
    return boto3.client(service_name, region_name=region_name or region())
