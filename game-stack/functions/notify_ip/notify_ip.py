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
from ssm_params import ssm_get, ssm_put_safe

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform の notify_ip.tf から注入）
GAME_NAME   = os.environ["GAME_NAME"]
CLUSTER_ARN = os.environ.get("CLUSTER_ARN", "")
READY_PARAM = os.environ.get("READY_PARAM", "")
# ゲームの接続ポート（game_ports の先頭）。未設定なら IP のみ通知（後方互換）
GAME_PORT   = os.environ.get("GAME_PORT", "")
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
        _handle_stopped_event(detail)
        return

    # --- SSM パラメータ変更イベント → 起動完了通知 ---
    if source == "aws.ssm":
        _handle_ssm_ready_event(detail)
        return

    logger.warning("未知のイベント source=%s", source)


def _handle_stopped_event(detail):
    """ECS タスク停止イベント（aws.ecs, lastStatus=STOPPED）を処理し停止通知を送る"""
    # デプロイメント入れ替え（タスク定義リビジョン更新を伴う /start 等）では、
    # 旧タスクの STOPPED イベントが飛んでも新タスクでサーバーは稼働継続している。
    # その場合「完全に停止しました」は誤報になるため、代わりに稼働中タスクの
    # 接続先を通知する（旧タスクの IP を通知済みだった場合の訂正を兼ねる）。
    others = _list_other_service_tasks(detail)
    if others:
        logger.info("同一サービスに稼働中タスクが残存（%d 件）。停止通知を抑制", len(others))
        _notify_running_task_address(
            f"♻️ **{GAME_NAME}** タスク入れ替えのため旧タスクが停止しました。サーバーは稼働中です。"
        )
        return

    if send_message_safe(
        f"⚫ **{GAME_NAME}** サーバーが完全に停止しました（ECSタスク終了）。課金は発生しません。"
    ):
        logger.info("停止通知送信完了")


def _list_other_service_tasks(detail):
    """
    停止イベントのタスクと同じ ECS サービスで稼働中（desiredStatus=RUNNING）の
    他タスク ARN 一覧を返す。サービス管理外のタスク（run_task 等）や
    ListTasks 失敗時は [] を返す（= 従来どおり停止通知を送る安全側動作）。
    """
    group = detail.get("group", "")
    if not group.startswith("service:") or not CLUSTER_ARN:
        return []
    try:
        arns = ecs.list_tasks(
            cluster=CLUSTER_ARN,
            serviceName=group[len("service:"):],
            desiredStatus="RUNNING",
        )["taskArns"]
    except Exception:
        logger.exception("list_tasks 失敗。従来どおり停止通知を送信します")
        return []
    stopped_arn = detail.get("taskArn", "")
    return [arn for arn in arns if arn != stopped_arn]


def _handle_ssm_ready_event(detail):
    """SSM ready パラメータ変更イベント（aws.ssm）を処理し起動完了通知を送る"""
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

    _notify_running_task_address(f"🟢 **{GAME_NAME}** サーバーが接続可能になりました！")


def _notify_running_task_address(header):
    """実行中タスクの接続先（IP:ポート）を通知する。同一タスクへの重複通知は排除する"""
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

    # ゲームクライアントにそのまま貼れるよう IP:ポート を 1 つのコード片にする
    address = f"{public_ip}:{GAME_PORT}" if GAME_PORT else public_ip
    message = f"{header}\n接続先: `{address}`"
    if not send_message_safe(message):
        return
    logger.info("接続先通知送信完了: IP=%s task_arn=%s", public_ip, task_arn)

    # 通知済みタスク ARN を記録（次回の重複排除に使用）
    if NOTIFIED_PARAM and task_arn:
        if not ssm_put_safe(ssm, NOTIFIED_PARAM, task_arn):
            logger.warning("notified_task の記録に失敗（次回重複する可能性あり）")


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
