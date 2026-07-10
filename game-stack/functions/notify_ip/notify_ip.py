"""
notify_ip.py - ゲームサーバー起動時にパブリック IP を通知する Lambda 関数

トリガー:
  [起動通知] Amazon EventBridge（SSM Parameter Store Change）
             → monitor サイドカーが /ready パラメータを "1" にした時
  [停止通知] Amazon EventBridge（ECS Task State Change → lastStatus=STOPPED）

処理（起動通知）:
  1. SSM パラメータの値を取得し "1" であることを確認（EventBridge は値を含まないため）
  2. ECS の実行中タスクから ENI のパブリック IP を取得
  3. notifier.send_message_safe() で「サーバーが接続可能になりました。IP: XX.XX.XX.XX」を送信

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、urllib（標準ライブラリ）のみ
メッセージング: notifier.py（共有モジュール）経由でツール非依存に送信する
"""

import logging
import os

from aws_clients import client as _aws_client
from ecs_net import get_running_task_public_ip
from notifier import send_message_safe
from ssm_params import ssm_get, ssm_put

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform の notify_ip.tf から注入）
GAME_NAME   = os.environ["GAME_NAME"]
CLUSTER_ARN = os.environ.get("CLUSTER_ARN", "")
READY_PARAM = os.environ.get("READY_PARAM", "")
# 最後に通知したタスク ARN を記録するパラメータ名（同一タスクへの重複通知を排除）。
# NOTIFIED_PARAM が未設定（terraform apply 前の後方互換）の場合のみ、
# READY_PARAM からの文字列置換にフォールバックする。
NOTIFIED_PARAM = os.environ.get("NOTIFIED_PARAM") or (
    READY_PARAM.replace("/ready", "/notified_task") if READY_PARAM else ""
)

ec2 = _aws_client("ec2")
ecs = _aws_client("ecs")
ssm = _aws_client("ssm")


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    # イベント全文ログは避ける（ECS/SSM イベントにアカウント情報が含まれるため）
    logger.info("受信イベント: source=%s detail-type=%s",
                event.get("source"), event.get("detail-type"))

    source = event.get("source", "")
    detail = event.get("detail", {})

    # --- 自動アップデートのワンオフタスク停止は通知を出さない ---
    # run_task で startedBy="ggs-auto-update" として起動したタスクが停止しても
    # 「サーバーが停止しました」通知を出さない（誤通知防止）
    if source == "aws.ecs" and detail.get("startedBy") == "ggs-auto-update":
        logger.info("auto-update タスクの停止イベント。通知抑制: startedBy=ggs-auto-update")
        return

    # --- ECS タスク停止イベント → 停止通知 ---
    if source == "aws.ecs" and detail.get("lastStatus") == "STOPPED":
        if send_message_safe(
            f"⚫ **{GAME_NAME}** サーバーが完全に停止しました（ECSタスク終了）。課金は発生しません。"
        ):
            logger.info("停止通知送信完了")
        return

    # --- SSM パラメータ変更イベント → 起動完了通知 ---
    if source == "aws.ssm":
        param_name = detail.get("name", "")
        logger.info("SSM 変更イベント: param=%s operation=%s", param_name, detail.get("operation"))

        # EventBridge は変更後の値を含まないため SSM API で取得する
        try:
            value = ssm_get(ssm, param_name)
        except Exception:
            logger.exception("SSM パラメータ取得失敗: %s", param_name)
            return

        if value != "1":
            # ready=0 の書き込み（タスク起動時の初期化）はスキップ
            logger.info("ready の値が '1' でないためスキップ: value=%s", value)
            return

        # 実行中タスクのパブリック IP とタスク ARN を取得
        result = get_public_ip_from_running_task()
        if result is None:
            logger.warning("パブリック IP を取得できませんでした。通知をスキップします。")
            return
        public_ip, task_arn = result

        # 同一タスクへの重複通知を排除（EventBridge at-least-once 再配信・Lambda リトライ対策）
        if NOTIFIED_PARAM and task_arn:
            prev = ssm_get(ssm, NOTIFIED_PARAM)
            if prev == task_arn:
                logger.info("同一 task_arn のためスキップ: %s", task_arn)
                return

        message = (
            f"🟢 **{GAME_NAME}** サーバーが接続可能になりました！\n"
            f"IP アドレス: `{public_ip}`"
        )
        if not send_message_safe(message):
            return
        logger.info("起動通知送信完了: IP=%s task_arn=%s", public_ip, task_arn)

        # 通知済みタスク ARN を記録（次回の重複排除に使用）
        if NOTIFIED_PARAM and task_arn:
            try:
                ssm_put(ssm, NOTIFIED_PARAM, task_arn)
            except Exception:
                logger.warning("notified_task の記録に失敗（次回重複する可能性あり）")
        return

    logger.warning("未知のイベント source=%s", source)


def get_public_ip_from_running_task():
    """
    ECS 実行中タスクのパブリック IP と タスク ARN を返す (public_ip, task_arn)。
    取得失敗時は None を返す。
    """
    if not CLUSTER_ARN:
        logger.error("CLUSTER_ARN が設定されていません")
        return None

    try:
        public_ip, _, task_arn = get_running_task_public_ip(ecs, ec2, CLUSTER_ARN)
    except Exception:
        logger.exception("タスク情報取得失敗")
        return None

    if not public_ip:
        logger.warning("パブリック IP または ENI が取得できませんでした")
        return None

    return public_ip, task_arn
