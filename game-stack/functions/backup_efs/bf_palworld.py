"""
bf_palworld.py - Palworld ワールドフォルダ（SaveGames/0/<GUID>/）専用ロジック

restore（zip 展開）・switch_slot（S3 スロット切り替え）の両ハンドラが共有する、
「既存ワールドフォルダの検出 → 退避 → クリア」の手順と zip 展開処理を集約する。
"""

import pathlib
import zipfile

from bf_config import SKIP_FILES, logger
from bf_storage import _clear_directory, _safe_join, _snapshot_to_s3


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
