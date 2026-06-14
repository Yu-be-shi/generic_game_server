"""
index.py - Discord Interactions Lambda ハンドラ

Discord のスラッシュコマンドを受信・処理する。
Function URL 経由で Discord から POST される。

対応コマンド:
    /games              → 全ゲームサーバーの一覧と稼働状態
    /start game:<name>  → ゲームサーバーを起動
    /stop  game:<name>  → ゲームサーバーを停止
    /status game:<name> → 稼働状態と現在の IP アドレス

ゲームの発見方法:
    ECS クラスターの「Game」タグで対象クラスターを特定する。
    → ゲームを追加しても Discord 側の再設定は不要。
"""
import base64
import json
import logging
import os

import boto3
import ed25519

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- 環境変数（Terraform の control-plane/main.tf から注入）---
DISCORD_PUBLIC_KEY = os.environ["DISCORD_PUBLIC_KEY"]
# Lambda が動いているリージョンより GAME_AWS_REGION を優先する
AWS_REGION = os.environ.get("GAME_AWS_REGION") or os.environ.get("AWS_REGION", "ap-northeast-1")
# カンマ区切りで許可ユーザーIDを受け取る。空の場合は全員許可
_raw_ids = os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(uid.strip() for uid in _raw_ids.split(",") if uid.strip())

ecs = boto3.client("ecs", region_name=AWS_REGION)
ec2 = boto3.client("ec2", region_name=AWS_REGION)


# =============================================================================
# Lambda エントリーポイント
# =============================================================================

def lambda_handler(event: dict, context) -> dict:
    logger.info("Event: %s", json.dumps(event))

    # --- リクエストボディを取得 ---
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode("utf-8")

    # --- Discord の署名を検証（失敗したら 401 を返す）---
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig_hex = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")

    if not ed25519.verify(
        DISCORD_PUBLIC_KEY,
        (timestamp + body_raw).encode("utf-8"),
        sig_hex,
    ):
        logger.warning("署名検証失敗")
        return {"statusCode": 401, "body": "Invalid request signature"}

    body = json.loads(body_raw)
    interaction_type = body.get("type")

    # --- PING（Discord が Interactions Endpoint URL 登録時に送るテスト）---
    if interaction_type == 1:
        return _json_response({"type": 1})

    # --- スラッシュコマンド ---
    if interaction_type == 2:
        return _handle_command(body)

    return _ephemeral("不明な Interaction タイプです。")


# =============================================================================
# コマンドルーティング
# =============================================================================

def _handle_command(body: dict) -> dict:
    """コマンド名で処理を振り分ける"""
    data = body.get("data", {})
    command = data.get("name", "")

    # 操作者 ID のチェック（DISCORD_ALLOWED_USER_IDS が設定されている場合のみ）
    if ALLOWED_USER_IDS:
        user = (body.get("member") or {}).get("user") or body.get("user") or {}
        if user.get("id", "") not in ALLOWED_USER_IDS:
            return _ephemeral("⛔ このボットを操作する権限がありません。")

    # コマンドオプションを dict に展開
    options = {o["name"]: o["value"] for o in data.get("options", [])}
    game_name = options.get("game", "").strip()

    if command == "games":
        return _cmd_games()
    elif command == "start":
        return _cmd_start(game_name) if game_name else _ephemeral("ゲーム名を指定してください。")
    elif command == "stop":
        return _cmd_stop(game_name) if game_name else _ephemeral("ゲーム名を指定してください。")
    elif command == "status":
        return _cmd_status(game_name) if game_name else _ephemeral("ゲーム名を指定してください。")
    else:
        return _ephemeral(f"不明なコマンド: `/{command}`")


# =============================================================================
# 各コマンドの実装
# =============================================================================

def _cmd_games() -> dict:
    """/games: 全ゲームの一覧と稼働状態を返す"""
    clusters = _list_game_clusters()

    if not clusters:
        return _ephemeral(
            "🔍 **ゲームサーバーが見つかりませんでした**\n"
            "`game-stack` はデプロイされていますか？"
        )

    lines = ["**🎮 ゲームサーバー一覧**\n"]
    for c in clusters:
        desired = c["desired_count"]
        running = c["running_count"]

        if desired > 0 and running > 0:
            icon = "🟢"
            stat = "稼働中"
        elif desired > 0 and running == 0:
            icon = "🟡"
            stat = "起動中..."
        else:
            icon = "⚫"
            stat = "停止中"

        lines.append(f"{icon} **{c['game_tag']}**: {stat}")

    lines.append("\n*`/status game:<name>` で IP を確認できます*")
    return _ephemeral("\n".join(lines))


def _cmd_start(game_name: str) -> dict:
    """/start game:<name>: サーバーを起動する"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return _ephemeral(
            f"❌ ゲーム `{game_name}` が見つかりません。\n"
            "`/games` で利用可能なゲームを確認してください。"
        )

    svc = _describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        return _ephemeral(
            f"ℹ️ **{game_name}** はすでに起動中（または起動処理中）です。\n"
            f"`/status game:{game_name}` で IP を確認できます。"
        )

    ecs.update_service(cluster=cluster_arn, service=service_arn, desiredCount=1)
    return _ephemeral(
        f"✅ **{game_name}** の起動を開始しました！\n"
        f"1〜2分後に起動 IP が通知されます 📨"
    )


def _cmd_stop(game_name: str) -> dict:
    """/stop game:<name>: サーバーを停止する"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return _ephemeral(f"❌ ゲーム `{game_name}` が見つかりません。")

    ecs.update_service(cluster=cluster_arn, service=service_arn, desiredCount=0)
    return _ephemeral(f"🛑 **{game_name}** を停止しました。")


