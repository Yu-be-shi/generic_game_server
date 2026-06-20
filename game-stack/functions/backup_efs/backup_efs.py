"""
backup_efs.py - EFS セーブデータの S3 バックアップ／リストア Lambda 関数

【バックアップモード（デフォルト）】
  トリガー: Amazon EventBridge（24時間ごと）
  処理:
    1. EFS マウントパス（/mnt/efs）以下を再帰的に走査
    2. S3 上の同名オブジェクトとサイズを比較し、変化があるファイルのみアップロード

【リストアモード】
  トリガー: aws lambda invoke による手動実行
  処理:
    1. S3 上の zip ファイルを /tmp へダウンロード
    2. EFS の SaveGames/0/ 内にある既存ワールドフォルダ（OLD_GUID）を特定
    3. 既存フォルダを S3 スナップショットとして退避（ロールバック用）
    4. OLD_GUID フォルダの中身を削除
    5. zip 内の指定ワールドの .sav / Players/*.sav を OLD_GUID フォルダへ展開
       ※ LocalData.sav はダディケーテッドサーバーでは不要のためスキップ
  イベント例:
    {
      "action": "restore",
      "s3_key": "save.zip",
      "source_world": "1D01670C455B39AD23DC8B8B6F1969CB"
    }

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、標準ライブラリのみ
前提:
  - Lambda は VPC 内に配置し、EFS アクセスポイント経由でマウント済み（/mnt/efs）
  - S3 Gateway VPC Endpoint が設定済み（NAT Gateway 不要）
"""

import logging
import os
import pathlib
import shutil
import zipfile

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform から注入）
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
BACKUP_PREFIX = os.environ["BACKUP_PREFIX"]

# EFS マウントパス（file_system_config.local_mount_path と一致させる）
EFS_MOUNT_PATH = pathlib.Path("/mnt/efs")

# Palworld ダディケーテッドサーバーのセーブ保存先（EFS マウント起点からの相対パス）
SAVEGAMES_REL = pathlib.Path("Pal/Saved/SaveGames/0")

# リストア対象とするファイル名（ルート直下の .sav）
# LocalData.sav は単機ローカル専用のため除外
SKIP_FILES = {"LocalData.sav"}

s3 = boto3.client("s3")


# ============================================================
# エントリーポイント
# ============================================================

def lambda_handler(event, context):
    """Lambda エントリーポイント"""
    action = event.get("action", "backup")

    if action == "restore":
        return restore_handler(event, context)
    else:
        return backup_handler()


# ============================================================
# バックアップモード
# ============================================================

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


# ============================================================
# リストアモード
# ============================================================

def restore_handler(event, context):
    """
    S3 の zip ファイルを取得し、EFS の SaveGames フォルダへ展開するリストアハンドラ。

    既存ワールドフォルダの「名前」を保ったまま「中身」を入れ替える設計:
      - サーバーは DedicatedServerName（=OLD_GUID のフォルダ名）でワールドを参照する
      - フォルダ名を変えず中身を差し替えることで GameUserSettings.ini の編集が不要になる
    """
    # s3_key と source_world は必須。デフォルト値を設けない（デフォルトがあると
    # 引数なし誤 invoke で _clear_directory が実行されてデータが消える危険がある）
    s3_key = event.get("s3_key")
    source_world = event.get("source_world")
    if not s3_key:
        raise ValueError(
            "restore アクションには 's3_key' フィールドが必要です。"
            "例: {\"action\": \"restore\", \"s3_key\": \"save.zip\", \"source_world\": \"<GUID>\"}"
        )
    if not source_world:
        raise ValueError(
            "restore アクションには 'source_world' フィールドが必要です。"
            "例: {\"action\": \"restore\", \"s3_key\": \"save.zip\", \"source_world\": \"<GUID>\"}"
        )
    # context.aws_request_id をスナップショットのユニーク ID として使用
    exec_id = context.aws_request_id

    savegames_root = EFS_MOUNT_PATH / SAVEGAMES_REL
    logger.info(
        "リストア開始: s3://%s/%s (source_world=%s) -> %s",
        BACKUP_BUCKET, s3_key, source_world, savegames_root,
    )

    # 1. zip を /tmp へダウンロード（Lambda /tmp は 512MB）
    local_zip = pathlib.Path(f"/tmp/{pathlib.Path(s3_key).name}")
    logger.info("zip ダウンロード中: s3://%s/%s -> %s", BACKUP_BUCKET, s3_key, local_zip)
    s3.download_file(BACKUP_BUCKET, s3_key, str(local_zip))
    logger.info("zip ダウンロード完了: %d bytes", local_zip.stat().st_size)

    # 2. 既存ワールドフォルダ（OLD_GUID）を特定
    old_guid_dir = _find_existing_world(savegames_root)

    if old_guid_dir is None:
        # SaveGames/0 が存在しないか Level.sav が見つからない場合は
        # source_world 名でフォルダを新規作成する
        savegames_root.mkdir(parents=True, exist_ok=True)
        old_guid_dir = savegames_root / source_world
        old_guid_dir.mkdir(exist_ok=True)
        logger.info("既存ワールドなし。新規フォルダを作成: %s", old_guid_dir)
    else:
        logger.info("既存ワールドフォルダを検出: %s", old_guid_dir)

        # 3. 安全スナップショット（S3 へ退避）
        _snapshot_to_s3(old_guid_dir, exec_id)

        # 4. 既存フォルダの中身を削除
        _clear_directory(old_guid_dir)
        logger.info("既存フォルダをクリア完了: %s", old_guid_dir)

    # 5. zip から対象ファイルを展開
    stats = _extract_world(local_zip, source_world, old_guid_dir)

    logger.info(
        "リストア完了: 展開=%d, スキップ=%d, 配置先=%s",
        stats["extracted"], stats["skipped"], old_guid_dir,
    )
    return {"action": "restore", "destination": str(old_guid_dir), **stats}


