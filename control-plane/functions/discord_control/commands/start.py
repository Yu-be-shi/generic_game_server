"""start.py - /start game:<name>: ゲームサーバーを起動する"""
import logging

import ecs_helpers
from clients import ecs, ssm
from commands.guards import is_under_maintenance, require_service
from ssm_params import ssm_put

logger = logging.getLogger()


def cmd_start(game_name: str) -> str:
    """/start game:<name>: サーバーを起動する"""
    cluster_arn, service_arn, err = require_service(game_name)
    if err:
        return err

    svc = ecs_helpers.describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        return (
            f"ℹ️ **{game_name}** はすでに起動中（または起動処理中）です。\n"
            f"`/status game:{game_name}` で IP を確認できます。"
        )

    # 自動アップデート実行中は起動を拒否（EFS install への二重書き込み防止）
    if is_under_maintenance(cluster_arn):
        return (
            f"🔧 **{game_name}** はメンテナンス中（自動アップデート実行中）です。\n"
            "数分後に完了通知が届きます。それからお試しください。"
        )

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
            latest_task_def_arn = ecs_helpers.get_latest_task_def_arn(family)

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
    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, "StatusParamPrefix")
    if ssm_prefix:
        try:
            ssm_put(ssm, f"{ssm_prefix}/ready", "0")
            logger.info("/start: SSM ready を 0 にリセット: %s/ready", ssm_prefix)
        except Exception:
            logger.warning("/start: SSM ready のリセットに失敗（権限確認を）: %s/ready", ssm_prefix)

    return (
        f"✅ **{game_name}** の起動を開始しました！\n"
        f"接続可能になったら IP が通知されます 📨"
    )
