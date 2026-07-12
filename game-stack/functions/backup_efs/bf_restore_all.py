"""
bf_restore_all.py - restore_all アクション（全体ミラーリストア・任意データ対応）

【全体ミラーリストアモード（restore_all・任意データ対応）】
  トリガー: aws lambda invoke による手動実行（Discord /restore コマンドから非同期 invoke）
  処理:
    1. 現在の EFS マウント配下を丸ごと S3 の _pre_restore_snapshot/<exec_id>/ へ退避
    2. S3 上の BACKUP_PREFIX 配下（_pre_restore_snapshot を除く）を EFS へダウンロード
       ※ 追加・上書きのみ行い、EFS 側にしかないファイルは削除しない（安全側）
  イベント例:
    { "action": "restore_all" }
"""

from bf_config import BACKUP_BUCKET, BACKUP_PREFIX, EFS_MOUNT_PATH, logger
from bf_storage import _mirror_from_s3, _snapshot_to_s3


def restore_all_handler(event, context):
    """
    S3 の BACKUP_PREFIX 配下を丸ごと EFS へミラーリングする汎用リストアハンドラ。

    restore_handler（zip指定・Palworldワールド形式専用）と異なり、ファイル形式を
    解釈せず BACKUP_PREFIX 配下の全オブジェクトをそのまま EFS へコピーする。

    安全側の設計:
      - 実行前に現在の EFS 内容を丸ごと S3 の _pre_restore_snapshot/<exec_id>/ へ退避する
        （ロールバックしたい場合はこのプレフィックスから手動で戻す）
      - 追加・上書きのみ行い、EFS 側にしかない（S3側にはもう存在しない）ファイルは削除しない
      - _pre_restore_snapshot/ 配下自体はダウンロード対象から除外する（無限に肥大化するため）
    """
    exec_id = context.aws_request_id
    snapshot_prefix = f"{BACKUP_PREFIX}/_pre_restore_snapshot/{exec_id}"

    # 1. 現状の EFS を退避（ロールバック用）
    snapshotted = _snapshot_to_s3(EFS_MOUNT_PATH, snapshot_prefix, swallow_errors=True)

    logger.info(
        "restore_all スナップショット完了: %d ファイル -> s3://%s/%s/",
        snapshotted, BACKUP_BUCKET, snapshot_prefix,
    )

    # 2. S3 → EFS ミラーリング。EFS 由来でない管理用プレフィックスは除外する:
    #    - _pre_restore_snapshot/: ロールバック退避（無限肥大化防止）
    #    - slots/: /switch-slot のスロット保管領域（EFS に書き戻すとゴミになる）
    #    - _events/: 通知用の結果イベント JSON
    exclude_prefix = (
        f"{BACKUP_PREFIX}/_pre_restore_snapshot/",
        f"{BACKUP_PREFIX}/slots/",
        f"{BACKUP_PREFIX}/_events/",
    )
    downloaded, failed = _mirror_from_s3(
        f"{BACKUP_PREFIX}/", EFS_MOUNT_PATH, exclude_prefix=exclude_prefix
    )

    stats = {"snapshotted": snapshotted, "downloaded": downloaded, "failed": failed}
    logger.info(
        "restore_all 完了: snapshotted=%d downloaded=%d failed=%d",
        stats["snapshotted"], stats["downloaded"], stats["failed"],
    )
    return {"action": "restore_all", "snapshot_prefix": snapshot_prefix, **stats}
