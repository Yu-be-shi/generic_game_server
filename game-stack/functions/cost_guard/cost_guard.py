"""
cost_guard.py - 長時間稼働タスクの強制停止バックストップ

トリガー: EventBridge スケジュール（rate(1 hour)）

監視サイドカー（auto_shutdown.sh）が落ちた場合の独立したバックストップ。
MAX_RUNTIME_HOURS を超えて RUNNING なタスクを検出した場合:
  1. サービスタスクなら update-service --desired-count 0（再起動防止）
  2. stop_task で強制停止（孤児更新タスクも含む）
  3. Discord/Slack に通知

通常プレイ中には発火しないよう MAX_RUNTIME_HOURS のデフォルトは 12 時間。
このバックストップはアイドル自動停止の代替ではなく最終安全網。
"""

import logging
import os
from datetime import datetime, timezone

import boto3

from notifier import send_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ecs = boto3.client("ecs")


def lambda_handler(event, context):
    cluster_arn = os.environ["CLUSTER_ARN"]
    service_name = os.environ["SERVICE_NAME"]
    max_runtime_hours = float(os.environ.get("MAX_RUNTIME_HOURS", "12"))
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
    now = datetime.now(timezone.utc)
    threshold_seconds = max_runtime_hours * 3600

    # RUNNING タスク ARN を全列挙（ページング対応）
    task_arns = []
    paginator = ecs.get_paginator("list_tasks")
    for page in paginator.paginate(cluster=cluster_arn, desiredStatus="RUNNING"):
        task_arns.extend(page["taskArns"])

    if not task_arns:
        return []

    # describe_tasks は最大 100 件 / リクエスト
    tasks = []
    for i in range(0, len(task_arns), 100):
        resp = ecs.describe_tasks(cluster=cluster_arn, tasks=task_arns[i : i + 100])
        tasks.extend(resp.get("tasks", []))

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
        elapsed_hours = elapsed_seconds / 3600
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
        except Exception as exc:
            logger.error("Failed to stop task %s: %s", task_arn, exc)

    # サービスタスクを停止した場合は desiredCount=0 にして再起動を防ぐ
    # （監視サイドカーの do_shutdown と同等の最終手段）
    if service_stop_needed:
        try:
            ecs.update_service(
                cluster=cluster_arn,
                service=service_name,
                desiredCount=0,
            )
            logger.info("Set service %s desiredCount=0 to prevent restart.", service_name)
        except Exception as exc:
            logger.error("Failed to set desiredCount=0 for service %s: %s", service_name, exc)

    # Discord/Slack 通知
    if stopped:
        content = (
            f"⚠️ **{game_name} コストガード発動**\n"
            f"```\n"
            f"{max_runtime_hours:.0f}時間以上稼働しているタスクを検出し、強制停止しました。\n"
            f"停止タスク数: {len(stopped)}\n"
            f"\n"
            f"監視サイドカーが正常に動作しているか CloudWatch Logs で確認してください。\n"
            f"```"
        )
        try:
            send_message(content)
        except Exception as exc:
            logger.error("Failed to send notification: %s", exc)

    return stopped
