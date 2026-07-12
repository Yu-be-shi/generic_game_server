"""
notify_backup.py - backup_efs 実行結果の Discord/Slack 通知 Lambda

backup_efs Lambda は VPC 内（NAT なし）で webhook に到達できないため、
処理結果を S3 の <backup_prefix>/_events/ へ JSON で書き込む（bf_events.py）。
この Lambda は S3 イベント通知（ObjectCreated）で起動し、イベント JSON を
読み取って整形し、webhook（notifier.py）へ送信する。

通知フロー:
  backup_efs（VPC 内）→ S3 _events/*.json → S3 Event → notify_backup（VPC 外）→ Discord
"""

import json
import logging
import urllib.parse

import boto3

import notifier

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def lambda_handler(event, context):
    """S3 イベント通知エントリーポイント。レコードごとに結果 JSON を読み通知する。"""
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 イベントのキーは URL エンコードされている（日本語・スペース等）
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            result_event = json.loads(body)
        except Exception:
            logger.exception("結果イベントの読み取りに失敗: s3://%s/%s", bucket, key)
            continue

        message = _format_message(result_event)
        notifier.send_message_safe(message)
        logger.info("通知送信: %s", message.splitlines()[0])


def _format_message(ev: dict) -> str:
    """結果イベント JSON をアクション別の Discord メッセージへ整形する。"""
    action = ev.get("action", "不明なアクション")
    detail = ev.get("detail") or {}

    if ev.get("status") != "success":
        return (
            f"❌ **{action}** が失敗しました: {detail.get('error', '不明なエラー')}\n"
            "詳細は CloudWatch Logs（`*-backup-efs`）を確認してください。"
        )

    if action == "switch_slot":
        message = (
            f"🔀 セーブデータ切り替え完了: **{detail.get('from_slot', '?')}** → "
            f"**{detail.get('to_slot', '?')}**"
            f"（保護 {detail.get('protected', 0)} / 復元 {detail.get('restored', 0)} ファイル）"
        )
        if not detail.get("restored"):
            message += (
                "\n⚠️ 切替先スロットに保存データが無かったため、次回起動時は"
                "**新規ワールド**になります。スロット名の打ち間違いにご注意ください。"
            )
        return message

    if action == "backup":
        return (
            f"💾 バックアップ完了（アップロード {detail.get('uploaded', 0)} / "
            f"スキップ {detail.get('skipped', 0)} / 失敗 {detail.get('failed', 0)}）"
        )

    if action == "restore":
        return (
            f"♻️ リストア完了: 展開 {detail.get('extracted', 0)} / "
            f"スキップ {detail.get('skipped', 0)}"
        )

    if action == "restore_all":
        return (
            f"♻️ 全体リストア完了: 復元 {detail.get('downloaded', 0)} / "
            f"失敗 {detail.get('failed', 0)}（実行前退避 {detail.get('snapshotted', 0)}）"
        )

    # 未知のアクション（将来の追加分）もそのまま通知する
    return f"✅ {action} 完了: {json.dumps(detail, ensure_ascii=False)[:500]}"
