"""
bf_restore.py - restore アクション（zip 指定・Palworld ワールド形式）

【リストアモード（zip 指定・Palworld ワールド形式）】
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
"""

import pathlib

from bf_config import BACKUP_BUCKET, BACKUP_PREFIX, EFS_MOUNT_PATH, SAVEGAMES_REL, logger, s3
from bf_palworld import _extract_world, _prepare_world_dir


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

    # 2-4. 既存ワールドフォルダ（OLD_GUID）を特定 → スナップショット退避 → クリア
    #      （見つからない場合は source_world 名でフォルダを新規作成）
    snapshot_prefix = f"{BACKUP_PREFIX}/_pre_restore_snapshot/{exec_id}"
    old_guid_dir, _, _ = _prepare_world_dir(
        savegames_root,
        fallback_name=source_world,
        snapshot_prefix=snapshot_prefix,
        swallow_errors=False,
        log_detected=lambda d: logger.info("既存ワールドフォルダを検出: %s", d),
        log_snapshot=lambda n: logger.info(
            "スナップショット完了: %d ファイル -> s3://%s/%s/",
            n, BACKUP_BUCKET, snapshot_prefix,
        ),
        log_clear=lambda d: logger.info("既存フォルダをクリア完了: %s", d),
    )

    # 5. zip から対象ファイルを展開
    stats = _extract_world(local_zip, source_world, old_guid_dir)

    logger.info(
        "リストア完了: 展開=%d, スキップ=%d, 配置先=%s",
        stats["extracted"], stats["skipped"], old_guid_dir,
    )
    return {"action": "restore", "destination": str(old_guid_dir), **stats}
