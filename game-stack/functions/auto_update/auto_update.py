"""
auto_update.py - ゲームサーバー手動アップデート Worker Lambda

Discord /update <game> コマンドにより discord_control Lambda から非同期 invoke される。
通常の /start が使う UPDATE_ON_BOOT=false タスク定義（サービス）には一切触れず、
同一タスク定義を ecs.run_task でワンオフ起動し、UPDATE_ON_BOOT=true に上書きして
SteamCMD アップデートを実行する。アップデート完了（=ゲームポート Listen 開始）を
SSM 経由で検知したら stop_task で停止する。

【設計のポイント】
  - aws_ecs_service.game と desired_count は変更しない → /start は常に高速起動のまま
  - run_task の containerOverrides.environment は名前キーでマージ
    → UPDATE_ON_BOOT のみ上書き、他のゲーム固有 env は保持される
  - monitor サイドカーを "update_ready" param（EventBridge ルール無し）に向ける
    → 誤 IP 通知が出ない
  - monitor の SERVICE_NAME を実在しないダミーに向ける
    → monitor の do_shutdown（update-service --desired-count 0）が実サービスを止めない
  - finally で必ず stop_task を実行（ハング時の唯一の停止経路）
  - SSM maintenance=1 で /start を拒否（EFS 二重書き込み防止）

【Steam バージョン事前チェック（STEAM_APP_ID 設定時のみ）】
  run_task の前に steamcmd.net の公開 API で最新 buildid を取得し、
  SSM の installed_buildid（monitor が appmanifest から保存）と比較する。
  一致（最新）→ コンテナを起動せず数秒で完了。不一致または判定不能 → 従来どおり run_task（fail-open）。

【トリガー】
  discord_control Lambda から InvocationType="Event"（非同期）で呼ばれる。
  event: {"game_name": "<name>"}  ← control-plane から渡される（参照のみ、実際は env で動作）
"""

import json
import logging
import os
import time
import urllib.request

from aws_clients import client as _aws_client
from notifier import send_message_safe
from ssm_params import ssm_get, ssm_put_safe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------
# 環境変数（Terraform の game-stack/auto_update.tf から注入）
# ---------------------------------------------------------------
CLUSTER_ARN       = os.environ["CLUSTER_ARN"]
SERVICE_NAME      = os.environ["SERVICE_NAME"]
TASK_DEF_FAMILY   = os.environ["TASK_DEF_FAMILY"]   # = local.name_prefix
SUBNET_IDS        = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
NAME_PREFIX       = os.environ["NAME_PREFIX"]
GAME_NAME         = os.environ.get("GAME_NAME", NAME_PREFIX)

# Steam バージョンチェック設定（空文字列 = チェック無効・非 Steam 系ゲーム）
STEAM_APP_ID = os.environ.get("STEAM_APP_ID", "")   # 例: "2394010"（Palworld Dedicated Server）
STEAM_BRANCH = os.environ.get("STEAM_BRANCH", "public")

# SSM パラメータ名
READY_PARAM          = f"/ggs/{NAME_PREFIX}/ready"
UPDATE_READY_PARAM   = f"/ggs/{NAME_PREFIX}/update_ready"    # このパスには EventBridge ルール無し
UPDATE_PLAYERS_PARAM = f"/ggs/{NAME_PREFIX}/update_players"
MAINT_PARAM          = f"/ggs/{NAME_PREFIX}/maintenance"
INSTALLED_BUILDID_PARAM = f"/ggs/{NAME_PREFIX}/installed_buildid"  # monitor が appmanifest から書込む

# monitor に渡すダミーサービス名（実在しないため do_shutdown が実サービスを止めない）
DUMMY_SERVICE_NAME = f"{NAME_PREFIX}-auto-update-noop"

# run_task startedBy 識別子（notify_ip が停止通知を抑止するためのキー）
STARTED_BY = "ggs-auto-update"

