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
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import boto3

from provider import get_provider

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- 環境変数（Terraform の control-plane/main.tf から注入）---
# Lambda が動いているリージョンより GAME_AWS_REGION を優先する
AWS_REGION = os.environ.get("GAME_AWS_REGION") or os.environ.get("AWS_REGION", "ap-northeast-1")
# カンマ区切りで許可ユーザーIDを受け取る。空の場合は全員許可
# MESSAGING_ALLOWED_USER_IDS を優先し、なければ後方互換で DISCORD_ALLOWED_USER_IDS を参照
_raw_ids = (
    os.environ.get("MESSAGING_ALLOWED_USER_IDS")
    or os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
)
ALLOWED_USER_IDS = set(uid.strip() for uid in _raw_ids.split(",") if uid.strip())

ecs = boto3.client("ecs", region_name=AWS_REGION)
ec2 = boto3.client("ec2", region_name=AWS_REGION)
ssm = boto3.client("ssm", region_name=AWS_REGION)
# Cost Explorer / Budgets はグローバルサービスのため us-east-1 固定
# AWS_REGION（ap-northeast-1）を流用すると EndpointResolutionError になる
ce       = boto3.client("ce",      region_name="us-east-1")
budgets  = boto3.client("budgets", region_name="us-east-1")
sts_client = boto3.client("sts")
# /update コマンド: game-stack の auto_update Worker Lambda を非同期 invoke するために使用
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

# /status が「稼働中」に昇格するまでの猶予時間（秒）
# ready=1 になってからこの時間が経過すれば、Webhook 通知済みでなくても「稼働中」を返す。
# 正常時の Webhook（notify_ip Lambda）は数秒で完了するため、
# 猶予中に先に「接続可能になりました！」が Discord に届く。
# Webhook が万一失敗しても、60 秒後にはユーザーが /status で IP を確認できる。
STATUS_READY_GRACE_SECONDS = 60


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
    if req.kind == "autocomplete":
        choices = _autocomplete_choices(req.focused)
        return provider.autocomplete(choices)

    # --- スラッシュコマンド ---
    if req.kind == "command":
        if ALLOWED_USER_IDS and req.user_id not in ALLOWED_USER_IDS:
            return provider.message("⛔ このボットを操作する権限がありません。")
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
        text = _dispatch_command(command, options)
    except Exception:
        logger.exception("deferred ワーカー: コマンド実行失敗: command=%s", command)
        text = "❌ コマンド実行中にエラーが発生しました。しばらく後に再試行してください。"

    try:
        provider.send_followup(app_id, token, text)
    except Exception:
        logger.exception("deferred ワーカー: フォローアップ送信失敗: app_id=%s", app_id)

    return {"statusCode": 200}


# =============================================================================
# コマンドルーティング
# =============================================================================

def _autocomplete_choices(focused: str) -> list:
    """
    オートコンプリート候補を返す。
    ECS の Game タグからゲーム名を取得し、入力値で部分一致フィルタする。
    先頭一致を優先してソート（先頭一致 → それ以外の含む → アルファベット順）。
    """
    game_names   = _list_game_names()
    partial_lower = focused.lower()
    starts   = [g for g in game_names if g.lower().startswith(partial_lower)]
    contains = [g for g in game_names if partial_lower in g.lower() and not g.lower().startswith(partial_lower)]
    return (starts + contains)[:25]  # Discord 上限 25 件


