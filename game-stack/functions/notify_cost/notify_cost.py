"""
notify_cost.py - AWS Budgets のコストアラートを通知する Lambda 関数

トリガー: Amazon SNS（AWS Budgets → SNS → Lambda）
処理:
  SNS メッセージ（Budgets が送った通知テキスト）を notifier.send_message_safe() で転送する

依存ライブラリ: urllib（標準ライブラリ）のみ
メッセージング: notifier.py（共有モジュール）経由でツール非依存に送信する
"""

import json
import logging

from notifier import send_block_safe

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    # イベント全文ログは避ける（SNS メッセージにアカウント ID・コスト情報が含まれるため）
    logger.info("受信イベント: Records件数=%d", len(event.get("Records", [])))

    for record in event.get("Records", []):
        # パース失敗と送信失敗を別々に扱う（ログから原因を区別できるようにする）
        try:
            sns         = record.get("Sns", {})
            subject     = sns.get("Subject") or "💸 AWS コストアラート"
            raw_message = sns.get("Message", "")

            # Budgets からの通知は JSON の場合とプレーンテキストの場合がある
            message_text = parse_budgets_message(raw_message)
        except Exception:
            logger.exception("SNSメッセージのパースに失敗しました: %s", record)
            continue

        if send_block_safe(f"💸 **{subject}**", message_text):
            logger.info("コストアラート通知送信完了")


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
            unit   = data["budgetLimit"].get("unit", "USD")
            lines.append(f"予算上限: {amount} {unit}")
        if "calculatedSpend" in data:
            actual = data["calculatedSpend"].get("actualSpend", {})
            amount = actual.get("amount", "?")
            unit   = actual.get("unit", "USD")
            lines.append(f"実績コスト: {amount} {unit}")
        if "notificationType" in data:
            lines.append(f"通知種別: {data['notificationType']}")

        return "\n".join(lines) if lines else raw_message
    except (json.JSONDecodeError, KeyError, TypeError):
        # JSON でない場合（プレーンテキスト）はそのまま返す
        return raw_message
