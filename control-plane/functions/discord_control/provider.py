"""
provider.py - メッセージングツール プロバイダー抽象（受信側）

スラッシュコマンド処理 Lambda のツール固有プロトコル（署名検証・ペイロード解析・
応答フォーマット）を隔離する。index.py はこのモジュールだけを介してツールと通信する。

現在の実装: Discord のみ。

他ツール（例: Slack）への差し替え手順:
  1. このファイルに SlackProvider クラスを実装する
       - verify(): HMAC-SHA256 署名 (X-Slack-Signature / X-Slack-Request-Timestamp)
       - parse():  application/x-www-form-urlencoded の slash-command ペイロード解析
       - message(): response_url への POST または JSON 即時応答
       - autocomplete(): 今のところ Slack に同等機能なし（空 choices を返す等）
  2. get_provider() に "slack" の分岐を追加する
  3. Lambda 環境変数 MESSAGING_PROVIDER=slack を設定する
       （Terraform: control-plane/main.tf → aws_lambda_function.discord_control の env）
  4. Slack アプリを作成してスラッシュコマンドを登録し、
     X-Slack-Signing-Secret を DISCORD_PUBLIC_KEY の代わりに設定する

送信側（起動 IP 通知・コストアラート Webhook）の差し替えは
game-stack/functions/_shared/notifier.py を参照。
"""

import json
import logging
import os
from dataclasses import dataclass, field

import ed25519

logger = logging.getLogger()


@dataclass
class Request:
    """ツール非依存のリクエスト表現"""
    kind: str                                    # "ping" | "command" | "autocomplete" | "unknown"
    command: str = ""                            # コマンド名（kind=="command"/"autocomplete" 時）
    options: dict = field(default_factory=dict)  # コマンドオプション {name: value}
    user_id: str = ""                            # 操作者 ID
    focused: str = ""                            # autocomplete 時の入力中文字列


class DiscordProvider:
    """
    Discord Interactions API プロバイダー。

    署名検証: Ed25519
      ヘッダ:  x-signature-ed25519, x-signature-timestamp
      署名対象: timestamp + raw_body (UTF-8 bytes)

    Interaction types（受信）:
      1 = PING, 2 = APPLICATION_COMMAND, 4 = APPLICATION_COMMAND_AUTOCOMPLETE

    Response types（送信）:
      1 = PONG
      4 = CHANNEL_MESSAGE_WITH_SOURCE  (flag 64 = EPHEMERAL)
      8 = APPLICATION_COMMAND_AUTOCOMPLETE_RESULT  (最大 25 choices)
    """

    def __init__(self) -> None:
        self._public_key: str = os.environ["DISCORD_PUBLIC_KEY"]

    def verify(self, headers: dict, raw_body: str) -> bool:
        """Ed25519 署名を検証する（失敗 → 401）"""
        sig_hex   = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        msg = (timestamp + raw_body).encode("utf-8")
        logger.info("署名検証: sig=%s ts=%s body_len=%d pk=%s",
                    sig_hex[:16], timestamp, len(raw_body), self._public_key[:16])
        return ed25519.verify(self._public_key, msg, sig_hex)

    def parse(self, headers: dict, body: dict) -> "Request":
        """Discord interaction body を汎用 Request に変換する"""
        interaction_type = body.get("type")

        if interaction_type == 1:
            return Request(kind="ping")

        data    = body.get("data", {})
        command = data.get("name", "")
        options = {o["name"]: o["value"] for o in data.get("options", [])}

        user    = (body.get("member") or {}).get("user") or body.get("user") or {}
        user_id = user.get("id", "")

        if interaction_type == 4:
            # オートコンプリート: focused==True のオプションの部分入力値を取得
            focused = ""
            for opt in data.get("options", []):
                if opt.get("focused"):
                    focused = opt.get("value", "")
                    break
            return Request(
                kind="autocomplete",
                command=command,
                options=options,
                user_id=user_id,
                focused=focused,
            )

        if interaction_type == 2:
            return Request(kind="command", command=command, options=options, user_id=user_id)

        return Request(kind="unknown")

    def ping_response(self) -> dict:
        """Discord PONG レスポンス（Interactions Endpoint URL 登録時の疎通確認）"""
        return _json_response({"type": 1})

    def message(self, text: str, ephemeral: bool = True) -> dict:
        """
        Discord メッセージ応答（CHANNEL_MESSAGE_WITH_SOURCE, type 4）。
        ephemeral=True の場合はコマンド実行者のみに表示される。
        2000 文字を超える場合は末尾を切り詰める。
        """
        if len(text) > 1990:
            text = text[:1990] + "\n...（省略）"
        return _json_response({
            "type": 4,
            "data": {
                "content": text,
                "flags": 64 if ephemeral else 0,  # 64 = EPHEMERAL
            },
        })

    def autocomplete(self, choices: list) -> dict:
        """
        Discord オートコンプリート応答（APPLICATION_COMMAND_AUTOCOMPLETE_RESULT, type 8）。
        choices は文字列リスト。Discord 上限 25 件。
        """
        choice_objects = [{"name": g, "value": g} for g in choices[:25]]
        return _json_response({"type": 8, "data": {"choices": choice_objects}})


def get_provider():
    """
    環境変数 MESSAGING_PROVIDER（既定: "discord"）でプロバイダーを選択して返す。
    未対応の値の場合は NotImplementedError を送出する。
    """
    provider_name = os.environ.get("MESSAGING_PROVIDER", "discord").lower()
    if provider_name == "discord":
        return DiscordProvider()
    # --- 他ツールへの拡張点 ---
    # elif provider_name == "slack":
    #     return SlackProvider()
    raise NotImplementedError(
        f"未対応の MESSAGING_PROVIDER: '{provider_name}'. "
        "provider.py の get_provider() に実装を追加してください。"
    )


def _json_response(body: dict, status_code: int = 200) -> dict:
    """Lambda Function URL / API Gateway 形式のレスポンスを生成する"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }
