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


def _send_discord(content: str) -> None:
    """Discord Webhook に POST する（Cloudflare 403 対策 User-Agent 付き）"""
    # Discord のメッセージ上限は 2000 文字
    if len(content) > 1990:
        content = content[:1990] + "\n...（省略）"

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
        logger.error("Webhook エラー (discord): HTTP %d - %s", e.code, e.read().decode())
        raise


def _send_slack(text: str) -> None:
    """
    Slack Incoming Webhook に POST する（参照実装）。

    Incoming Webhook URL は Slack アプリ設定から取得し、
    MESSAGING_WEBHOOK_URL 環境変数に設定する。
    メッセージは Slack のデフォルトフォーマット（markdown 非対応部分あり）。
    """
    # Slack は 40000 文字まで許容するが、実用的な上限として 4000 文字で切詰
    if len(text) > 3990:
        text = text[:3990] + "\n...（省略）"

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
        logger.error("Webhook エラー (slack): HTTP %d - %s", e.code, e.read().decode())
        raise
