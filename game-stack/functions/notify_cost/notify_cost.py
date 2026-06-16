"""
notify_cost.py - AWS Budgets のコストアラートを Discord に通知する Lambda 関数

トリガー: Amazon SNS（AWS Budgets → SNS → Lambda）
処理:
  SNS メッセージ（Budgets が送った通知テキスト）を Discord Webhook に転送する

依存ライブラリ: urllib（標準ライブラリ）のみ
"""

import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform の lambda.tf から注入）
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    logger.info("受信イベント: %s", json.dumps(event, ensure_ascii=False))

    for record in event.get("Records", []):
        try:
            sns = record.get("Sns", {})
            subject = sns.get("Subject") or "💸 AWS コストアラート"
            raw_message = sns.get("Message", "")

            # Budgets からの通知は JSON の場合とプレーンテキストの場合がある
            message_text = parse_budgets_message(raw_message)

            discord_content = f"💸 **{subject}**\n```\n{message_text}\n```"
            send_discord_message(discord_content)
            logger.info("Discord 通知送信完了")

        except Exception:
            logger.exception("レコードの処理中にエラーが発生しました: %s", record)


def parse_budgets_message(raw_message: str) -> str:
    """
    Budgets の SNS メッセージをパースして読みやすい文字列を返す。
    JSON 形式の場合は主要フィールドを抽出し、そうでなければそのまま返す。
    """
    try:
        data = json.loads(raw_message)
        # Budgets SNS メッセージが JSON オブジェクトの場合、主要情報を整形
        lines = []
        if "budgetName" in data:
            lines.append(f"予算名: {data['budgetName']}")
        if "budgetType" in data:
            lines.append(f"予算種別: {data['budgetType']}")
        if "budgetLimit" in data:
            amount = data["budgetLimit"].get("amount", "?")
            unit = data["budgetLimit"].get("unit", "USD")
            lines.append(f"予算上限: {amount} {unit}")
        if "calculatedSpend" in data:
            actual = data["calculatedSpend"].get("actualSpend", {})
            amount = actual.get("amount", "?")
            unit = actual.get("unit", "USD")
            lines.append(f"実績コスト: {amount} {unit}")
        if "notificationType" in data:
            lines.append(f"通知種別: {data['notificationType']}")

        return "\n".join(lines) if lines else raw_message
    except (json.JSONDecodeError, KeyError, TypeError):
        # JSON でない場合（プレーンテキスト）はそのまま返す
        return raw_message


def send_discord_message(content: str) -> None:
    """Discord Webhook に POST リクエストを送信する"""
    # Discord のメッセージ上限は 2000 文字
    if len(content) > 1990:
        content = content[:1990] + "\n...（省略）"

    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # デフォルトの Python-urllib UA は Cloudflare (Discord) に 403/1010 でブロックされるため
            # 明示的に User-Agent を指定する
            "User-Agent": "GameServerBot (https://github.com/yu-be-shi, 1.0)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Discord API レスポンス: HTTP %d", resp.status)
    except urllib.error.HTTPError as e:
        logger.error("Discord API エラー: HTTP %d - %s", e.code, e.read().decode())
        raise
