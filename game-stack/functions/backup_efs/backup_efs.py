"""
backup_efs.py - EFS セーブデータを S3 へ定期バックアップする Lambda 関数

トリガー: Amazon EventBridge（24時間ごと）
処理:
  1. EFS マウントパス（/mnt/efs）以下を再帰的に走査
  2. S3 上の同名オブジェクトとサイズを比較し、変化があるファイルのみアップロード
     （未変更ファイルをスキップして実行時間とバージョン乱発を抑制）
  3. アップロード結果をログに出力

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、標準ライブラリのみ
前提:
  - Lambda は VPC 内に配置し、EFS アクセスポイント経由でマウント済み（/mnt/efs）
  - S3 Gateway VPC Endpoint が設定済み（NAT Gateway 不要）
"""

import logging
import os
import pathlib

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform から注入）
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
BACKUP_PREFIX = os.environ["BACKUP_PREFIX"]

# EFS マウントパス（file_system_config.local_mount_path と一致させる）
EFS_MOUNT_PATH = pathlib.Path("/mnt/efs")

s3 = boto3.client("s3")


def lambda_handler(event, context):
    """Lambda エントリーポイント"""
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

    S3 に存在しない場合、または取得に失敗した場合は False を返し
    アップロードを行う（安全側に倒した設計）。
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