# ポーリング設定
POLL_INTERVAL_S = 15      # SSM update_ready をチェックする間隔（秒）
POLL_TIMEOUT_S  = 720     # ポーリング上限（12分）。Lambda timeout(900s) より余裕を持たせる
REMAINING_TIME_THRESHOLD_MS = 60_000  # この残時間未満で打ち切り（stop_task の余裕確保）

# run_task の containerOverrides で上書き対象のコンテナ名（タスク定義との契約。値は変更不可）
GAME_CONTAINER_NAME    = "game"
MONITOR_CONTAINER_NAME = "monitor"

ecs = _aws_client("ecs")
ssm = _aws_client("ssm")


def lambda_handler(event, context):
    """
    Lambda エントリーポイント。discord_control から非同期 invoke される。

    成功時:  {"status": "done",         "taskArn": "arn:..."}
    タイムアウト: {"status": "timeout",  "taskArn": "arn:..."}
    スキップ: {"status": "skipped_running"}
    失敗:    {"status": "failed",        "error":   "..."}
    """
    logger.info("auto_update 開始: event=%s", json.dumps(event))

    # 1. ガード: サーバーが起動中またはメンテ中なら中止（EFS 二重書き込み防止）
    if _server_running():
        msg = f"⚠️ **{GAME_NAME}** はすでに起動中のためアップデートをスキップしました。"
        send_message_safe(msg)
        return {"status": "skipped_running"}

    # 2. Steam バージョン事前チェック（STEAM_APP_ID 設定時のみ）
    #    コンテナ起動前に Lambda から直接照合 → 最新なら数秒で完了（コンテナ不要）
    skip_result = _is_up_to_date()
    if skip_result:
        return skip_result

    task_arn = None
    success = False
    try:
        # 3. メンテナンスフラグ + update_ready を先行リセット（/start 拒否 & stale値排除）
        ssm_put_safe(ssm, MAINT_PARAM, "1")
        ssm_put_safe(ssm, UPDATE_READY_PARAM, "0")
        logger.info("maintenance=1, update_ready=0 を設定")

        # 4. UPDATE_ON_BOOT=true のワンオフタスクを起動
        task_arn = _run_update_task()
        logger.info("アップデートタスク起動完了: %s", task_arn)

        # 5. SSM update_ready=1（=アップデート完了 + ポート Listen）をポーリング
        success = _poll_ready(context)
        if success:
            logger.info("アップデート完了を確認（update_ready=1）")
        else:
            logger.warning("ポーリングタイムアウト（%d 秒）: アップデート未完了の可能性", POLL_TIMEOUT_S)

    except Exception:
        logger.exception("アップデート処理中に例外が発生")

    finally:
        # 6-7. 完了・タイムアウト・例外いずれでも必ず後始末する（ハング時の唯一の停止経路）
        _cleanup(task_arn)

    # 8. 完了通知
    if success:
        send_message_safe(
            f"✅ **{GAME_NAME}** のサーバーアップデートが完了しました！\n"
            f"`/start game:{GAME_NAME}` で起動できます。"
        )
        return {"status": "done", "taskArn": task_arn}
    else:
        send_message_safe(
            f"⚠️ **{GAME_NAME}** のアップデートがタイムアウトしました。\n"
            "更新が適用されていない可能性があります。CloudWatch Logs を確認してください。"
        )
        return {"status": "timeout", "taskArn": task_arn}


# ---------------------------------------------------------------
# 内部関数
# ---------------------------------------------------------------

def _is_up_to_date():
    """
    Steam バージョン事前チェック（STEAM_APP_ID 設定時のみ）。

    最新バージョンと確認できた場合はスキップ用のレスポンス dict
    （lambda_handler がそのまま return する）を返す。
    チェック対象外（STEAM_APP_ID 未設定）・判定不能・要アップデートの場合は None を返す。
    """
    if not STEAM_APP_ID:
        return None

    latest    = _latest_steam_buildid()
    installed = _installed_buildid()
    if latest and installed and latest == installed:
        msg = (
            f"✅ **{GAME_NAME}** は既に最新バージョンです（build `{installed}`）。\n"
            "更新不要のためコンテナは起動しません。"
        )
        logger.info(
            "Steam バージョンチェック: 最新 build=%s = インストール済み → スキップ", installed
        )
        send_message_safe(msg)
        return {"status": "skipped_up_to_date", "buildId": installed}

    logger.info(
        "Steam バージョンチェック: latest=%s installed=%s → アップデート実行",
        latest, installed,
    )
    return None


