"""
bf_events.py - 実行結果イベントの S3 書き込み

backup_efs Lambda は VPC 内（NAT なし・S3 Gateway エンドポイントのみ）で動作し、
Discord webhook へ直接 POST できない（docs/troubleshooting/vpc-lambda-cannot-reach-ssm.md）。
そこで処理結果を S3 の <BACKUP_PREFIX>/_events/ へ JSON で書き込み、
S3 イベント通知経由で VPC 外の notify_backup Lambda に Discord への送信を委譲する。

_events/ 配下は S3 ライフサイクルルールで短期削除される（backup.tf 参照）。
"""

import json
from datetime import datetime, timezone

from bf_config import BACKUP_BUCKET, BACKUP_PREFIX, logger, s3


def emit_event(action: str, status: str, detail: dict, context) -> None:
    """
    処理結果イベントを S3 へ書き込む（fire-and-forget）。

    通知はあくまで補助機能のため、書き込みに失敗しても本処理は失敗させない
    （ログに残すだけで継続する）。

    Args:
        action: 実行したアクション名（backup / restore / restore_all / switch_slot）
        status: "success" または "error"
        detail: ハンドラの戻り値（成功時）またはエラー情報（失敗時）
        context: Lambda コンテキスト（request_id をイベントキーの一意化に使う）
    """
    key = f"{BACKUP_PREFIX}/_events/{context.aws_request_id}-{status}.json"
    body = {
        "action": action,
        "status": status,
        "detail": detail,
        "request_id": context.aws_request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        s3.put_object(
            Bucket=BACKUP_BUCKET,
            Key=key,
            Body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("結果イベント書き込み完了: s3://%s/%s", BACKUP_BUCKET, key)
    except Exception:
        logger.exception("結果イベント書き込み失敗（継続）: %s", key)
