"""
cost_guard.py - 長時間稼働タスクの強制停止バックストップ

トリガー: EventBridge スケジュール（rate(1 hour)）

監視サイドカー（auto_shutdown.sh）が落ちた場合の独立したバックストップ。
MAX_RUNTIME_HOURS を超えて RUNNING なタスクを検出した場合:
  1. サービスタスクなら update-service --desired-count 0（再起動防止）
  2. stop_task で強制停止（孤児更新タスクも含む）
  3. Discord/Slack に通知

通常プレイ中には発火しないよう MAX_RUNTIME_HOURS のデフォルトは 24 時間。
このバックストップはアイドル自動停止の代替ではなく最終安全網。
"""

import logging
import os
from datetime import datetime, timezone

from aws_clients import client as _aws_client
from notifier import send_message_safe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ecs = _aws_client("ecs")

SECONDS_PER_HOUR = 3600
# ECS describe_tasks の最大タスク数 / リクエスト（API 制限）
DESCRIBE_TASKS_BATCH = 100


def lambda_handler(event, context):
    cluster_arn = os.environ["CLUSTER_ARN"]
    service_name = os.environ["SERVICE_NAME"]
    max_runtime_hours = float(os.environ.get("MAX_RUNTIME_HOURS", "24"))
    game_name = os.environ.get("GAME_NAME", service_name)

    try:
        stopped = _check_and_stop(cluster_arn, service_name, max_runtime_hours, game_name)
        if stopped:
            logger.info("Stopped %d long-running task(s): %s", len(stopped), stopped)
        else:
            logger.info("No long-running tasks detected (threshold: %.0fh).", max_runtime_hours)
    except Exception as exc:
        # バックストップ自身が落ちないようにトップレベルはログのみ
        logger.error("cost_guard unhandled error (non-fatal): %s", exc, exc_info=True)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 内部ロジック
# ---------------------------------------------------------------------------

def _check_and_stop(cluster_arn, service_name, max_runtime_hours, game_name):
    """
    クラスター内の RUNNING タスクを検査し、閾値超過タスクを強制停止する。
    戻り値: 停止した task ARN のリスト
    """
    # 経過時間の基準時刻はタスク列挙前に固定する
    now = datetime.now(timezone.utc)

    tasks = _list_running_tasks(cluster_arn)
    if not tasks:
        return []

    stopped, service_stop_needed = _stop_expired_tasks(
        tasks, cluster_arn, service_name, max_runtime_hours, now
    )
    if service_stop_needed:
        _prevent_service_restart(cluster_arn, service_name)
    if stopped:
        _notify_stopped(game_name, max_runtime_hours, len(stopped))
    return stopped


def _list_running_tasks(cluster_arn):
    """RUNNING タスクを全列挙し describe_tasks の詳細を返す。"""
    task_arns = []
    paginator = ecs.get_paginator("list_tasks")
    for page in paginator.paginate(cluster=cluster_arn, desiredStatus="RUNNING"):
        task_arns.extend(page["taskArns"])

    tasks = []
    for i in range(0, len(task_arns), DESCRIBE_TASKS_BATCH):
        resp = ecs.describe_tasks(
            cluster=cluster_arn, tasks=task_arns[i : i + DESCRIBE_TASKS_BATCH]
        )
        tasks.extend(resp.get("tasks", []))
    return tasks


def _stop_expired_tasks(tasks, cluster_arn, service_name, max_runtime_hours, now):
    """
    閾値超過タスクを stop_task で停止する。
    戻り値: (停止した task ARN のリスト, サービスタスクを停止したか)
    """
    threshold_seconds = max_runtime_hours * SECONDS_PER_HOUR
    stopped = []
    service_stop_needed = False

    for task in tasks:
        started_at = task.get("startedAt")
        if not started_at:
            # まだ起動中（PROVISIONING / PENDING 等）はスキップ
            continue

        elapsed_seconds = (now - started_at).total_seconds()
        if elapsed_seconds <= threshold_seconds:
            continue

        task_arn = task["taskArn"]
        elapsed_hours = elapsed_seconds / SECONDS_PER_HOUR
        task_group = task.get("group", "")
        is_service_task = task_group == f"service:{service_name}"

        logger.warning(
            "Long-running task detected: %s (elapsed=%.1fh, group=%s)",
            task_arn,
            elapsed_hours,
            task_group,
        )

        if is_service_task:
            service_stop_needed = True

        try:
            ecs.stop_task(
                cluster=cluster_arn,
                task=task_arn,
                reason=(
                    f"cost_guard: running for {elapsed_hours:.1f}h "
                    f"> {max_runtime_hours:.0f}h limit"
                ),
            )
            stopped.append(task_arn)
            logger.info("Stopped task: %s", task_arn)
        except Exception:
            logger.exception("Failed to stop task %s", task_arn)

    return stopped, service_stop_needed


def _prevent_service_restart(cluster_arn, service_name):
    """desiredCount=0 にしてサービスによるタスク再起動を防ぐ。
    （監視サイドカーの do_shutdown と同等の最終手段）"""
    try:
        ecs.update_service(
            cluster=cluster_arn,
            service=service_name,
            desiredCount=0,
        )
        logger.info("Set service %s desiredCount=0 to prevent restart.", service_name)
    except Exception:
        logger.exception("Failed to set desiredCount=0 for service %s", service_name)


def _notify_stopped(game_name, max_runtime_hours, stopped_count):
    """強制停止を Discord/Slack に通知する。"""
    content = (
        f"⚠️ **{game_name} コストガード発動**\n"
        f"```\n"
        f"{max_runtime_hours:.0f}時間以上稼働しているタスクを検出し、強制停止しました。\n"
        f"停止タスク数: {stopped_count}\n"
        f"\n"
        f"監視サイドカーが正常に動作しているか CloudWatch Logs で確認してください。\n"
        f"```"
    )
    send_message_safe(content)
