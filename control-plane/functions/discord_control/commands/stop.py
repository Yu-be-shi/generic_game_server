"""stop.py - /stop game:<name>: ゲームサーバーを停止する"""
from clients import ecs
from commands.guards import require_service


def cmd_stop(game_name: str) -> str:
    """/stop game:<name>: サーバーを停止する"""
    cluster_arn, service_arn, err = require_service(game_name)
    if err:
        return err

    ecs.update_service(cluster=cluster_arn, service=service_arn, desiredCount=0)
    return f"🛑 **{game_name}** の停止処理を開始しました。完全停止後に通知します。"