def _cleanup(task_arn):
    """
    完了・タイムアウト・例外いずれの場合でも lambda_handler の finally から必ず呼ばれる後始末。
    ワンオフタスクの停止（task_arn がある場合のみ）とメンテナンスフラグの解除を行う。
    stop_task の失敗は握りつぶす（タスクがすでに停止済みの可能性があるため）。
    """
    if task_arn:
        try:
            ecs.stop_task(
                cluster=CLUSTER_ARN,
                task=task_arn,
                reason="auto-update complete (managed by ggs-auto-update Lambda)",
            )
            logger.info("stop_task 完了: %s", task_arn)
        except Exception:
            logger.exception("stop_task 失敗（タスクはすでに停止済みの可能性）")

    ssm_put_safe(ssm, MAINT_PARAM, "0")
    logger.info("maintenance=0 を解除")


def _server_running() -> bool:
    """
    サービスの desiredCount > 0 か、RUNNING タスクがあれば True を返す。
    ただし自分自身が起動した update タスク（startedBy=STARTED_BY）は除外する。
    例外時はフェイルセーフとして True（= 実行しない）を返す。
    """
    try:
        svcs = ecs.describe_services(cluster=CLUSTER_ARN, services=[SERVICE_NAME]).get("services", [])
        if svcs and svcs[0].get("desiredCount", 0) > 0:
            logger.info("_server_running: desiredCount > 0 → スキップ")
            return True
    except Exception:
        logger.exception("describe_services 失敗 → 安全側でスキップ")
        return True

    try:
        task_arns = ecs.list_tasks(cluster=CLUSTER_ARN, desiredStatus="RUNNING").get("taskArns", [])
        if not task_arns:
            return False

        # startedBy で自分の update タスクを除外
        tasks = ecs.describe_tasks(cluster=CLUSTER_ARN, tasks=task_arns).get("tasks", [])
        real_running = [t for t in tasks if t.get("startedBy") != STARTED_BY]
        if real_running:
            logger.info("_server_running: 実タスクが %d 件 RUNNING → スキップ", len(real_running))
            return True
    except Exception:
        logger.exception("list_tasks/describe_tasks 失敗 → 安全側でスキップ")
        return True

    return False


def _run_update_task() -> str:
    """
    UPDATE_ON_BOOT=true のワンオフタスクを run_task で起動し、タスク ARN を返す。
    monitor サイドカーの READY_PARAM/SERVICE_NAME を上書きして
    誤通知・実サービス誤停止を防ぐ。
    """
    resp = ecs.run_task(
        cluster=CLUSTER_ARN,
        taskDefinition=TASK_DEF_FAMILY,  # family 名 → 最新 ACTIVE リビジョン
        launchType="FARGATE",
        platformVersion="LATEST",        # EFS は platform version >= 1.4 が必要
        count=1,
        startedBy=STARTED_BY,            # notify_ip が停止通知を抑止するキー
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "securityGroups": [SECURITY_GROUP_ID],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": GAME_CONTAINER_NAME,
                    "environment": [
                        # UPDATE_ON_BOOT のみ上書き。他のゲーム固有 env はマージで保持される
                        {"name": "UPDATE_ON_BOOT", "value": "true"},
                    ],
                },
                {
                    "name": MONITOR_CONTAINER_NAME,
                    "environment": [
                        # EventBridge ルール無しの param に向けて誤 IP 通知を防ぐ
                        {"name": "READY_PARAM",   "value": UPDATE_READY_PARAM},
                        {"name": "PLAYERS_PARAM", "value": UPDATE_PLAYERS_PARAM},
                        # 実在しないサービス名 → monitor の do_shutdown が実サービスを止めない
                        {"name": "SERVICE_NAME",  "value": DUMMY_SERVICE_NAME},
                        # Phase B（アイドル停止）を Lambda の窓内で発火させない
                        {"name": "IDLE_MINUTES",  "value": "999"},
                    ],
                },
            ]
        },
    )

    failures = resp.get("failures", [])
    if failures:
        raise RuntimeError(f"run_task failures: {json.dumps(failures)}")

    tasks = resp.get("tasks", [])
    if not tasks:
        raise RuntimeError("run_task: tasks が空（failures も空）")

    return tasks[0]["taskArn"]


