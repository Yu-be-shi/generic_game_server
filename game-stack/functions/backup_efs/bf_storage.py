"""
bf_storage.py - S3 <-> EFS 間の汎用ストレージ操作ヘルパー

backup / restore_all / switch_slot / restore の複数アクションから共有される、
ファイル形式（Palworld ワールドかどうか）に依存しない汎用処理を集める。
"""

import pathlib
import shutil

from bf_config import BACKUP_BUCKET, logger, s3


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
    exclude_prefix が指定されていれば、そのプレフィックスに一致するキーは除外する
    （str.startswith に渡すため、文字列 1 つでもタプルでもよい）。
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
