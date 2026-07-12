"""
transplant_map_fog.py - Palworld マップ踏破データ（霧の晴れ具合）の移植ツール

co-op 時代の LocalData.sav から、ダディケーテッドサーバー接続用の LocalData.sav へ
WorldMapUISaveDataMap の MaskTextureData（マップ可視領域マスク）を同名キー
（MainMap / Tree 等）同士でコピーする。クライアント側ファイル専用で、
サーバー側データには一切関係しない。

使い方（詳細は docs/palworld-map-fog-transfer.md）:
    python transplant_map_fog.py <移植元 LocalData.sav> <移植先 LocalData.sav> <出力先.sav>

依存: PlM（Oodle）形式対応の palsav エンジン（deafdudecomputers/PalworldSaveTools 由来）
    git clone --depth 1 https://github.com/deafdudecomputers/PalworldSaveTools.git
    python3 -m venv .venv && .venv/bin/pip install \
        ./PalworldSaveTools/src/palsav/palooz ./PalworldSaveTools/src/palsav
"""

import sys
from pathlib import Path

from palsav.core import compress_gvas_to_sav, decompress_sav_to_gvas
from palsav.gvas import GvasFile
from palsav.paltypes import PALWORLD_CUSTOM_PROPERTIES, PALWORLD_TYPE_HINTS

save_types = {}


def load(path: Path):
    raw, st = decompress_sav_to_gvas(path.read_bytes())
    save_types[path] = st
    gvas = GvasFile.read(raw, PALWORLD_TYPE_HINTS, PALWORLD_CUSTOM_PROPERTIES, allow_nan=True)
    return gvas.dump()


def map_entries(data, label):
    """SaveData 直下の WorldMapUISaveDataMap のエントリ一覧を返す（無ければ None）。"""
    sd = data.get("properties", {}).get("SaveData", {}).get("value", {})
    m = sd.get("WorldMapUISaveDataMap")
    if not m:
        print(f"[{label}] WorldMapUISaveDataMap が見つかりません。SaveData のキー: {sorted(sd.keys())}")
        return None
    for e in m["value"]:
        mask = e.get("value", {}).get("MaskTextureData")
        size = len(str(mask)) if mask is not None else 0
        print(f"[{label}] key={e.get('key')} mask={size}")
    return m["value"]


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    src_path, tgt_path, out_path = (Path(p) for p in sys.argv[1:4])
    assert src_path.exists(), f"移植元がありません: {src_path}"
    assert tgt_path.exists(), f"移植先がありません: {tgt_path}"
    assert not out_path.exists(), f"出力先が既に存在します（上書き防止）: {out_path}"

    src = load(src_path)
    tgt = load(tgt_path)
    src_entries = map_entries(src, "移植元")
    tgt_entries = map_entries(tgt, "移植先")
    if not src_entries or not tgt_entries:
        sys.exit("構造が想定と異なるため中断しました（上記の出力を確認）")

    # 同名キー同士で MaskTextureData を置き換える（キー・その他フィールドは維持）
    src_by_key = {str(e.get("key")): e for e in src_entries}
    replaced = 0
    for e in tgt_entries:
        key = str(e.get("key"))
        src_e = src_by_key.get(key)
        if not src_e or "MaskTextureData" not in src_e.get("value", {}):
            print(f"  スキップ（移植元に {key} のマスクなし）")
            continue
        e["value"]["MaskTextureData"] = src_e["value"]["MaskTextureData"]
        replaced += 1
        print(f"  置き換え: {key}")
    if replaced == 0:
        sys.exit("置き換え対象がありませんでした")

    gvas = GvasFile.load(tgt)
    out_path.write_bytes(compress_gvas_to_sav(gvas.write(PALWORLD_CUSTOM_PROPERTIES), save_types[tgt_path]))

    # 出力を読み直して検証
    assert map_entries(load(out_path), "検証") is not None
    print(f"\n✅ 完了: {out_path}（{replaced} マップのマスクを移植）")


if __name__ == "__main__":
    main()
