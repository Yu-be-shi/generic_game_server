# Palworld セーブ移行ガイド（ゲーム固有）

Co-op/ローカルセーブ → ダディケーテッドサーバー（EFS）への移行手順。  
汎用の共通手順は [`docs/save-migration-runbook.md`](save-migration-runbook.md) を参照。

---

## Co-op → ダディケーテッドの特殊事情

### ワールド・拠点・パル: そのまま引き継げる

Co-op セーブの `Level.sav` にはワールド全体の状態（拠点・パルボックス・パル）が入っている。
`restore` でそのまま展開すればサーバーが読み込む。

### ホストキャラクター本体: GUID 変換が必要

Co-op ホストの GUID は `00000000000000000000000000000001`（固定値）だが、
ダディケーテッドサーバーでは **Steam ID から算出された別の GUID** になる。
そのため Co-op セーブをそのまま持ってきてもホストキャラのデータが認識されず、
新規作成プロンプトになる。

`xNul/palworld-host-save-fix` を使い `CharacterSaveParameterMap` と
`Players/<GUID>.sav` の GUID を書き換えることで解消できる。

---

## Phase 1: ワールド移行

[汎用ランブックの Phase 1](save-migration-runbook.md#phase-1-ワールド移行) に従う。

**Palworld 固有の補足:**

- `source_world` には Steam userdata 内のフォルダ名（32文字 GUID）を指定する。
  複数のワールドが save.zip に入っている場合は最も新しいものを選ぶ。
  ```
  例: source_world = "1D01670C455B39AD23DC8B8B6F1969CB"
  ```

- Lambda は `LocalData.sav` を自動スキップする（シングルプレイ専用ファイルのため）。

- Phase 1 完了後にサーバーへ接続すると **新規キャラ作成プロンプト**が出る。これは正常。
  次の Phase 2 のために **新キャラを作成しレベル2程度まで進めてから切断**する。
  これによりサーバー側に `Players/<NEW_GUID>.sav` が生成される。

---

## Phase 2: ホストキャラクター引き継ぎ

### 2-1. 前提の確認

| 変数 | 値 | 確認方法 |
|---|---|---|
| OLD_GUID（EFS ワールドフォルダ） | 自動検出 | S3 バックアップの `SaveGames/0/` 以下のフォルダ名 |
| 旧ホスト GUID | `00000000000000000000000000000001` | Co-op では常にこの固定値 |
| 新キャラ GUID（NEW_GUID） | `<SteamID を変換した 32文字>` | EFS の `Players/` に生成されたファイル名 |

NEW_GUID の確認コマンド:
```bash
# バックアップを実行して S3 で確認する
aws lambda invoke \
  --function-name palworld-palworld-backup-efs \
  --payload '{"action":"backup"}' \
  --cli-binary-format raw-in-base64-out /tmp/backup_out.json

aws s3 ls s3://<BACKUP_BUCKET>/palworld-palworld/Pal/Saved/SaveGames/0/<OLD_GUID>/Players/
```

`00000000000000000000000000000001.sav` 以外に現れたファイル名（`.sav` を除いた部分）が NEW_GUID。

### 2-2. 必要ファイルを S3 から取得する

```bash
mkdir -p /tmp/palworld_fix/Players

# Level.sav（現在 EFS にある状態＝Phase 1 適用済み + 新キャラ登録済み）
aws s3 cp \
  s3://<BACKUP_BUCKET>/palworld-palworld/Pal/Saved/SaveGames/0/<OLD_GUID>/Level.sav \
  /tmp/palworld_fix/Level.sav

# 旧ホストキャラ（Co-op 時代のデータ）— 2通りの入手元
#   A) S3 バックアップの Players/ フォルダから直接コピー（Phase 1 適用済みなら存在する）
aws s3 cp \
  s3://<BACKUP_BUCKET>/palworld-palworld/Pal/Saved/SaveGames/0/<OLD_GUID>/Players/00000000000000000000000000000001.sav \
  /tmp/palworld_fix/Players/00000000000000000000000000000001.sav

#   B) _pre_restore_snapshot/ から取得
#      aws s3 cp s3://<BACKUP_BUCKET>/palworld-palworld/_pre_restore_snapshot/<EXEC_ID>/Players/00000000000000000000000000000001.sav /tmp/palworld_fix/Players/

# 新キャラ（ダディケーテッドで作成したもの）
aws s3 cp \
  s3://<BACKUP_BUCKET>/palworld-palworld/Pal/Saved/SaveGames/0/<OLD_GUID>/Players/<NEW_GUID>.sav \
  /tmp/palworld_fix/Players/<NEW_GUID>.sav
```

### 2-3. GUID 変換ツールの環境を構築する

> **必要なもの**: gcc、python3-dev（C 拡張のビルドに必要）  
> Palworld v0.7.3 以降のセーブは **PlM（Oodle 圧縮）** 形式のため、
> 標準 `palworld-save-tools`（PyPI 版 0.24.0）では読めない。
> 以下のフォーク + Oodle バインディングが必要。

```bash
# 作業用 venv
python3 -m venv /tmp/palworld_venv

# 1. Oodle バインディング（C ソースからビルド）
/tmp/palworld_venv/bin/pip install git+https://github.com/oMaN-Rod/pyooz.git

# 2. palworld-save-tools の基盤（gvas.py, paltypes.py 等）
/tmp/palworld_venv/bin/pip install palworld-save-tools loguru

# 3. PlM 対応フォーク（palsav.py + compressor/ + archive.py 等）を上書き
git clone https://github.com/deafdudecomputers/PalworldSaveTools /tmp/deafdudecomputers-palworld-save-tools

SITE_PKGS=$(/tmp/palworld_venv/bin/python3 -c "import site; print(site.getsitepackages()[0])")
FORK=/tmp/deafdudecomputers-palworld-save-tools/src/palworld_save_tools
PST=$SITE_PKGS/palworld_save_tools

for f in palsav.py archive.py gvas.py paltypes.py json_tools.py; do
    [ -f "$FORK/$f" ] && cp "$FORK/$f" "$PST/$f"
done
cp -r "$FORK/compressor/" "$PST/compressor/"
rm -rf "$PST/rawdata"
cp -r "$FORK/rawdata" "$PST/rawdata"
```

動作確認:
```bash
/tmp/palworld_venv/bin/python3 -c "
from palworld_save_tools.palsav import decompress_sav_to_gvas
with open('/tmp/palworld_fix/Level.sav', 'rb') as f:
    data = f.read()
raw, save_type = decompress_sav_to_gvas(data)
print(f'OK: {len(raw):,} bytes, save_type=0x{save_type:02X}')
"
# → OK: 22819644 bytes, save_type=0x31  ← 0x31 = PLM
```

### 2-4. fix_host_save.py を取得・パッチする

```bash
git clone https://github.com/xNul/palworld-host-save-fix /tmp/palworld-host-save-fix
cp /tmp/palworld-host-save-fix/fix_host_save.py /tmp/fix_host_save_patched.py
```

`sav_to_json` / `json_to_sav` 関数に以下のパッチを当てる。  
**目的**: 本家スクリプトは `PalWorldSaveGame` クラスを `save_type=0x32`（PLZ/zlib）で
再圧縮するが、v0.7.3 は PLM（0x31）が正しい形式。解凍時に検出した `save_type` を
保持して再圧縮時に使うように修正する。

```diff
-def sav_to_json(filepath):
+def sav_to_json(filepath):
     print(f'Converting {filepath} to JSON...', end='', flush=True)
     with open(filepath, 'rb') as f:
         data = f.read()
-        raw_gvas, _ = decompress_sav_to_gvas(data)
+        raw_gvas, detected_save_type = decompress_sav_to_gvas(data)
     gvas_file = GvasFile.read(
         raw_gvas, PALWORLD_TYPE_HINTS, PALWORLD_CUSTOM_PROPERTIES, allow_nan=True
     )
     json_data = gvas_file.dump()
+    json_data['_detected_save_type'] = detected_save_type
     print('Done!', flush=True)
     return json_data

 def json_to_sav(json_data, output_filepath):
     print(f'Converting JSON to {output_filepath}...', end='', flush=True)
     gvas_file = GvasFile.load(json_data)
-    if (
-        'Pal.PalWorldSaveGame' in gvas_file.header.save_game_class_name
-        or 'Pal.PalLocalWorldSaveGame' in gvas_file.header.save_game_class_name
-    ):
-        save_type = 0x32
-    else:
-        save_type = 0x31
+    save_type = json_data.pop('_detected_save_type', None)
+    if save_type is None:
+        if (
+            'Pal.PalWorldSaveGame' in gvas_file.header.save_game_class_name
+            or 'Pal.PalLocalWorldSaveGame' in gvas_file.header.save_game_class_name
+        ):
+            save_type = 0x32
+        else:
+            save_type = 0x31
     sav_file = compress_gvas_to_sav(
```

### 2-5. GUID 変換を実行する

```bash
# 対話プロンプトは echo "" で通過させる
echo "" | /tmp/palworld_venv/bin/python3 /tmp/fix_host_save_patched.py \
  /tmp/palworld_fix \
  <NEW_GUID> \
  00000000000000000000000000000001 \
  True
```

**期待される出力:**
```
WARNING: Running this script WILL change your save files...
> Converting Level.sav to JSON... Done!
Converting Players/00000000000000000000000000000001.sav to JSON... Done!
Modifying JSON save data... Done!
Converting JSON to Level.sav... Done!
Converting JSON to Players/00000000000000000000000000000001.sav... Done!
Fix has been applied! Have fun!
```

完了後、`Players/` に `<NEW_GUID>.sav` が存在し
`00000000000000000000000000000001.sav` が消えていることを確認する。

### 2-6. patched.zip を作成して S3 へアップロードする

```bash
python3 -c "
import zipfile, pathlib
WORLD = '<SOURCE_WORLD_GUID>'  # Phase 1 で使った source_world と同じ値
src = pathlib.Path('/tmp/palworld_fix')
with zipfile.ZipFile('/tmp/patched.zip', 'w', zipfile.ZIP_STORED) as zf:
    zf.write(src / 'Level.sav', f'{WORLD}/Level.sav')
    zf.write(src / 'Players/<NEW_GUID>.sav', f'{WORLD}/Players/<NEW_GUID>.sav')
"

aws s3 cp /tmp/patched.zip s3://<BACKUP_BUCKET>/patched.zip
```

### 2-7. リストアを再実行する

```bash
# サーバーが起動中なら停止
aws ecs update-service \
  --cluster <CLUSTER_NAME> --service <SERVICE_NAME> --desired-count 0
# runningCount=0 を確認してから

aws lambda invoke \
  --function-name <LAMBDA_NAME> \
  --payload '{
    "action":       "restore",
    "s3_key":       "patched.zip",
    "source_world": "<SOURCE_WORLD_GUID>"
  }' \
  --cli-binary-format raw-in-base64-out /tmp/restore_patched.json

cat /tmp/restore_patched.json
# → "extracted": 2  (Level.sav + Players/<NEW_GUID>.sav)
```

### 2-8. サーバーを起動して確認する

```bash
aws ecs update-service \
  --cluster <CLUSTER_NAME> --service <SERVICE_NAME> --desired-count 1
```

サーバーへ接続し、**新規作成プロンプトなし・引き継いだキャラクターで入れる**ことを確認する。

---

## 今回の実値（記録用）

> この基盤を再利用する際の参考値。次回移行では GUID 等が変わる。

| 項目 | 値 |
|---|---|
| EFS ワールドフォルダ（OLD_GUID） | `7C776116845C445DB60C3B8A9C38783B` |
| 移行元ワールド（save.zip 内） | `1D01670C455B39AD23DC8B8B6F1969CB` |
| 旧 Co-op ホスト GUID | `00000000000000000000000000000001` |
| 新ダディケーテッドキャラ GUID | `1AD41CD9000000000000000000000000` |
| 実施日 | 2026-06-15 |
| Palworld バージョン | v0.7.3（PlM/Oodle 形式）|

---

## トラブルシューティング（Palworld 固有）

### `Exception: not a compressed Palworld save, found b'PlM' instead of b'PlZ'`

`palworld-save-tools` の PyPI 版（0.24.0 以前）が PLM 形式に非対応。  
→ [2-3. 環境構築](#2-3-guid-変換ツールの環境を構築する) の手順で
`deafdudecomputers/PalworldSaveTools` フォークに差し替える。

### `ImportError: Failed to import 'ooz' module`

`pyooz`（Oodle バインディング）が未インストール。  
→ `pip install git+https://github.com/oMaN-Rod/pyooz.git`（gcc / python3-dev が必要）。

### `Exception: Warning: EOF not reached` in `rawdata/character.py`

v0.7.3 の GVAS フォーマット変更に古い `rawdata/` が対応していない。  
→ `deafdudecomputers/PalworldSaveTools` の `rawdata/` ディレクトリで上書きする（2-3 参照）。

### 変換後の Level.sav をサーバーが読めない / クラッシュする

`json_to_sav` が `save_type=0x32`（zlib）で圧縮してしまった可能性。  
→ [2-4 のパッチ](#2-4-fix_host_savepy-を取得パッチする)が正しく当たっているか確認する。  
  `python3 -c "with open('Level.sav','rb') as f: d=f.read(12); print(d[8:11])"` で
  `b'PlM'` が返れば PLM 形式で正しく圧縮されている。

### `fix_host_save.py` が対話プロンプトで止まる

`echo "" | python3 fix_host_save_patched.py ...` で Enter を自動入力する。