def _cmd_status(game_name: str) -> dict:
    """/status game:<name>: 稼働状態と現在の IP アドレスを返す"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return _ephemeral(f"❌ ゲーム `{game_name}` が見つかりません。")

    svc = _describe_service(cluster_arn, service_arn)
    if not svc:
        return _ephemeral("❌ サービス情報を取得できませんでした。")

    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)

    if desired == 0:
        return _ephemeral(
            f"⚫ **{game_name}** は停止中です。\n"
            f"`/start game:{game_name}` で起動できます。"
        )

    if running == 0:
        return _ephemeral(
            f"🟡 **{game_name}** は起動処理中です。しばらくお待ちください。"
        )

    # 実行中タスクのパブリック IP を取得
    public_ip = _get_running_task_ip(cluster_arn)
    ip_str = f"`{public_ip}`" if public_ip else "取得中..."

    return _ephemeral(
        f"🟢 **{game_name}** 稼働中\n"
        f"IP アドレス: {ip_str}"
    )


# =============================================================================
# ECS ユーティリティ
# =============================================================================

def _list_game_clusters() -> list[dict]:
    """
    全クラスターの中から Game タグが付いているものを返す。
    各エントリに game_tag, cluster_arn, desired_count, running_count を含む。
    """
    try:
        cluster_arns = ecs.list_clusters()["clusterArns"]
    except Exception:
        logger.exception("list_clusters 失敗")
        return []

    if not cluster_arns:
        return []

    clusters_info = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"])["clusters"]
    result = []

    for c in clusters_info:
        # Game タグがあるクラスターのみ対象
        game_tag = next((t["value"] for t in c.get("tags", []) if t["key"] == "Game"), None)
        if not game_tag:
            continue

        desired, running = 0, 0
        try:
            svc_arns = ecs.list_services(cluster=c["clusterArn"])["serviceArns"]
            if svc_arns:
                svc = ecs.describe_services(cluster=c["clusterArn"], services=svc_arns)["services"]
                if svc:
                    desired = svc[0].get("desiredCount", 0)
                    running = svc[0].get("runningCount", 0)
        except Exception:
            logger.exception("サービス情報の取得に失敗: %s", c["clusterArn"])

        result.append({
            "game_tag": game_tag,
            "cluster_arn": c["clusterArn"],
            "desired_count": desired,
            "running_count": running,
        })

    return sorted(result, key=lambda x: x["game_tag"])


def _find_service(game_name: str) -> tuple[str | None, str | None]:
    """
    game_name に対応するクラスター ARN とサービス ARN を返す。
    見つからない場合は (None, None)。
    Game タグで照合するため大文字小文字を無視する。
    """
    try:
        cluster_arns = ecs.list_clusters()["clusterArns"]
    except Exception:
        return None, None

    if not cluster_arns:
        return None, None

    clusters_info = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"])["clusters"]

    for c in clusters_info:
        tags = {t["key"]: t["value"] for t in c.get("tags", [])}
        if tags.get("Game", "").lower() != game_name.lower():
            continue

        try:
            svc_arns = ecs.list_services(cluster=c["clusterArn"])["serviceArns"]
        except Exception:
            continue

        if svc_arns:
            return c["clusterArn"], svc_arns[0]

    return None, None


def _describe_service(cluster_arn: str, service_arn: str) -> dict | None:
    """サービス情報を取得する"""
    try:
        result = ecs.describe_services(cluster=cluster_arn, services=[service_arn])
        services = result.get("services", [])
        return services[0] if services else None
    except Exception:
        logger.exception("describe_services 失敗")
        return None


def _get_running_task_ip(cluster_arn: str) -> str | None:
    """実行中タスクのパブリック IP を返す。なければ None。"""
    try:
        task_arns = ecs.list_tasks(cluster=cluster_arn, desiredStatus="RUNNING")["taskArns"]
        if not task_arns:
            return None

        tasks = ecs.describe_tasks(cluster=cluster_arn, tasks=task_arns[:1])["tasks"]
        if not tasks:
            return None

        # attachments から ENI ID を探す
        eni_id = None
        for attachment in tasks[0].get("attachments", []):
            if attachment.get("type") != "ElasticNetworkInterface":
                continue
            for detail in attachment.get("details", []):
                if detail.get("name") == "networkInterfaceId":
                    eni_id = detail.get("value")
                    break

        if not eni_id:
            return None

        interfaces = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"]
        if not interfaces:
            return None

        return interfaces[0].get("Association", {}).get("PublicIp")

    except Exception:
        logger.exception("IP 取得失敗")
        return None


# =============================================================================
# レスポンスヘルパー
# =============================================================================

def _json_response(body: dict, status_code: int = 200) -> dict:
    """Lambda Function URL 形式のレスポンスを生成する"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _ephemeral(content: str) -> dict:
    """
    エフェメラルメッセージ（コマンド実行者にのみ表示されるメッセージ）。
    Discord の CHANNEL_MESSAGE_WITH_SOURCE (type 4) + EPHEMERAL フラグ (64)。
    """
    # Discord の文字数制限（2000文字）に収める
    if len(content) > 1990:
        content = content[:1990] + "\n...（省略）"

    return _json_response({
        "type": 4,   # CHANNEL_MESSAGE_WITH_SOURCE
        "data": {
            "content": content,
            "flags": 64,  # EPHEMERAL（本人のみに表示）
        },
    })