def _dispatch_command(command: str, options: dict) -> str:
    """コマンド名でハンドラに振り分け、メッセージ本文（str）を返す"""
    game_name = options.get("game", "").strip()

    if command == "games":
        return _cmd_games()
    elif command == "start":
        return _cmd_start(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "stop":
        return _cmd_stop(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "status":
        return _cmd_status(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "cost":
        return _cmd_cost()
    elif command == "update":
        return _cmd_update(game_name) if game_name else "ゲーム名を指定してください。"
    else:
        return f"不明なコマンド: `/{command}`"


# =============================================================================
# 各コマンドの実装（全て str を返す）
# =============================================================================

def _cmd_games() -> str:
    """/games: 全ゲームの一覧と稼働状態を返す"""
    clusters = _list_game_clusters()

    if not clusters:
        return (
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
    return "\n".join(lines)


def _cmd_start(game_name: str) -> str:
    """/start game:<name>: サーバーを起動する"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return (
            f"❌ ゲーム `{game_name}` が見つかりません。\n"
            "`/games` で利用可能なゲームを確認してください。"
        )

    svc = _describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        return (
            f"ℹ️ **{game_name}** はすでに起動中（または起動処理中）です。\n"
            f"`/status game:{game_name}` で IP を確認できます。"
        )

    # 自動アップデート実行中は起動を拒否（EFS install への二重書き込み防止）
    ssm_prefix_for_check = _get_cluster_tag(cluster_arn, "StatusParamPrefix")
    if ssm_prefix_for_check:
        try:
            maint = ssm.get_parameter(Name=f"{ssm_prefix_for_check}/maintenance")["Parameter"]["Value"]
            if maint == "1":
                return (
                    f"🔧 **{game_name}** はメンテナンス中（自動アップデート実行中）です。\n"
                    "数分後に完了通知が届きます。それからお試しください。"
                )
        except Exception:
            pass  # パラメータ未作成（初回）または取得失敗 → そのまま続行

    # 常に最新タスク定義で起動する。
    # ecs.tf の ignore_changes=[task_definition] により terraform apply しても
    # サービスが参照するリビジョンは更新されないため、起動時に明示的に指定する。
    # 失敗した場合はタスク定義を指定せず従来通りサービス参照中のリビジョンで起動する。
    latest_task_def_arn = None
    if svc:
        current_task_def_arn = svc.get("taskDefinition", "")
        # ARN からファミリー名（例: "palworld-palworld"）を抽出して最新リビジョンを取得
        family = current_task_def_arn.split("/")[-1].split(":")[0] if current_task_def_arn else ""
        if family:
            latest_task_def_arn = _get_latest_task_def_arn(family)

    update_kwargs: dict = {"cluster": cluster_arn, "service": service_arn, "desiredCount": 1}
    if latest_task_def_arn:
        update_kwargs["taskDefinition"] = latest_task_def_arn
        logger.info("最新タスク定義を指定して起動: %s", latest_task_def_arn)

    ecs.update_service(**update_kwargs)

    # /start の時点で SSM ready を 0 にリセットする。
    # monitor が ready=0 を書くまでの空白（約30〜90秒）で古い ready=1 が残ると
    # /status が「稼働中」と誤表示するため、ここで先手を打つ。
    # monitor 起動失敗時も /status が永久に「稼働中」になるのを防ぐ。
    # (notify_ip.py は value!="1" をスキップするため誤通知なし)
    ssm_prefix = _get_cluster_tag(cluster_arn, "StatusParamPrefix")
    if ssm_prefix:
        try:
            ssm.put_parameter(
                Name=f"{ssm_prefix}/ready",
                Value="0",
                Type="String",
                Overwrite=True,
            )
            logger.info("/start: SSM ready を 0 にリセット: %s/ready", ssm_prefix)
        except Exception:
            logger.warning("/start: SSM ready のリセットに失敗（権限確認を）: %s/ready", ssm_prefix)

    return (
        f"✅ **{game_name}** の起動を開始しました！\n"
        f"接続可能になったら IP が通知されます 📨"
    )


def _cmd_stop(game_name: str) -> str:
    """/stop game:<name>: サーバーを停止する"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return f"❌ ゲーム `{game_name}` が見つかりません。"

    ecs.update_service(cluster=cluster_arn, service=service_arn, desiredCount=0)
    return f"🛑 **{game_name}** の停止処理を開始しました。完全停止後に通知します。"


def _cmd_status(game_name: str) -> str:
    """/status game:<name>: 稼働状態と現在の IP アドレスを返す"""
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return f"❌ ゲーム `{game_name}` が見つかりません。"

    svc = _describe_service(cluster_arn, service_arn)
    if not svc:
        return "❌ サービス情報を取得できませんでした。"

    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)

    if desired == 0:
        return (
            f"⚫ **{game_name}** は停止中です。\n"
            f"`/start game:{game_name}` で起動できます。"
        )

    if running == 0:
        return f"🟡 **{game_name}** は起動処理中です。しばらくお待ちください。"

    # 実行中タスクのパブリック IP・タスク ARN を取得
    public_ip, _, task_arn = _get_running_task_info(cluster_arn)
    ip_str = f"`{public_ip}`" if public_ip else "取得中..."

    # SSM からゲームサーバーの実起動状態・プレイヤー数を取得
    # クラスターの StatusParamPrefix タグが SSM パラメータのプレフィックスを示す
    ssm_prefix = _get_cluster_tag(cluster_arn, "StatusParamPrefix")
    if ssm_prefix:
        ready, players, ready_age = _get_ssm_status(ssm_prefix)
        notified = _get_notified_task(ssm_prefix)
        # Webhook 通知済み = notify_ip Lambda が通知 POST 後に notified_task を書き込んだ
        ip_notified = bool(task_arn and notified and notified == task_arn)
        # ready=1 になってから GRACE 秒以上経過していれば、通知済みでなくてもフォールバック
        # （Webhook 遅延/失敗時でも /status から IP を確認できるようにする）
        ready_long_enough = ready_age is not None and ready_age >= STATUS_READY_GRACE_SECONDS
        logger.info(
            "SSM ステータス: prefix=%s ready=%s players=%s ready_age=%.1fs "
            "ip_notified=%s ready_long_enough=%s",
            ssm_prefix, ready, players, ready_age or 0.0, ip_notified, ready_long_enough,
        )
        if ready and (ip_notified or ready_long_enough):
            if players is not None and players >= 0:
                player_str = f"（プレイヤー {players} 人）"
            else:
                player_str = ""
            return (
                f"🟢 **{game_name}** 稼働中{player_str}\n"
                f"IP アドレス: {ip_str}"
            )
        else:
            return (
                f"🟡 **{game_name}** 起動処理中\n"
                f"ECSタスクは起動済み。サーバー初期化中です…（1〜数分かかります）"
            )

    # StatusParamPrefix タグ未設定のゲームはフォールバック
    return (
        f"🟢 **{game_name}** 稼働中\n"
        f"IP アドレス: {ip_str}"
    )


def _cmd_cost() -> str:
    """
    /cost: 今月の AWS コスト・月末着地予測・予算と残額を返す。

    Cost Explorer / Budgets はグローバルサービスのため us-east-1 クライアントを使用。
    コスト配分タグの有効化は不要（アカウント全体合計を集計）。
    """
    today       = date.today()
    month_start = today.replace(day=1)
    tomorrow    = today + timedelta(days=1)

    # 翌月1日（forecast の EndDate）
    if today.month == 12:
        next_month_first = date(today.year + 1, 1, 1)
    else:
        next_month_first = date(today.year, today.month + 1, 1)

    lines = [f"💰 **今月の AWS コスト** ({month_start.isoformat()} 〜 {today.isoformat()})\n"]

    # --- 今月累計 (MTD) ---
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": month_start.isoformat(), "End": tomorrow.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        result_by_time = resp.get("ResultsByTime", [])
        if result_by_time:
            total = result_by_time[0]["Total"]["UnblendedCost"]
            amount = float(total["Amount"])
            unit   = total["Unit"]
            lines.append(f"使用額（MTD）: **${amount:.2f} {unit}**")
    except Exception:
        logger.exception("Cost Explorer: MTD 取得失敗")
        lines.append("使用額（MTD）: 取得失敗")

    # --- 月末着地予測 ---
    # 履歴が少ない（新アカウント等）と DataUnavailableException が送出されるためスキップ
    try:
        forecast_resp = ce.get_cost_forecast(
            TimePeriod={"Start": today.isoformat(), "End": next_month_first.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        forecast_total = forecast_resp.get("Total", {})
        forecast_amount = float(forecast_total.get("Amount", 0))
        forecast_unit   = forecast_total.get("Unit", "USD")
        lines.append(f"月末着地予測: **${forecast_amount:.2f} {forecast_unit}**")
    except Exception:
        # 履歴不足等で予測が取れない場合はサイレントスキップ
        logger.info("Cost Explorer: 予測取得失敗（履歴不足の可能性）")

    # --- 予算と残額 ---
    try:
        account_id = sts_client.get_caller_identity()["Account"]
        b_resp   = budgets.describe_budgets(AccountId=account_id)
        budget_list = b_resp.get("Budgets", [])
        if budget_list:
            lines.append("")  # 空行
            for b in budget_list:
                name  = b.get("BudgetName", "")
                limit = float(b.get("BudgetLimit", {}).get("Amount", 0))
                limit_unit = b.get("BudgetLimit", {}).get("Unit", "USD")
                actual    = float(b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0))
                remaining = limit - actual
                lines.append(
                    f"📊 **{name}**\n"
                    f"  予算: ${limit:.2f} / 使用: ${actual:.2f} / 残: ${remaining:.2f} {limit_unit}"
                )
        else:
            lines.append("（予算未設定）")
    except Exception:
        logger.exception("Budgets: 取得失敗")

    return "\n".join(lines)


def _cmd_update(game_name: str) -> str:
    """
    /update game:<name>: サーバーアップデートを実行する。

    同一タスク定義を UPDATE_ON_BOOT=true でワンオフ起動し、SteamCMD アップデートを
    実行する Worker Lambda を非同期 invoke する。通常の /start（UPDATE_ON_BOOT=false）
    の高速起動には一切影響しない。

    処理フロー:
      1. ゲームが存在するか確認
      2. サーバーが起動中なら拒否（EFS への二重書き込み防止）
      3. メンテナンス中なら拒否（二重実行防止）
      4. AutoUpdateFunction タグから Worker Lambda 名を取得
      5. Worker を非同期 invoke（Discord 3秒制限内で即応答するため Event モード）
      6. 「🔄 開始しました」を返す（完了通知は Worker から Webhook で届く）
    """
    cluster_arn, service_arn = _find_service(game_name)
    if not cluster_arn:
        return (
            f"❌ ゲーム `{game_name}` が見つかりません。\n"
            "`/games` で利用可能なゲームを確認してください。"
        )

    # サービス起動中は拒否（ゲームプロセスが EFS を使用中）
    svc = _describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        return (
            f"⚠️ **{game_name}** は現在起動中です。\n"
            f"`/stop game:{game_name}` で停止してからアップデートしてください。"
        )

    # メンテナンス中（別の update が実行中）は拒否
    ssm_prefix = _get_cluster_tag(cluster_arn, "StatusParamPrefix")
    if ssm_prefix:
        try:
            maint = ssm.get_parameter(Name=f"{ssm_prefix}/maintenance")["Parameter"]["Value"]
            if maint == "1":
                return (
                    f"🔧 **{game_name}** はすでにアップデート中です。\n"
                    "完了通知が届くまでお待ちください。"
                )
        except Exception:
            pass  # パラメータ未作成（初回）または取得失敗 → そのまま続行

    # AutoUpdateFunction タグから Worker Lambda 名を取得
    worker_function = _get_cluster_tag(cluster_arn, "AutoUpdateFunction")
    if not worker_function:
        return (
            f"❌ **{game_name}** の AutoUpdateFunction タグが見つかりません。\n"
            "`game-stack` の `terraform apply` を実行してください。"
        )

    # Worker Lambda を非同期 invoke（Discord 3秒制限内で即応答するため Event モード）
    try:
        lambda_client.invoke(
            FunctionName=worker_function,
            InvocationType="Event",  # 非同期: StatusCode=202 → すぐに返る
            Payload=json.dumps({"game_name": game_name}).encode("utf-8"),
        )
        logger.info("auto_update Worker を非同期 invoke: function=%s game=%s", worker_function, game_name)
    except Exception:
        logger.exception("Worker Lambda の invoke に失敗: %s", worker_function)
        return (
            f"❌ **{game_name}** のアップデート Worker の起動に失敗しました。\n"
            "IAM 権限または Lambda 設定を確認してください。"
        )

    return (
        f"🔄 **{game_name}** のアップデートを開始しました。\n"
        "SteamCMD でサーバーを更新中です。完了したら通知します（数分かかります）。\n"
        "アップデート中は `/start` できません。"
    )


# =============================================================================
# ECS ユーティリティ
# =============================================================================

def _list_game_clusters() -> list:
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
            "game_tag":     game_tag,
            "cluster_arn":  c["clusterArn"],
            "desired_count": desired,
            "running_count": running,
        })

    return sorted(result, key=lambda x: x["game_tag"])


def _list_game_names() -> list:
    """
    ECS クラスターの Game タグからゲーム名一覧を返す（軽量版）。

    オートコンプリート用途のため describe_services は呼ばず、
    タグ取得のみで済ませる（Discord の ~3 秒 autocomplete 制限に収めるため）。
    """
    try:
        cluster_arns = ecs.list_clusters()["clusterArns"]
        if not cluster_arns:
            return []
        clusters_info = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"])["clusters"]
        names = [
            t["value"]
            for c in clusters_info
            for t in c.get("tags", [])
            if t["key"] == "Game"
        ]
        return sorted(names)
    except Exception:
        logger.exception("_list_game_names 失敗")
        return []


def _find_service(game_name: str):
    """
    game_name に対応するクラスター ARN とサービス ARN を返す。
    見つからない場合は (None, None)。

    照合優先順位:
      1. Game タグとの完全一致（大文字小文字無視）
      2. Game タグへの一意な部分一致（autocomplete 未使用で手打ちした場合の救済）
         複数候補がある場合はあいまいなため not found とする。
    """
    try:
        cluster_arns = ecs.list_clusters()["clusterArns"]
    except Exception:
        return None, None

    if not cluster_arns:
        return None, None

    clusters_info = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"])["clusters"]
    name_lower = game_name.lower()

    exact_match   = None      # (cluster_arn, svc_arn)
    partial_matches = []      # [(cluster_arn, svc_arn)]

    for c in clusters_info:
        tags     = {t["key"]: t["value"] for t in c.get("tags", [])}
        game_tag = tags.get("Game", "")
        if not game_tag:
            continue

        try:
            svc_arns = ecs.list_services(cluster=c["clusterArn"])["serviceArns"]
        except Exception:
            continue

        if not svc_arns:
            continue

        if game_tag.lower() == name_lower:
            exact_match = (c["clusterArn"], svc_arns[0])
            break  # 完全一致が見つかれば即確定

        if name_lower in game_tag.lower():
            partial_matches.append((c["clusterArn"], svc_arns[0]))

    if exact_match:
        return exact_match

    # 部分一致が一意ならフォールバック採用（複数候補は曖昧なため不採用）
    if len(partial_matches) == 1:
        logger.info("_find_service: 部分一致フォールバック採用: input=%s", game_name)
        return partial_matches[0]

    if len(partial_matches) > 1:
        logger.info("_find_service: 部分一致が複数あり採用不可: input=%s count=%d", game_name, len(partial_matches))

    return None, None


def _describe_service(cluster_arn: str, service_arn: str):
    """サービス情報を取得する"""
    try:
        result   = ecs.describe_services(cluster=cluster_arn, services=[service_arn])
        services = result.get("services", [])
        return services[0] if services else None
    except Exception:
        logger.exception("describe_services 失敗")
        return None


def _get_latest_task_def_arn(family: str):
    """
    タスク定義ファミリーの最新 ACTIVE リビジョン ARN を返す。
    取得に失敗した場合は None を返す（呼び出し元がフォールバック処理を行う）。
    """
    try:
        result = ecs.describe_task_definition(taskDefinition=family)
        arn    = result.get("taskDefinition", {}).get("taskDefinitionArn")
        logger.info("最新タスク定義 ARN 取得: family=%s arn=%s", family, arn)
        return arn
    except Exception:
        logger.exception("最新タスク定義 ARN の取得に失敗: family=%s", family)
        return None


def _get_running_task_info(cluster_arn: str):
    """
    実行中タスクのパブリック IP、タスク定義 ARN、タスクインスタンス ARN を返す。
    どちらかを取得できない場合は対応する要素を None にして返す。
    """
    try:
        task_arns = ecs.list_tasks(cluster=cluster_arn, desiredStatus="RUNNING")["taskArns"]
        if not task_arns:
            return None, None, None

        tasks = ecs.describe_tasks(cluster=cluster_arn, tasks=task_arns[:1])["tasks"]
        if not tasks:
            return None, None, None

        task         = tasks[0]
        task_def_arn = task.get("taskDefinitionArn")
        task_arn     = task.get("taskArn")

        # attachments から ENI ID を探す
        # type は "ElasticNetworkInterface" または "eni"（API バージョンにより異なる）
        # notify_ip.py と同じ判定ロジックに統一して /status の IP 取得漏れを防ぐ
        eni_id = None
        for attachment in task.get("attachments", []):
            if attachment.get("type") not in ("ElasticNetworkInterface", "eni"):
                continue
            for detail in attachment.get("details", []):
                if detail.get("name") == "networkInterfaceId":
                    eni_id = detail.get("value")
                    break

        if not eni_id:
            return None, task_def_arn, task_arn

        interfaces = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"]
        if not interfaces:
            return None, task_def_arn, task_arn

        public_ip = interfaces[0].get("Association", {}).get("PublicIp")
        return public_ip, task_def_arn, task_arn

    except Exception:
        logger.exception("タスク情報取得失敗")
        return None, None, None


def _get_cluster_tag(cluster_arn: str, tag_key: str):
    """クラスターの指定タグ値を返す。なければ None。"""
    try:
        clusters = ecs.describe_clusters(clusters=[cluster_arn], include=["TAGS"])["clusters"]
        if not clusters:
            return None
        tags = {t["key"]: t["value"] for t in clusters[0].get("tags", [])}
        return tags.get(tag_key)
    except Exception:
        logger.exception("クラスタータグ取得失敗: %s", cluster_arn)
        return None


def _get_ssm_status(prefix: str):
    """
    SSM からゲームサーバーの受付状態・プレイヤー数・ready=1 経過時間を読み取る。

    Returns:
        (ready, players, ready_age_seconds)
        ready=True         → ゲームが接続受付中（monitor サイドカーが確認済み）
        ready=False        → まだ初期化中（または SSM 未書込み）
        players=None       → プレイヤー数不明
        ready_age_seconds  → ready=1 になってからの経過秒数。ready=False または取得失敗時は None
    """
    ready = False
    players = None
    ready_age_seconds = None

    try:
        ready_resp = ssm.get_parameter(Name=f"{prefix}/ready")
        param      = ready_resp["Parameter"]
        ready      = param["Value"] == "1"
        if ready:
            last_modified     = param["LastModifiedDate"]  # timezone-aware datetime
            ready_age_seconds = (datetime.now(timezone.utc) - last_modified).total_seconds()
    except Exception:
        # ParameterNotFound（初回起動前）は想定内、それ以外はデバッグログ
        logger.debug("SSM ready パラメータ未取得（初回起動前か権限不足）: %s/ready", prefix)

    if ready:
        try:
            players_resp = ssm.get_parameter(Name=f"{prefix}/players")
            players      = int(players_resp["Parameter"]["Value"])
        except ValueError:
            logger.warning("SSM players の値が整数ではありません: %s/players", prefix)
        except Exception:
            logger.debug("SSM players パラメータ未取得: %s/players", prefix)

    return ready, players, ready_age_seconds


def _get_notified_task(prefix: str):
    """
    IP 通知済みタスク ARN を SSM から読む。

    notify_ip Lambda が通知を送信した後に
    /ggs/<prefix>/notified_task へ記録するタスク ARN を返す。
    未通知（パラメータ未存在）の場合は None を返す。
    """
    try:
        return ssm.get_parameter(Name=f"{prefix}/notified_task")["Parameter"]["Value"]
    except Exception:
        return None
