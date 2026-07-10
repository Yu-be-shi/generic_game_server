"""
backup_efs.py - EFS セーブデータの S3 バックアップ／リストア Lambda 関数

【バックアップモード（デフォルト）】
  トリガー: Amazon EventBridge（24時間ごと）
  処理:
    1. EFS マウントパス（/mnt/efs）以下を再帰的に走査
    2. S3 上の同名オブジェクトとサイズを比較し、変化があるファイルのみアップロード

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

【全体ミラーリストアモード（restore_all・任意データ対応）】
  トリガー: aws lambda invoke による手動実行（Discord /restore コマンドから非同期 invoke）
  処理:
    1. 現在の EFS マウント配下を丸ごと S3 の _pre_restore_snapshot/<exec_id>/ へ退避
    2. S3 上の BACKUP_PREFIX 配下（_pre_restore_snapshot を除く）を EFS へダウンロード
       ※ 追加・上書きのみ行い、EFS 側にしかないファイルは削除しない（安全側）
  イベント例:
    { "action": "restore_all" }

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

依存ライブラリ: boto3（Lambda ランタイムに標準搭載）、標準ライブラリのみ
前提:
  - Lambda は VPC 内に配置し、EFS アクセスポイント経由でマウント済み（/mnt/efs）
  - S3 Gateway VPC Endpoint が設定済み（NAT Gateway 不要）
"""

import logging
import os
import pathlib
import re
import shutil
import zipfile

import boto3
from botocore.exceptions import ClientError

from ssm_params import ssm_get, ssm_put

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 環境変数（Terraform から注入）
BACKUP_BUCKET = os.environ["BACKUP_BUCKET"]
BACKUP_PREFIX = os.environ["BACKUP_PREFIX"]
# switch_slot 専用。空文字列の場合はスロット切り替え機能を使わない前提（既存デプロイ後方互換）
ACTIVE_SLOT_PARAM = os.environ.get("ACTIVE_SLOT_PARAM", "")

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
ssm = boto3.client("ssm")


# ============================================================
# エントリーポイント
# ============================================================

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


def _safe_join(root: pathlib.Path, relative: str):
    """
    root 配下に安全に結合したパスを返す。パストラバーサル（zip や S3 キーに含まれる
    "../" 等で root 外に出ようとする経路）を検出した場合は None を返す。
    """
    dest_path = root / relative
    try:
        dest_path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return dest_path


def _snapshot_to_s3(root_dir: pathlib.Path, prefix: str, swallow_errors: bool = False) -> int:
    """
    root_dir 配下のファイルを丸ごと S3 の prefix 配下へアップロードする
    （ロールバック用スナップショット・スロット保護の両方で使う汎用ヘルパー）。

    swallow_errors=True の場合、個別ファイルの失敗はログに残して継続する
    （restore_all/switch_slot の「途中で諦めない」設計）。
    False（既定）では失敗時に例外をそのまま伝播させる
    （restore の「スナップショットが不完全なら危険な削除に進まない」設計を保つ）。

    アップロードしたファイル数を返す。
    """
    if not root_dir.exists():
        return 0

    uploaded = 0
    for f in root_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root_dir)
        key = f"{prefix}/{rel}"
        if swallow_errors:
            try:
                s3.upload_file(str(f), BACKUP_BUCKET, key)
                uploaded += 1
            except Exception:
                logger.exception("スナップショット失敗: %s", f)
        else:
            s3.upload_file(str(f), BACKUP_BUCKET, key)
            uploaded += 1

    return uploaded


def _mirror_from_s3(prefix: str, dest_root: pathlib.Path, exclude_prefix: str = None):
    """
    S3 の prefix 配下（末尾 "/" 付き）を dest_root へミラーダウンロードする
    （restore_all・switch_slot の両方で使う汎用ヘルパー）。
    exclude_prefix が指定されていれば、そのプレフィックスに一致するキーは除外する。
    追加・上書きのみ行い、dest_root 側にしかないファイルの削除は行わない（安全側）。

    (downloaded, failed) のタプルを返す。
    """
    downloaded = 0
    failed = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BACKUP_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if exclude_prefix and key.startswith(exclude_prefix):
                continue

            relative = key[len(prefix):]
            if not relative:
                continue

            dest_path = _safe_join(dest_root, relative)
            if dest_path is None:
                logger.warning("不正なキーをスキップ: %s", key)
                failed += 1
                continue

            try:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(BACKUP_BUCKET, key, str(dest_path))
                downloaded += 1
            except Exception:
                logger.exception("ダウンロード失敗: %s", key)
                failed += 1

    return downloaded, failed


