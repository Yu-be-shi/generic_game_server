"""
notifier.py - メッセージング Webhook 送信ユーティリティ（共有モジュール）

game-stack の複数 Lambda（notify_ip, notify_cost）から共用される。
MESSAGING_PROVIDER 環境変数（既定: "discord"）でプロバイダーを選択する。

Slack への差し替え:
  MESSAGING_PROVIDER=slack + MESSAGING_WEBHOOK_URL（または DISCORD_WEBHOOK_URL）
  に Slack Incoming Webhook URL を設定するだけで切り替わる。

受信側（Discord スラッシュコマンド処理）の差し替えは
control-plane/functions/discord_control/provider.py を参照。
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger()

# Discord のメッセージ上限は 2000 文字。Slack は 40000 文字まで許容するが、
# 実用的な上限として 4000 文字で切り詰める。
DISCORD_MESSAGE_LIMIT = 1990
SLACK_MESSAGE_LIMIT = 3990


def _truncate(text: str, limit: int) -> str:
    """limit 文字を超える場合は切り詰めて省略記号を付与する。"""
    if len(text) > limit:
        return text[:limit] + "\n...（省略）"
    return text


# Webhook URL: MESSAGING_WEBHOOK_URL を優先し、なければ後方互換で DISCORD_WEBHOOK_URL を参照
_WEBHOOK_URL: str = (
    os.environ.get("MESSAGING_WEBHOOK_URL")
    or os.environ.get("DISCORD_WEBHOOK_URL", "")
)
_PROVIDER: str = os.environ.get("MESSAGING_PROVIDER", "discord").lower()


def send_message(text: str) -> None:
    """
    設定されたプロバイダーの Webhook へテキストメッセージを送信する。

    Args:
        text: 送信するメッセージ本文（絵文字・Markdown 可）
    Raises:
        ValueError: Webhook URL が未設定
        NotImplementedError: 未対応の MESSAGING_PROVIDER
        urllib.error.HTTPError: Webhook への POST が失敗
    """
    if not _WEBHOOK_URL:
        logger.error(
            "Webhook URL が設定されていません "
            "（MESSAGING_WEBHOOK_URL または DISCORD_WEBHOOK_URL を確認してください）"
        )
        raise ValueError("Webhook URL が未設定です")

    if _PROVIDER == "discord":
        _send_discord(text)
    elif _PROVIDER == "slack":
        _send_slack(text)
    else:
        raise NotImplementedError(
            f"未対応の MESSAGING_PROVIDER: '{_PROVIDER}'. "
            "notifier.py に実装を追加してください。"
        )


def send_message_safe(text: str) -> bool:
    """
    send_message() の fire-and-forget 版。
    通知の送信失敗（Webhook URL未設定・HTTPエラー等）はログに残すだけで、
    呼び出し元の本処理を止めない用途に使う。

    Args:
        text: 送信するメッセージ本文（絵文字・Markdown 可）
    Returns:
        送信に成功したら True、失敗したら False
    """
    try:
        send_message(text)
        return True
    except Exception:
        logger.warning("通知送信失敗（継続）: %s", text[:80], exc_info=True)
        return False


def _send_discord(content: str) -> None:
    """Discord Webhook に POST する（Cloudflare 403 対策 User-Agent 付き）"""
    content = _truncate(content, DISCORD_MESSAGE_LIMIT)

    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        _WEBHOOK_URL,
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
            logger.info("Webhook 送信完了 (discord): HTTP %d", resp.status)
    except urllib.error.HTTPError as e:
        # レスポンスボディは CloudWatch へのリクエスト内容漏洩を防ぐため記録しない
        logger.error("Webhook エラー (discord): HTTP %d %s", e.code, e.reason)
        raise


def _send_slack(text: str) -> None:
    """
    Slack Incoming Webhook に POST する（参照実装）。

    Incoming Webhook URL は Slack アプリ設定から取得し、
    MESSAGING_WEBHOOK_URL 環境変数に設定する。
    メッセージは Slack のデフォルトフォーマット（markdown 非対応部分あり）。
    """
    text = _truncate(text, SLACK_MESSAGE_LIMIT)

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        _WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Webhook 送信完了 (slack): HTTP %d", resp.status)
    except urllib.error.HTTPError as e:
        # レスポンスボディは CloudWatch へのリクエスト内容漏洩を防ぐため記録しない
        logger.error("Webhook エラー (slack): HTTP %d %s", e.code, e.reason)
        raise