def _poll_ready(context) -> bool:
    """
    SSM update_ready=1（=アップデート完了 + ゲームポート Listen）をポーリングする。

    - monitor の dnf install（60〜120秒）完了前は ParameterNotFound → 待機継続
    - POLL_TIMEOUT_S 到達 or Lambda 残時間不足で False を返す
    """
    deadline = time.time() + POLL_TIMEOUT_S
    elapsed = 0

    while time.time() < deadline:
        # Lambda 残時間が 60 秒未満になったら打ち切り（stop_task のための余裕を確保）
        if context.get_remaining_time_in_millis() < REMAINING_TIME_THRESHOLD_MS:
            logger.warning("Lambda 残時間不足（< 60s）のためポーリングを打ち切り")
            return False

        try:
            value = ssm_get(ssm, UPDATE_READY_PARAM)
            if value == "1":
                logger.info("update_ready=1 を確認（経過: %d 秒）", elapsed)
                return True
            elif value is None:
                # monitor の dnf install 完了前は存在しない。正常な待機状態
                logger.debug("ParameterNotFound（monitor セットアップ中）。待機継続（経過: %d 秒）", elapsed)
            else:
                logger.debug("update_ready=%s 待機中（経過: %d 秒）", value, elapsed)
        except Exception:
            logger.exception("get_parameter 失敗（継続）")

        time.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

    logger.warning("ポーリングタイムアウト: %d 秒", POLL_TIMEOUT_S)
    return False


def _latest_steam_buildid() -> "str | None":
    """
    steamcmd.net の公開 API から最新の Steam ビルドID を取得する（APIキー不要）。

    エンドポイント: https://api.steamcmd.net/v1/info/<appid>
    取得パス: data.<appid>.depots.branches.<branch>.buildid

    エラー・タイムアウト時は None を返す（fail-open: アップデートを実行）。
    """
    url = f"https://api.steamcmd.net/v1/info/{STEAM_APP_ID}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ggs-auto-update/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        buildid = (
            data.get("data", {})
                .get(STEAM_APP_ID, {})
                .get("depots", {})
                .get("branches", {})
                .get(STEAM_BRANCH, {})
                .get("buildid")
        )
        if buildid:
            logger.info("steamcmd.net: latest buildid=%s (branch=%s)", buildid, STEAM_BRANCH)
        else:
            logger.warning(
                "steamcmd.net: buildid が見つかりません（branch=%s, appid=%s）。"
                "API レスポンス構造が変化した可能性があります。",
                STEAM_BRANCH, STEAM_APP_ID,
            )
        return buildid or None
    except Exception:
        logger.warning(
            "steamcmd.net への接続に失敗しました（fail-open: アップデートを実行）", exc_info=True
        )
        return None


def _installed_buildid() -> "str | None":
    """
    SSM から monitor が保存した installed buildid を取得する。

    monitor の write_buildid() が appmanifest_<appid>.acf から読んで保存する。
    未保存（初回/manifest 未検出）の場合は None を返す（fail-open: アップデートを実行）。
    """
    try:
        value = ssm_get(ssm, INSTALLED_BUILDID_PARAM)
        if value is None:
            logger.info("installed_buildid 未保存（初回 install 前、またはマニフェスト未検出）")
            return None
        logger.info("SSM installed_buildid=%s", value)
        return value or None
    except Exception:
        logger.warning("installed_buildid の取得に失敗しました（fail-open）", exc_info=True)
        return None