def _clear_directory(directory: pathlib.Path):
    """ディレクトリの中身を削除する（ディレクトリ自体は残す）。"""
    for item in directory.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def _prepare_world_dir(
    savegames_root: pathlib.Path,
    fallback_name: str,
    snapshot_prefix: str,
    swallow_errors: bool,
    log_detected,
    log_snapshot,
    log_clear,
):
    """
    restore_handler・switch_slot_handler 共通の
    「ワールドフォルダ特定 → (存在すればスナップショットしてクリア) → (なければ新規作成)」手順。

    見つからない場合: savegames_root/fallback_name を新規作成して返す
    （"既存ワールドなし。新規フォルダを作成" を固定文言でログ出力。両ハンドラで同一文言）。

    見つかった場合: log_detected(dir) → _snapshot_to_s3(swallow_errors=swallow_errors) →
    log_snapshot(count) → _clear_directory(dir) → log_clear(dir) の順で実行する
    （呼び出し元ごとに文言が異なるログはコールバックとして受け取り、
    元の実装と同じ順序・文言で出力する）。

    swallow_errors=False（restore_handler）の場合はスナップショット失敗が例外として
    そのまま伝播し、危険な削除（_clear_directory）には進まない。
    swallow_errors=True（switch_slot_handler）の場合は失敗をログに残して続行する。

    戻り値: (target_dir, found_existing, snapshotted_count)
    found_existing=False の場合 snapshotted_count は常に 0。
    """
    existing_dir = _find_existing_world(savegames_root)

    if existing_dir is None:
        savegames_root.mkdir(parents=True, exist_ok=True)
        target_dir = savegames_root / fallback_name
        target_dir.mkdir(exist_ok=True)
        logger.info("既存ワールドなし。新規フォルダを作成: %s", target_dir)
        return target_dir, False, 0

    log_detected(existing_dir)
    snapshotted = _snapshot_to_s3(existing_dir, snapshot_prefix, swallow_errors=swallow_errors)
    log_snapshot(snapshotted)
    _clear_directory(existing_dir)
    log_clear(existing_dir)

    return existing_dir, True, snapshotted


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

            # Zip-Slip 対策: 展開先が dest_dir 配下であることを検証する
            # 悪意ある zip（"../../etc/passwd" 等）によるパストラバーサルを防ぐ
            dest_path = _safe_join(dest_dir, relative)
            if dest_path is None:
                logger.warning("パストラバーサルを検出してスキップ: %s", relative)
                stats["skipped"] += 1
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(zf.read(member.filename))
            logger.info("展開: %s -> %s", relative, dest_path)
            stats["extracted"] += 1

    return stats


# ============================================================
# 全体ミラーリストアモード（任意データ対応）
# ============================================================

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

    # 2. S3 → EFS ミラーリング（_pre_restore_snapshot 配下は除外）
    exclude_prefix = f"{BACKUP_PREFIX}/_pre_restore_snapshot/"
    downloaded, failed = _mirror_from_s3(
        f"{BACKUP_PREFIX}/", EFS_MOUNT_PATH, exclude_prefix=exclude_prefix
    )

    stats = {"snapshotted": snapshotted, "downloaded": downloaded, "failed": failed}
    logger.info(
        "restore_all 完了: snapshotted=%d downloaded=%d failed=%d",
        stats["snapshotted"], stats["downloaded"], stats["failed"],
    )
    return {"action": "restore_all", "snapshot_prefix": snapshot_prefix, **stats}


# ============================================================
# スロット切り替えモード（Palworld セーブデータ専用）
# ============================================================

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
