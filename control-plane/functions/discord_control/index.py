"""
index.py - ゲームサーバー制御 Lambda ハンドラ

Discord のスラッシュコマンドを受信・処理する。
Function URL 経由で Discord から POST される。

対応コマンド:
    /games              → 全ゲームサーバーの一覧と稼働状態
    /start game:<name>  → ゲームサーバーを起動
    /stop  game:<name>  → ゲームサーバーを停止
    /status game:<name> → 稼働状態と現在の IP アドレス
    /cost               → 今月の AWS コスト・予算残・月末予測
    /update game:<name> → サーバー本体を停止したままアップデート
    /backup game:<name> → 今すぐ EFS→S3 のバックアップを実行
    /restore game:<name> → S3→EFS へ最新バックアップをミラーリング（要停止）
    /switch-slot game:<name> slot:<name> → セーブデータのスロットを切り替え（要停止）
    /launch-mode game:<name> [mode:<spot|ondemand>] → 起動タイプの表示・切り替え（次回 /start から適用）

各コマンドの実装本体は commands/ パッケージにコマンド単位で分割されている
（commands/games.py, commands/start.py, ...）。共通のガード処理・AWS 呼び出し
ヘルパーはそれぞれ commands/guards.py・ecs_helpers.py に集約。

ゲームの発見方法:
    ECS クラスターの「Game」タグで対象クラスターを特定する。
    → ゲームを追加しても Discord 側の再設定は不要。

起動状態の判定:
    ECS クラスターの「StatusParamPrefix」タグに SSM パラメータのプレフィックスを持つ。
    monitor サイドカーが SSM の ready/players を書き込み、このLambdaがそれを読む。

ツール非依存設計:
    Discord 固有プロトコル（署名検証・ペイロード解析・応答フォーマット）は
    provider.py の DiscordProvider に隔離している。
    このファイルは AWS ビジネスロジックのみを扱う。
    他ツールへの切り替えは provider.py を参照。
"""
import base64
import json
import logging
import os

from clients import lambda_client
from commands import autocomplete_choices, dispatch_command
from constants import RESTRICTED_COMMANDS
from provider import get_provider

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- 環境変数（Terraform の control-plane/main.tf から注入）---
# カンマ区切りで許可ユーザーIDを受け取る。空の場合は全員許可
# MESSAGING_ALLOWED_USER_IDS を優先し、なければ後方互換で DISCORD_ALLOWED_USER_IDS を参照
_raw_ids = (
    os.environ.get("MESSAGING_ALLOWED_USER_IDS")
    or os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
)
ALLOWED_USER_IDS = set(uid.strip() for uid in _raw_ids.split(",") if uid.strip())


# =============================================================================
# Lambda エントリーポイント
# =============================================================================

def lambda_handler(event: dict, context) -> dict:
    # イベント全文のログ出力は避ける（Discord ヘッダーに署名・機密が含まれるため）
    # デバッグが必要な場合は環境変数 LOG_LEVEL=DEBUG に変更してから確認すること
    logger.debug("Event: %s", json.dumps(event))
    logger.info("Lambda invoked: requestContext.requestId=%s",
                event.get("requestContext", {}).get("requestId", "deferred"))

    # --- deferred ワーカーモード（自己非同期 invoke から呼ばれる）---
    # Discord からの HTTP リクエストではなく内部 JSON ペイロードのため、
    # 署名検証をスキップして直接コマンドを実行し、フォローアップ Webhook で結果を返す。
    if event.get("ggs_deferred"):
        return _handle_deferred_worker(event)

    provider = get_provider()

    # --- リクエストボディを取得 ---
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode("utf-8")

    # --- ツール固有の署名検証（失敗したら 401 を返す）---
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not provider.verify(headers, body_raw):
        logger.warning("署名検証失敗")
        return {"statusCode": 401, "body": "Invalid request signature"}

    body = json.loads(body_raw)
    req  = provider.parse(headers, body)

    # --- PING（Interactions Endpoint URL 登録時の疎通確認）---
    if req.kind == "ping":
        return provider.ping_response()

    # --- オートコンプリート（game: オプションの候補補完）---
    # 認証は行わない（返すのはゲーム名の一覧のみで、制限コマンドの実行自体は
    # 下の許可リストチェックで拒否されるため実害がない）
    if req.kind == "autocomplete":
        choices = autocomplete_choices(req.focused)
        return provider.autocomplete(choices)

    # --- スラッシュコマンド ---
    if req.kind == "command":
        # 破壊的・コスト影響のあるコマンド（RESTRICTED_COMMANDS）のみ許可リストで制限する。
        # 閲覧系（/games /status /cost）は誰でも実行可。ALLOWED_USER_IDS 未設定なら全コマンド全員許可
        if (
            ALLOWED_USER_IDS
            and req.command in RESTRICTED_COMMANDS
            and req.user_id not in ALLOWED_USER_IDS
        ):
            return provider.message(
                "⛔ このコマンドを実行する権限がありません。\n"
                "閲覧系コマンド（/games /status /cost）は誰でも利用できます。"
            )
        # 重い AWS API 呼び出しを自己非同期 invoke に移譲し、3 秒以内に deferred を返す。
        # ワーカーが処理後に send_followup() で「考え中…」を結果に上書きする。
        try:
            lambda_client.invoke(
                FunctionName=context.function_name,
                InvocationType="Event",  # 非同期: StatusCode=202 → すぐに返る
                Payload=json.dumps({
                    "ggs_deferred": True,
                    "command":      req.command,
                    "options":      req.options,
                    "app_id":       req.app_id,
                    "token":        req.token,
                }).encode("utf-8"),
            )
            logger.info("deferred ワーカーを非同期 invoke: command=%s", req.command)
        except Exception:
            logger.exception("deferred ワーカーの invoke に失敗: command=%s", req.command)
            return provider.message("❌ コマンド処理の開始に失敗しました。しばらく後に再試行してください。")
        return provider.deferred_response()

    return provider.message("不明なリクエストタイプです。")


# =============================================================================
# Deferred ワーカー
# =============================================================================

def _handle_deferred_worker(event: dict) -> dict:
    """
    deferred モードのワーカー実行。
    自己非同期 invoke（InvocationType="Event"）で呼ばれ、
    コマンドを実行して Discord フォローアップ Webhook で「考え中…」を結果に上書きする。
    例外が発生してもエラー文言を必ずフォローアップ送信し、「考え中…」放置を防ぐ。
    """
    provider = get_provider()
    app_id   = event.get("app_id", "")
    token    = event.get("token", "")
    command  = event.get("command", "")
    options  = event.get("options", {})

    logger.info("deferred ワーカー起動: command=%s options=%s", command, options)
    try:
        text = dispatch_command(command, options)
    except Exception:
        logger.exception("deferred ワーカー: コマンド実行失敗: command=%s", command)
        text = "❌ コマンド実行中にエラーが発生しました。しばらく後に再試行してください。"

    try:
        provider.send_followup(app_id, token, text)
    except Exception:
        logger.exception("deferred ワーカー: フォローアップ送信失敗: app_id=%s", app_id)

    return {"statusCode": 200}
