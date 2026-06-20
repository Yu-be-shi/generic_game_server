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
  3. message(): 即時応答（Slack は JSON 即時応答か response_url POST）
     deferred_response(): Slack は「:white_check_mark:」の ack 即時応答
     send_followup(): Slack は response_url への POST
  4. Lambda 環境変数 MESSAGING_PROVIDER=slack を設定する
       （Terraform: control-plane/main.tf → aws_lambda_function.discord_control の env）
  5. Slack アプリを作成してスラッシュコマンドを登録し、
     X-Slack-Signing-Secret を DISCORD_PUBLIC_KEY の代わりに設定する

送信側（起動 IP 通知・コストアラート Webhook）の差し替えは
game-stack/functions/_shared/notifier.py を参照。
"""

import json
import logging
import os
import time
import urllib.request
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
    app_id: str = ""                             # アプリケーション ID（deferred フォローアップ用）
    token: str = ""                              # インタラクション token（deferred フォローアップ用・有効期限 15 分）


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
      5 = DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE  (3 秒以内に返し「考え中…」を表示)
      8 = APPLICATION_COMMAND_AUTOCOMPLETE_RESULT  (最大 25 choices)

    Deferred フロー:
      1. コマンド受信 → type 5 を即返す（3 秒制限を回避）
      2. 重い処理を別 Lambda で非同期実行
      3. PATCH /webhooks/{app_id}/{token}/messages/@original でフォローアップ送信
    """

    def __init__(self) -> None:
        self._public_key: str = os.environ["DISCORD_PUBLIC_KEY"]

    def verify(self, headers: dict, raw_body: str) -> bool:
        """
        Ed25519 署名を検証する（失敗 → 401）。

        タイムスタンプ鮮度チェック（5 分窓）でリプレイ攻撃を防ぐ。
        Discord 仕様上必須ではないが、/start・/stop のような副作用コマンドへの
        過去リクエスト再送を防ぐためのベストプラクティス実装。
        （参照: Discord Interactions Overview — Security and Authorization）
        """
        sig_hex   = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")

        # タイムスタンプ鮮度チェック（リプレイ攻撃防止）
        try:
            ts_age = abs(time.time() - int(timestamp))
            if ts_age > 300:  # 5 分以上古いリクエストは拒否
                logger.warning("タイムスタンプが古すぎます（リプレイ攻撃の可能性）: age=%.0fs", ts_age)
                return False
        except (ValueError, TypeError):
            logger.warning("x-signature-timestamp が不正な値です")
            return False

        msg = (timestamp + raw_body).encode("utf-8")
        # 署名・公開鍵の断片は CloudWatch へのキー素材漏洩を防ぐため記録しない
        logger.debug("署名検証: body_len=%d", len(raw_body))
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

        # deferred フォローアップに必要な app_id と token を取得
        app_id = body.get("application_id", "")
        token  = body.get("token", "")

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
                app_id=app_id,
                token=token,
            )

        if interaction_type == 2:
            return Request(
                kind="command",
                command=command,
                options=options,
                user_id=user_id,
                app_id=app_id,
                token=token,
            )

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

    def deferred_response(self, ephemeral: bool = True) -> dict:
        """
        Discord Deferred response（DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, type 5）。
        3 秒以内に返すことで Discord の「応答待ち」状態（考え中…）を設定し、
        重い処理を別 Lambda invoke で非同期実行できるようにする。
        フォローアップは send_followup() で @original を PATCH する。
        """
        return _json_response({
            "type": 5,
            "data": {"flags": 64 if ephemeral else 0},  # 64 = EPHEMERAL
        })

    def send_followup(self, app_id: str, token: str, text: str) -> None:
        """
        Deferred インタラクションにフォローアップメッセージを送信する。
        PATCH /webhooks/{app_id}/{token}/messages/@original で「考え中…」を結果に上書きする。
        urllib.request を使用（外部依存ゼロ）。token の有効期限は 15 分。
        """
        if len(text) > 1990:
            text = text[:1990] + "\n...（省略）"
        url  = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
        data = json.dumps({"content": text}).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data=data,
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("フォローアップ送信成功: status=%d app_id=%s", resp.status, app_id)

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
