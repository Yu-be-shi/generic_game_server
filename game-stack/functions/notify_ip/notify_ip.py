"""
notify_ip.py - ゲームサーバー起動時にパブリック IP を通知する Lambda 関数

トリガー:
  [起動通知] Amazon EventBridge（SSM Parameter Store Change）
             → monitor サイドカーが /ready パラメータを "1" にした時
  [停止通知] Amazon EventBridge（ECS Task State Change → lastStatus=STOPPED）

処理（起動通知）:
  1. SSM パラメータの値を取得し "1" であることを確認（EventBridge は値を含まないため）
  2. ECS の実行中タスクから ENI のパブリック IP を取得
  3. notifier.send_message() で「サーバーが接続可能になりました。IP: XX.XX.XX.XX」を送信

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、urllib（標準ライブラリ）のみ
メッセージング: notifier.py（共有モジュール）経由でツール非依存に送信する
"""

import json
import logging
import os

import boto3

from notifier import send_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform の notifications.tf から注入）
GAME_NAME   = os.environ["GAME_NAME"]
CLUSTER_ARN = os.environ.get("CLUSTER_ARN", "")
READY_PARAM = os.environ.get("READY_PARAM", "")
# 最後に通知したタスク ARN を記録するパラメータ名（同一タスクへの重複通知を排除）
NOTIFIED_PARAM = READY_PARAM.replace("/ready", "/notified_task") if READY_PARAM else ""

AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

ec2 = boto3.client("ec2", region_name=AWS_REGION)
ecs = boto3.client("ecs", region_name=AWS_REGION)
ssm = boto3.client("ssm", region_name=AWS_REGION)


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    logger.info("受信イベント: %s", json.dumps(event, ensure_ascii=False))

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
        try:
            send_message(
                f"⚫ **{GAME_NAME}** サーバーが完全に停止しました（ECSタスク終了）。課金は発生しません。"
            )
            logger.info("停止通知送信完了")
        except Exception:
            logger.exception("停止通知の送信に失敗しました")
        return

    # --- SSM パラメータ変更イベント → 起動完了通知 ---
    if source == "aws.ssm":
        param_name = detail.get("name", "")
        logger.info("SSM 変更イベント: param=%s operation=%s", param_name, detail.get("operation"))

        # EventBridge は変更後の値を含まないため SSM API で取得する
        try:
            resp  = ssm.get_parameter(Name=param_name)
            value = resp["Parameter"]["Value"]
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
            try:
                prev = ssm.get_parameter(Name=NOTIFIED_PARAM)["Parameter"]["Value"]
                if prev == task_arn:
                    logger.info("同一 task_arn のためスキップ: %s", task_arn)
                    return
            except ssm.exceptions.ParameterNotFound:
                pass  # 初回通知

        message = (
            f"🟢 **{GAME_NAME}** サーバーが接続可能になりました！\n"
            f"IP アドレス: `{public_ip}`"
        )
        try:
            send_message(message)
            logger.info("起動通知送信完了: IP=%s task_arn=%s", public_ip, task_arn)
        except Exception:
            logger.exception("通知中にエラーが発生しました")
            return

        # 通知済みタスク ARN を記録（次回の重複排除に使用）
        if NOTIFIED_PARAM and task_arn:
            try:
                ssm.put_parameter(Name=NOTIFIED_PARAM, Value=task_arn, Type="String", Overwrite=True)
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
        task_arns = ecs.list_tasks(cluster=CLUSTER_ARN, desiredStatus="RUNNING")["taskArns"]
        if not task_arns:
            logger.warning("実行中タスクが見つかりません: cluster=%s", CLUSTER_ARN)
            return None

        tasks = ecs.describe_tasks(cluster=CLUSTER_ARN, tasks=task_arns[:1])["tasks"]
        if not tasks:
            return None

        task     = tasks[0]
        task_arn = task.get("taskArn", "")

        # attachments から ENI ID を探す
        eni_id = None
        for attachment in task.get("attachments", []):
            if attachment.get("type") not in ("ElasticNetworkInterface", "eni"):
                continue
            for detail in attachment.get("details", []):
                if detail.get("name") == "networkInterfaceId":
                    eni_id = detail.get("value")
                    break
            if eni_id:
                break

        if not eni_id:
            logger.warning("ENI ID が見つかりません。attachments: %s", json.dumps(task.get("attachments", [])))
            return None

        logger.info("ENI ID: %s task_arn: %s", eni_id, task_arn)

        interfaces = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"]
        if not interfaces:
            logger.warning("ENI %s が見つかりません", eni_id)
            return None

        public_ip = interfaces[0].get("Association", {}).get("PublicIp")
        if not public_ip:
            logger.warning("パブリック IP が見つかりません。ENI: %s", eni_id)
            return None
        return public_ip, task_arn

    except Exception:
        logger.exception("タスク情報取得失敗")
        return None
