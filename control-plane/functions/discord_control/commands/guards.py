"""
guards.py - /update・/backup・/restore・/switch-slot に共通するガード列とヘルパー
"""
import json
import logging

import ecs_helpers
from clients import lambda_client, ssm
from constants import SSM_SUFFIX_MAINTENANCE, TAG_STATUS_PARAM_PREFIX
from ssm_params import ssm_get

logger = logging.getLogger()


def require_service(game_name: str):
    """
    ゲームサービスを検索する。
    見つかった場合は (cluster_arn, service_arn, None) を、
    見つからない場合は (None, None, エラーメッセージ) を返す。
    """
    cluster_arn, service_arn = ecs_helpers.find_service(game_name)
    if not cluster_arn:
        return None, None, (
            f"❌ ゲーム `{game_name}` が見つかりません。\n"
            "`/games` で利用可能なゲームを確認してください。"
        )
    return cluster_arn, service_arn, None


def reject_if_running(cluster_arn: str, service_arn: str, game_name: str, action_verb: str):
    """サービス起動中なら拒否メッセージを返す。起動中でなければ None を返す。"""
    svc = ecs_helpers.describe_service(cluster_arn, service_arn)
    if svc and svc.get("desiredCount", 0) > 0:
        return (
            f"⚠️ **{game_name}** は現在起動中です。\n"
            f"`/stop game:{game_name}` で停止してから{action_verb}してください。"
        )
    return None


def is_under_maintenance(cluster_arn: str) -> bool:
    """メンテナンス中（別の /update 等が実行中）かどうかを SSM から判定する。"""
    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, TAG_STATUS_PARAM_PREFIX)
    if not ssm_prefix:
        return False
    try:
        return ssm_get(ssm, f"{ssm_prefix}{SSM_SUFFIX_MAINTENANCE}") == "1"
    except Exception:
        return False  # パラメータ未作成（初回）または取得失敗 → メンテナンス中ではないとみなす


def require_cluster_tag(cluster_arn: str, tag_key: str, game_name: str):
    """
    クラスタータグを取得する。
    見つかった場合は (値, None) を、見つからない場合は (None, エラーメッセージ) を返す。
    """
    value = ecs_helpers.get_cluster_tag(cluster_arn, tag_key)
    if not value:
        return None, (
            f"❌ **{game_name}** の {tag_key} タグが見つかりません。\n"
            "`game-stack` の `terraform apply` を実行してください。"
        )
    return value, None


def invoke_worker_async(
    function_name: str,
    payload: dict,
    log_message: str,
    error_return: str,
    error_log_message: str,
):
    """
    Worker Lambda を非同期 invoke する（Discord 3秒制限内で即応答するため Event モード）。
    成功時は log_message をログに出し None を返す。失敗時は error_log_message を
    例外付きでログに出し、error_return をそのまま返す。
    """
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        logger.info(log_message)
        return None
    except Exception:
        logger.exception(error_log_message)
        return error_return


def guarded_worker_invoke(
    game_name: str,
    tag_key: str,
    payload: dict,
    log_message: str,
    error_return: str,
    error_log_message: str,
    success_message: str,
    require_stopped: bool = True,
    action_verb: str = "",
) -> str:
    """
    /update・/backup・/restore・/switch-slot に共通するガード列を実行する:
      1. サービス検索（require_service）
      2. require_stopped=True の場合のみ:
         a. 起動中なら拒否（reject_if_running。action_verb をメッセージに埋め込む）
         b. メンテナンス中（別の /update 実行中）なら固定メッセージで拒否
         （/backup はファイル読み取りのみで安全なため require_stopped=False で呼ぶ）
      3. tag_key のクラスタータグから Worker Lambda 名を取得（require_cluster_tag）
      4. Worker Lambda を非同期 invoke（invoke_worker_async）。
         log_message / error_log_message / success_message は worker_function が
         判明してから初めて確定するため、`{worker_function}` プレースホルダを含む
         テンプレート文字列として受け取り、ここで .format(worker_function=...) する。
         呼び出し側で game_name 等を先に f-string 展開し、`{worker_function}` だけは
         `{{worker_function}}` と二重波括弧でエスケープして残す。
    """
    cluster_arn, service_arn, err = require_service(game_name)
    if err:
        return err

    if require_stopped:
        err = reject_if_running(cluster_arn, service_arn, game_name, action_verb)
        if err:
            return err

        if is_under_maintenance(cluster_arn):
            return (
                f"🔧 **{game_name}** はすでにアップデート中です。\n"
                "完了通知が届くまでお待ちください。"
            )

    worker_function, err = require_cluster_tag(cluster_arn, tag_key, game_name)
    if err:
        return err

    err = invoke_worker_async(
        worker_function,
        payload,
        log_message=log_message.format(worker_function=worker_function),
        error_return=error_return,
        error_log_message=error_log_message.format(worker_function=worker_function),
    )
    if err:
        return err

    return success_message.format(worker_function=worker_function)
