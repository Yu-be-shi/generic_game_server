"""
backup_efs.py - EFS セーブデータの S3 バックアップ／リストア Lambda 関数

Lambda エントリーポイント。event["action"] で以下の 4 モードへディスパッチする
（各モードの詳細は対応する bf_*.py モジュールの docstring を参照）:

  - "backup"（デフォルト）  : EFS → S3 の差分バックアップ            -> bf_backup.py
  - "restore"               : zip 指定・Palworld ワールド形式リストア -> bf_restore.py
  - "restore_all"           : 全体ミラーリストア（任意データ対応）     -> bf_restore_all.py
  - "switch_slot"           : Palworld セーブデータのスロット切り替え -> bf_switch_slot.py

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、標準ライブラリのみ
前提:
  - Lambda は VPC 内に配置し、EFS アクセスポイント経由でマウント済み（/mnt/efs）
  - S3 Gateway VPC Endpoint が設定済み（NAT Gateway 不要）
"""

from bf_backup import backup_handler
from bf_config import logger
from bf_events import emit_event
from bf_restore import restore_handler
from bf_restore_all import restore_all_handler
from bf_switch_slot import list_slots_handler, switch_slot_handler


def lambda_handler(event, context):
    """Lambda エントリーポイント。実行結果を _events/ へ書き込み Discord 通知に繋げる。"""
    action = event.get("action", "backup")

    # list_slots は Discord コマンドの実行前チェック用の同期照会（RequestResponse）。
    # 結果は呼び出し元へ直接返すため _events/ 経由の Discord 通知は行わず、
    # 失敗も例外のまま同期呼び出し元へ返す（FunctionError として観測される）。
    if action == "list_slots":
        return list_slots_handler(event, context)
    # "action" キーの有無で Discord からの明示実行か EventBridge 定期実行かを判別する
    # （定期実行の成功まで毎日通知するとノイズになるため、成功通知は明示実行のみ）
    explicit = "action" in event

    try:
        result = _dispatch(action, event, context)
    except Exception as e:
        logger.exception("%s の実行に失敗", action)
        # 例外を再送出すると Lambda 非同期リトライで同じ失敗（と失敗通知）が
        # 3 回繰り返されるため、失敗イベントの通知に置き換えて正常終了する。
        # これらのアクションは冪等な単発操作であり、再実行は Discord から手動で行う。
        emit_event(action, "error", {"error": f"{type(e).__name__}: {e}"}, context)
        return {"action": action, "status": "error"}

    if explicit:
        emit_event(action, "success", result, context)
    return result


def _dispatch(action, event, context):
    if action == "restore":
        return restore_handler(event, context)
    elif action == "restore_all":
        return restore_all_handler(event, context)
    elif action == "switch_slot":
        return switch_slot_handler(event, context)
    else:
        return backup_handler()
