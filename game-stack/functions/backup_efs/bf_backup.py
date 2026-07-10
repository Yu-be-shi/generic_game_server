"""
bf_backup.py - backup アクション（EFS -> S3 差分同期）

【バックアップモード（デフォルト）】
  トリガー: Amazon EventBridge（24時間ごと）
  処理:
    1. EFS マウントパス（/mnt/efs）以下を再帰的に走査
    2. S3 上の同名オブジェクトとサイズを比較し、変化があるファイルのみアップロード
"""

import pathlib

from botocore.exceptions import ClientError

from bf_config import BACKUP_BUCKET, BACKUP_PREFIX, EFS_MOUNT_PATH, logger, s3


def backup_handler():
    """EFS → S3 バックアップ（差分同期）"""
    logger.info(
        "バックアップ開始: EFS=%s -> s3://%s/%s/",
        EFS_MOUNT_PATH,
        BACKUP_BUCKET,
        BACKUP_PREFIX,
    )

    stats = {"uploaded": 0, "skipped": 0, "failed": 0}

    if not EFS_MOUNT_PATH.exists():
        logger.warning("EFS マウントパス %s が存在しません。バックアップをスキップします。", EFS_MOUNT_PATH)
        return stats

    for local_path in EFS_MOUNT_PATH.rglob("*"):
        if not local_path.is_file():
            continue

        # S3 キーを構築: <BACKUP_PREFIX>/<EFS内の相対パス>
        relative = local_path.relative_to(EFS_MOUNT_PATH)
        s3_key = f"{BACKUP_PREFIX}/{relative}"

        try:
            if _is_unchanged(local_path, s3_key):
                logger.debug("スキップ（未変更）: %s", s3_key)
                stats["skipped"] += 1
                continue

            s3.upload_file(str(local_path), BACKUP_BUCKET, s3_key)
            logger.info("アップロード完了: %s -> s3://%s/%s", local_path, BACKUP_BUCKET, s3_key)
            stats["uploaded"] += 1

        except Exception:
            logger.exception("アップロード失敗: %s", local_path)
            stats["failed"] += 1

    logger.info(
        "バックアップ完了: アップロード=%d, スキップ=%d, 失敗=%d",
        stats["uploaded"],
        stats["skipped"],
        stats["failed"],
    )
    return stats


def _is_unchanged(local_path: pathlib.Path, s3_key: str) -> bool:
    """
    ローカルファイルと S3 オブジェクトのサイズを比較し、
    同じサイズなら「未変更」と判断して True を返す。
    """
    try:
        head = s3.head_object(Bucket=BACKUP_BUCKET, Key=s3_key)
        s3_size = head["ContentLength"]
        local_size = local_path.stat().st_size
        return s3_size == local_size
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False  # S3 に存在しない → アップロード要
        logger.warning("head_object 失敗（アップロードを実行します）: %s - %s", s3_key, e)
        return False
