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
from bf_restore import restore_handler
from bf_restore_all import restore_all_handler
from bf_switch_slot import switch_slot_handler


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    action = event.get("action", "backup")

    if action == "restore":
        return restore_handler(event, context)
    elif action == "restore_all":
        return restore_all_handler(event, context)
    elif action == "switch_slot":
        return switch_slot_handler(event, context)
    else:
        return backup_handler()
