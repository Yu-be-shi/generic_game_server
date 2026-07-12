"""
bf_config.py - backup_efs Lambda の設定・定数・共有クライアント

環境変数の読み取り、EFS/Palworld のパス定数、boto3 クライアントを一箇所に集約する。
他の bf_*.py モジュールはすべてここから import する（このモジュール自身はローカル
import を持たない葉ノード）。
"""

import logging
import pathlib
import os
import re

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform から注入）
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
BACKUP_PREFIX = os.environ["BACKUP_PREFIX"]

# switch_slot 専用: 現在アクティブなスロット名を記録する S3 オブジェクトのキー。
# この Lambda は VPC 内（NAT なし・S3 Gateway エンドポイントのみ）で動くため
# SSM には到達できない。状態は S3 に置く。
ACTIVE_SLOT_KEY = f"{BACKUP_PREFIX}/slots/_active_slot"

# EFS マウントパス（file_system_config.local_mount_path と一致させる）
EFS_MOUNT_PATH = pathlib.Path("/mnt/efs")

# Palworld ダディケーテッドサーバーのセーブ保存先（EFS マウント起点からの相対パス）
SAVEGAMES_REL = pathlib.Path("Pal/Saved/SaveGames/0")

# リストア対象とするファイル名（ルート直下の .sav）
# LocalData.sav は単機ローカル専用のため除外
SKIP_FILES = {"LocalData.sav"}

# switch_slot 未実施の場合のデフォルトスロット名（既存セーブデータの呼び名）
DEFAULT_SLOT = "default"

SLOT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

s3 = boto3.client("s3")
