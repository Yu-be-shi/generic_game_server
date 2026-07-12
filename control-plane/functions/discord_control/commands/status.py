"""status.py - /status game:<name>: 稼働状態と現在の IP アドレスを返す"""
import logging

import ecs_helpers
from commands.guards import require_service
from constants import TAG_STATUS_PARAM_PREFIX

logger = logging.getLogger()

# /status が「稼働中」に昇格するまでの猶予時間（秒）
# ready=1 になってからこの時間が経過すれば、Webhook 通知済みでなくても「稼働中」を返す。
# 正常時の Webhook（notify_ip Lambda）は数秒で完了するため、
# 猶予中に先に「接続可能になりました！」が Discord に届く。
# Webhook が万一失敗しても、60 秒後にはユーザーが /status で IP を確認できる。
STATUS_READY_GRACE_SECONDS = 60


def cmd_status(game_name: str) -> str:
    """/status game:<name>: 稼働状態と現在の IP アドレスを返す"""
    cluster_arn, service_arn, err = require_service(game_name)
    if err:
        return err

    svc = ecs_helpers.describe_service(cluster_arn, service_arn)
    if not svc:
        return "❌ サービス情報を取得できませんでした。"

    desired = svc.get("desiredCount", 0)
    running = svc.get("runningCount", 0)

    # クラスターの StatusParamPrefix タグが SSM パラメータのプレフィックスを示す
    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, TAG_STATUS_PARAM_PREFIX)

    # 使用中のセーブデータスロット（/switch-slot 未実行なら未記録 = 既定スロット）
    active_slot = ecs_helpers.get_active_slot(ssm_prefix) if ssm_prefix else None
    slot_line = f"\n使用中スロット: `{active_slot or 'default'}`"

    if desired == 0:
        return (
            f"⚫ **{game_name}** は停止中です。{slot_line}\n"
            f"`/start game:{game_name}` で起動できます。"
        )

    if running == 0:
        return f"🟡 **{game_name}** は起動処理中です。しばらくお待ちください。"

    # 実行中タスクのパブリック IP・タスク ARN を取得
    public_ip, _, task_arn = ecs_helpers.get_running_task_info(cluster_arn)
    ip_str = f"`{public_ip}`" if public_ip else "取得中..."

    # SSM からゲームサーバーの実起動状態・プレイヤー数を取得
    if ssm_prefix:
        ready, players, ready_age = ecs_helpers.get_ssm_status(ssm_prefix)
        notified = ecs_helpers.get_notified_task(ssm_prefix)
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
                f"IP アドレス: {ip_str}{slot_line}"
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