def _find_existing_world(savegames_root: pathlib.Path):
    """
    SaveGames/0/ 内で Level.sav を含むフォルダを返す。
    複数ある場合は最終更新時刻が新しいものを選択しログに警告を出す。
    """
    if not savegames_root.exists():
        return None

    candidates = []
    for d in savegames_root.iterdir():
        if d.is_dir() and (d / "Level.sav").exists():
            mtime = (d / "Level.sav").stat().st_mtime
            candidates.append((mtime, d))

    if not candidates:
        logger.info("SaveGames/0 に Level.sav を含むフォルダが見つかりませんでした: %s", savegames_root)
        return None

    candidates.sort(reverse=True)
    if len(candidates) > 1:
        names = [str(c[1].name) for c in candidates]
        logger.warning("複数のワールドフォルダを検出。最新更新を採用: %s", names)

    return candidates[0][1]


def _snapshot_to_s3(world_dir: pathlib.Path, exec_id: str):
    """
    既存ワールドフォルダを S3 のスナップショット領域へ退避する。
    パス: s3://<BACKUP_BUCKET>/<BACKUP_PREFIX>/_pre_restore_snapshot/<exec_id>/...
    ロールバック時はこのパスからファイルを手動で戻す。
    """
    snapshot_prefix = f"{BACKUP_PREFIX}/_pre_restore_snapshot/{exec_id}"
    uploaded = 0
    for f in world_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(world_dir)
        key = f"{snapshot_prefix}/{rel}"
        s3.upload_file(str(f), BACKUP_BUCKET, key)
        uploaded += 1

    logger.info(
        "スナップショット完了: %d ファイル -> s3://%s/%s/",
        uploaded, BACKUP_BUCKET, snapshot_prefix,
    )


def _clear_directory(directory: pathlib.Path):
    """ディレクトリの中身を削除する（ディレクトリ自体は残す）。"""
    for item in directory.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def _extract_world(
    local_zip: pathlib.Path,
    source_world: str,
    dest_dir: pathlib.Path,
) -> dict:
    """
    zip 内の source_world 配下から対象ファイルを dest_dir へ展開する。

    対象:
      - <source_world>/*.sav       （Level.sav, LevelMeta.sav, WorldOption.sav 等）
      - <source_world>/Players/*.sav

    除外:
      - LocalData.sav（ダディケーテッドサーバー非対応のため）
      - ディレクトリエントリ
    """
    stats = {"extracted": 0, "skipped": 0}
    marker = f"{source_world}/"  # zip 内でワールドフォルダを示すマーカー

    with zipfile.ZipFile(local_zip) as zf:
        for member in zf.infolist():
            name = member.filename

            # source_world 配下のエントリを探す（プレフィックス部分は無視）
            idx = name.find(marker)
            if idx == -1:
                continue

            # source_world/ 以降のパス（例: "Level.sav" / "Players/xxx.sav"）
            relative = name[idx + len(marker):]

            # ディレクトリエントリや空パスはスキップ
            if not relative or member.is_dir():
                continue

            rel_path = pathlib.PurePosixPath(relative)
            parts = rel_path.parts

            # 除外リスト（LocalData.sav など）
            if rel_path.name in SKIP_FILES:
                logger.info("スキップ（ダディケーテッド非対応）: %s", relative)
                stats["skipped"] += 1
                continue

            # ルート直下の .sav か Players/<name>.sav のみ対象
            is_root_sav = (len(parts) == 1 and relative.endswith(".sav"))
            is_players_sav = (
                len(parts) == 2
                and parts[0] == "Players"
                and relative.endswith(".sav")
            )
            if not (is_root_sav or is_players_sav):
                logger.debug("対象外エントリをスキップ: %s", relative)
                stats["skipped"] += 1
                continue

            # 展開先パスを構築する
            dest_path = dest_dir / relative

            # Zip-Slip 対策: 展開先が dest_dir 配下であることを検証する
            # 悪意ある zip（"../../etc/passwd" 等）によるパストラバーサルを防ぐ
            try:
                dest_path.resolve().relative_to(dest_dir.resolve())
            except ValueError:
                logger.warning("パストラバーサルを検出してスキップ: %s", relative)
                stats["skipped"] += 1
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(zf.read(member.filename))
            logger.info("展開: %s -> %s", relative, dest_path)
            stats["extracted"] += 1

    return stats
