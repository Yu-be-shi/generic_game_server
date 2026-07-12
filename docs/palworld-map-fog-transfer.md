# Palworld マップ踏破データ（霧）の移植手順

## 概要

co-op からダディケーテッドサーバーへワールドを移行すると、マップの可視部分（霧の晴れ具合）がリセットされる。これを co-op 時代の状態に復元する手順。**プレイヤー 1 人につき 1 回**、各自のクライアント側データに対して行う。

## 背景

マップの踏破状況はワールドデータ（`Level.sav`）ではなく、各プレイヤーの PC にあるクライアント側データ `LocalData.sav` の `WorldMapUISaveDataMap`（`MainMap` / `Tree` の 2 マップ、`MaskTextureData` = 可視領域マスク）に保存される。ワールド移行では引き継がれず、サーバー接続時に新しい LocalData が作られるため、移植元→移植先で同名キー同士のマスクをコピーして復元する。

2026-07-12 にホスト分で実施し成功済み。

## 構成・仕組み

- 変換スクリプト: [`scripts/transplant_map_fog.py`](../scripts/transplant_map_fog.py)
- 依存: PlM（Oodle）形式対応の palsav エンジン（[deafdudecomputers/PalworldSaveTools](https://github.com/deafdudecomputers/PalworldSaveTools) 由来）。本家 cheahjs 版は旧形式（PlZ）のみで**使えない**
- 入力 2 ファイル → 出力 1 ファイル（入力は変更しない・出力先の上書き禁止チェックあり）

## 使い方

### プレイヤー側（ファイルを送る人）の手順

1. **Palworld を完全に終了する**
2. エクスプローラーで次を開く（`Win+R` → 貼り付け）:
   `%LOCALAPPDATA%\Pal\Saved\SaveGames\`
3. 数字のフォルダ（自分の SteamID）の中から 2 ファイルを見つける:
   - **移植元**: `912BE13641B41519D7E91697902D8CD4\LocalData.sav`（co-op 時代のデータ。ファイルサイズが大きめ）
   - **移植先**: `7C776116845C445DB60C3B8A9C38783B\LocalData.sav`（サーバー用。**サーバーに一度も入っていないと存在しない** → 先に一度サーバーに入って抜ける）
4. 取り違え防止のためコピーしてリネームし（`coop_LocalData.sav` / `server_LocalData.sav`）、作業者へ送る（Discord 添付等）
5. 返ってきた `LocalData.sav` を適用する:
   - 元の `7C776116...\LocalData.sav` を `LocalData.sav.bak` にリネームして保険にする
   - 受け取ったファイルを `LocalData.sav` という名前でそこに置く
6. Palworld を起動してサーバーに入り、マップを確認する

### 作業者側の手順

```bash
# 初回のみ: ツールのセットアップ
git clone --depth 1 https://github.com/deafdudecomputers/PalworldSaveTools.git
python3 -m venv .venv
.venv/bin/pip install ./PalworldSaveTools/src/palsav/palooz   # Oodle C++ 拡張（要 g++）
.venv/bin/pip install ./PalworldSaveTools/src/palsav

# プレイヤーごとに実行
.venv/bin/python scripts/transplant_map_fog.py \
    coop_LocalData.sav server_LocalData.sav 出力_LocalData.sav
# 「✅ 完了（2 マップのマスクを移植）」と出たら 出力_LocalData.sav を本人へ返す
```

正常時の出力例: 移植元/移植先とも `MainMap` と `Tree` の 2 エントリが表示され、検証行で MainMap ≒ 16.6M・Tree ≒ 4.2M 程度のサイズになる。

## 注意点

- **Palworld 起動中に LocalData.sav を差し替えない**（終了時の保存で上書きされて無意味になる）
- 移植されるのは霧の晴れ具合のみ。ファストトラベル解放・実績・進行はサーバー側データなのでこの作業とは無関係（すでに移行済み）
- スクリプトは入力を変更せず出力の上書きも拒否するが、適用時の `.bak` 保険は必ず作ること
- マップフォルダの GUID（`912BE...` / `7C77...`）はこのプロジェクトの環境固有。他の移行に流用する場合は読み替える
