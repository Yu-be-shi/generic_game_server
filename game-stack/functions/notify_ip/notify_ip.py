"""
notify_ip.py - ECS タスク起動時にパブリック IP を Discord に通知する Lambda 関数

トリガー: Amazon EventBridge（ECS Task State Change → lastStatus=RUNNING）
処理:
  1. イベントから ECS タスクに紐づく ENI (ElasticNetworkInterface) の ID を取得
  2. EC2 API で ENI のパブリック IP を取得
  3. Discord Webhook に「サーバーが起動しました。IP: XX.XX.XX.XX」を POST

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、urllib（標準ライブラリ）のみ
"""

import json
import logging
import os
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform の lambda.tf から注入）
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
GAME_NAME = os.environ["GAME_NAME"]


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    logger.info("受信イベント: %s", json.dumps(event, ensure_ascii=False))

    try:
        public_ip = get_public_ip_from_event(event)
        if public_ip is None:
            logger.warning("パブリック IP を取得できませんでした。通知をスキップします。")
            return

        message = f"🟢 **{GAME_NAME}** サーバーが起動しました！\nIP アドレス: `{public_ip}`"
        send_discord_message(message)
        logger.info("Discord 通知送信完了: IP=%s", public_ip)

    except Exception:
        # 通知の失敗はログに記録するが、Lambda はエラーで落とさない
        # （ゲームサーバーの起動には影響しない）
        logger.exception("Discord 通知中にエラーが発生しました")


def get_public_ip_from_event(event: dict) -> str | None:
    """
    EventBridge の ECS Task State Change イベントから ENI ID を取得し、
    EC2 API でパブリック IP を引く。

    イベント構造（抜粋）:
      event.detail.attachments[].type == "ElasticNetworkInterface"
        .details[].name == "networkInterfaceId"
        .details[].value == "eni-xxxxxxxx"
    """
    attachments = event.get("detail", {}).get("attachments", [])

    eni_id = None
    for attachment in attachments:
        if attachment.get("type") != "ElasticNetworkInterface":
            continue
        for detail in attachment.get("details", []):
            if detail.get("name") == "networkInterfaceId":
                eni_id = detail.get("value")
                break
        if eni_id:
            break

    if not eni_id:
        logger.warning("ENI ID が見つかりませんでした。attachments: %s", json.dumps(attachments))
        return None

    logger.info("ENI ID: %s", eni_id)

    # ENI からパブリック IP を取得
    ec2 = boto3.client("ec2")
    response = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
    interfaces = response.get("NetworkInterfaces", [])

    if not interfaces:
        logger.warning("ENI %s が見つかりません", eni_id)
        return None

    association = interfaces[0].get("Association", {})
    public_ip = association.get("PublicIp")

    if not public_ip:
        logger.warning("パブリック IP が見つかりません。ENI: %s, Association: %s", eni_id, association)

    return public_ip


def send_discord_message(content: str) -> None:
    """Discord Webhook に POST リクエストを送信する"""
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Discord API レスポンス: HTTP %d", resp.status)
    except urllib.error.HTTPError as e:
        logger.error("Discord API エラー: HTTP %d - %s", e.code, e.read().decode())
        raise
