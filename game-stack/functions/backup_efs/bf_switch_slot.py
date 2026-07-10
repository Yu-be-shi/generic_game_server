"""
bf_switch_slot.py - switch_slot アクション（Palworld セーブデータ専用スロット切り替え）

【スロット切り替えモード（switch_slot・Palworld セーブデータ専用）】
  トリガー: aws lambda invoke による手動実行（Discord /switch-slot コマンドから非同期 invoke）
  処理:
    1. SSM（ACTIVE_SLOT_PARAM）から現在のスロット名を取得（未設定なら "default"）
    2. 現在のワールドフォルダ（SaveGames/0/<GUID>/）の中身を S3 の
       <BACKUP_PREFIX>/slots/<現スロット>/ へ保存（保護）
    3. ワールドフォルダの中身を削除（フォルダ自体・GUID 名は残す）
    4. S3 の <BACKUP_PREFIX>/slots/<切替先スロット>/ にデータがあれば EFS へ書き戻す
       （無ければ空のまま → 次回起動時に新規ワールドとして生成される）
    5. ACTIVE_SLOT_PARAM を切替先スロット名に更新
  EFS アクセスポイントや ECS タスク定義は一切変更しないため、terraform apply は不要。
  イベント例:
    { "action": "switch_slot", "slot": "world2" }
"""

from ssm_params import ssm_get, ssm_put

from bf_config import (
    ACTIVE_SLOT_PARAM,
    BACKUP_BUCKET,
    BACKUP_PREFIX,
    DEFAULT_SLOT,
    EFS_MOUNT_PATH,
    SAVEGAMES_REL,
    SLOT_NAME_RE,
    logger,
    ssm,
)
from bf_palworld import _prepare_world_dir
from bf_storage import _mirror_from_s3


def switch_slot_handler(event, context):
    """
    S3 上の名前付きスロット間で Palworld のセーブデータを切り替える。

    restore_all_handler と異なり EFS マウント全体（ゲーム本体含む）は対象にせず、
    ワールドフォルダ（SaveGames/0/<GUID>/）の中身のみを操作する。
    EFS アクセスポイント・ECS タスク定義は一切変更しないため terraform apply は不要。

    処理:
      1. 現在アクティブなスロット名を SSM から取得（未設定なら DEFAULT_SLOT）
      2. 現在のワールドフォルダの中身を S3 の <BACKUP_PREFIX>/slots/<現スロット>/ へ保存（保護）
      3. ワールドフォルダの中身を削除（フォルダ自体・GUID 名は残す。
         サーバーはフォルダ名でワールドを参照するため、名前を変えないことで
         GameUserSettings.ini の編集が不要になる ―― restore_handler と同じ設計）
      4. S3 の <BACKUP_PREFIX>/slots/<切替先スロット>/ にデータがあれば EFS へ書き戻す
         （無ければ空のまま → 次回起動時に新規ワールドとして生成される）
      5. SSM の ACTIVE_SLOT_PARAM を切替先スロット名に更新
    """
    new_slot = event.get("slot")
    if not new_slot:
        raise ValueError(
            "switch_slot アクションには 'slot' フィールドが必要です。"
            "例: {\"action\": \"switch_slot\", \"slot\": \"world2\"}"
        )
    if not SLOT_NAME_RE.match(new_slot):
        raise ValueError("slot は英数字・ハイフン・アンダースコアのみ使用できます。")

    current_slot = _get_active_slot()
    savegames_root = EFS_MOUNT_PATH / SAVEGAMES_REL
    stats = {"protected": 0, "restored": 0, "failed": 0}

    # 1-2. 現在のワールドフォルダを特定 → 現スロットへスナップショット退避 → クリア
    #      （見つからない場合は new_slot 名でフォルダを新規作成、保護対象なし）
    protect_prefix = f"{BACKUP_PREFIX}/slots/{current_slot}"
    world_dir, _, stats["protected"] = _prepare_world_dir(
        savegames_root,
        fallback_name=new_slot,
        snapshot_prefix=protect_prefix,
        swallow_errors=True,
        log_detected=lambda d: None,
        log_snapshot=lambda n: logger.info(
            "スロット保護完了: %s -> s3://%s/%s/ (%d ファイル)",
            current_slot, BACKUP_BUCKET, protect_prefix, n,
        ),
        log_clear=lambda d: logger.info("ワールドフォルダをクリア完了: %s", d),
    )

    # 3. 切替先スロットのデータを復元（S3 に存在する場合のみ）
    restore_prefix = f"{BACKUP_PREFIX}/slots/{new_slot}/"
    stats["restored"], stats["failed"] = _mirror_from_s3(restore_prefix, world_dir)

    # 4. アクティブスロットを更新
    _set_active_slot(new_slot)

    logger.info(
        "switch_slot 完了: %s -> %s (protected=%d, restored=%d, failed=%d)",
        current_slot, new_slot, stats["protected"], stats["restored"], stats["failed"],
    )
    return {
        "action": "switch_slot",
        "from_slot": current_slot,
        "to_slot": new_slot,
        **stats,
    }


def _get_active_slot() -> str:
    """SSM から現在アクティブなスロット名を取得する。未設定・未構成時は DEFAULT_SLOT を返す。"""
    if not ACTIVE_SLOT_PARAM:
        return DEFAULT_SLOT
    return ssm_get(ssm, ACTIVE_SLOT_PARAM, default=DEFAULT_SLOT)


def _set_active_slot(slot: str):
    """SSM のアクティブスロットパラメータを更新する。ACTIVE_SLOT_PARAM 未設定時は何もしない。"""
    if not ACTIVE_SLOT_PARAM:
        return
    ssm_put(ssm, ACTIVE_SLOT_PARAM, slot)
