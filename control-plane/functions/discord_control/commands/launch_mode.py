"""launch_mode.py - /launch-mode game:<name> [mode:<spot|ondemand>]: 起動タイプを切り替える"""
import logging

import ecs_helpers
from clients import ecs, ssm
from commands.guards import require_service
from constants import (
    LAUNCH_MODE_ONDEMAND,
    LAUNCH_MODE_SPOT,
    SSM_SUFFIX_LAUNCH_MODE,
    TAG_STATUS_PARAM_PREFIX,
)
from ssm_params import ssm_put_safe

logger = logging.getLogger()

_MODE_LABEL = {
    LAUNCH_MODE_SPOT: "⚡ Spot（約7割引・稀に中断あり）",
    LAUNCH_MODE_ONDEMAND: "🛡 通常（オンデマンド・安定）",
}


def cmd_launch_mode(game_name: str, mode: str = "") -> str:
    """
    /launch-mode game:<name> [mode:<spot|ondemand>]: Fargate の起動タイプを切り替える。

    設定は SSM の /ggs/<prefix>/launch_mode に保持され、**次回の /start から**適用される
    （/start が update_service の capacityProviderStrategy として反映する。
    ecs.tf 側は ignore_changes で起動モードを Terraform 管理外にしている）。
    稼働中のサーバーには影響しない。mode 省略時は現在の設定を表示する。
    """
    cluster_arn, service_arn, err = require_service(game_name)
    if err:
        return err

    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, TAG_STATUS_PARAM_PREFIX)
    if not ssm_prefix:
        return (
            f"❌ **{game_name}** の {TAG_STATUS_PARAM_PREFIX} タグが見つかりません。\n"
            "`game-stack` の `terraform apply` を実行してください。"
        )

    # mode 省略 = 現在の設定を表示
    if not mode:
        current = ecs_helpers.get_launch_mode(ssm_prefix) or LAUNCH_MODE_ONDEMAND
        return (
            f"**{game_name}** の起動タイプ: {_MODE_LABEL.get(current, current)}\n"
            f"変更するには `/launch-mode game:{game_name} mode:<spot|ondemand>` を実行してください。"
        )

    if mode not in _MODE_LABEL:
        return "❌ mode は `spot` または `ondemand` を指定してください。"

    # spot 指定時はクラスターに FARGATE_SPOT が関連付いているか確認する
    # （game-stack 側の apply 前に設定して /start が失敗するのを防ぐ）
    if mode == LAUNCH_MODE_SPOT and not _cluster_has_spot(cluster_arn):
        return (
            f"❌ **{game_name}** のクラスターに FARGATE_SPOT が関連付けられていません。\n"
            "`game-stack` の `terraform apply` 後に再実行してください。"
        )

    if not ssm_put_safe(ssm, f"{ssm_prefix}{SSM_SUFFIX_LAUNCH_MODE}", mode):
        return (
            f"❌ **{game_name}** の起動タイプの保存に失敗しました。\n"
            "IAM 権限（ssm:PutParameter）を確認してください。"
        )
    logger.info("launch_mode を更新: %s%s = %s", ssm_prefix, SSM_SUFFIX_LAUNCH_MODE, mode)

    lines = [
        f"✅ **{game_name}** の起動タイプを {_MODE_LABEL[mode]} に設定しました。",
        "**次回の `/start` から適用**されます。",
    ]
    svc = ecs_helpers.describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        lines.append("現在稼働中のサーバーには影響しません。")
    if mode == LAUNCH_MODE_SPOT:
        lines.append(
            "※ Spot は AWS 都合で稀に停止します（自動再起動・IP 変更あり）。"
            "セーブデータは EFS に永続化されているため消えません。"
        )
    return "\n".join(lines)


def _cluster_has_spot(cluster_arn: str) -> bool:
    """クラスターに FARGATE_SPOT capacity provider が関連付いているかを返す。判定失敗時は False"""
    try:
        clusters = ecs.describe_clusters(clusters=[cluster_arn])["clusters"]
        return bool(clusters) and "FARGATE_SPOT" in clusters[0].get("capacityProviders", [])
    except Exception:
        logger.exception("capacityProviders の取得に失敗")
        return False
