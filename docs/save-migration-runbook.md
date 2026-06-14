# セーブデータ移行 ランブック（汎用）

バックアップ Lambda の `restore` アクションを使い、ローカルまたは別サーバーのセーブを
EFS 上の本番ダディケーテッドサーバーへ移す手順書。  
**ゲーム固有の注意点は各ゲームのサブドキュメントを参照。**

- Palworld → [`docs/palworld-save-migration.md`](palworld-save-migration.md)

---

## 概要・適用条件

| 状況 | 適用 |
|---|---|
| ローカル/Co-op セーブを初めてダディケーテッドサーバーへ移す | ✅ |
| 別のダディケーテッドサーバーからデータを引っ越す | ✅ |
| セーブが壊れてスナップショットからロールバックしたい | ✅ |
| 通常の EFS バックアップ・リストア | ❌（`action: backup` を使う） |

**仕組み**: バックアップ Lambda（`palworld-palworld-backup-efs`）には `action: restore`
モードがある。S3 にアップロードした zip を EFS の SaveGames フォルダへ展開する。  
既存ワールドフォルダの **名前（OLD_GUID）を保ったまま中身だけ差し替える** 設計のため、
`GameUserSettings.ini` の `DedicatedServerName` を編集せずに新しいセーブが読み込まれる。

---

## 前提

- `game-stack/functions/backup_efs/backup_efs.py` に `restore` アクション実装済み。
- Lambda 関数名: `<game_name>-<workspace>-backup-efs`（例: `palworld-palworld-backup-efs`）。
- バックアップ S3 バケット: `<game_name>-<workspace>-backup-<account_id>`。
- ECS クラスター: `<game_name>-<workspace>-cluster`、サービス: `<game_name>-<workspace>-service`。

---

## Phase 1: ワールド移行

### 手順

#### 1. 現行 EFS を保全する（任意だが推奨）

```bash
aws lambda invoke \
  --function-name <LAMBDA_NAME> \
  --payload '{"action":"backup"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/backup_out.json
cat /tmp/backup_out.json
```

`uploaded: N` が返れば最新バックアップが S3 に入った。

#### 2. EFS 上の既存ワールド GUID（OLD_GUID）を確認する

```bash
aws s3 ls s3://<BACKUP_BUCKET>/<BACKUP_PREFIX>/Pal/Saved/SaveGames/0/ --recursive \
  | awk '{print $4}' | cut -d/ -f9 | sort -u
```

`Level.sav` を含むフォルダ名が OLD_GUID。Lambda はこれを自動検出するが、
念のため目視で確認しておく。

> `<BACKUP_PREFIX>` は Terraform の `local.name_prefix`（= `<game_name>-<workspace>`）。

#### 3. 移行元セーブを zip にまとめる

zip の内部パス構造を以下に合わせる。

```
<source_world_guid>/
├── Level.sav
├── LevelMeta.sav        ← 存在する場合
├── WorldOption.sav      ← 存在する場合
└── Players/
    ├── <player_guid>.sav
    └── ...
```

- `LocalData.sav` は Lambda 側でスキップされるため含めても問題ない。
- `source_world_guid` はフォルダ名として zip 内に現れればよく、
  任意の文字列でも Lambda の `source_world` 引数と一致させれば動く。

#### 4. zip を S3 にアップロードする

```bash
aws s3 cp /path/to/save.zip s3://<BACKUP_BUCKET>/save.zip
```

#### 5. サーバーを停止する（書き込み競合を防ぐ）

```bash
aws ecs update-service \
  --cluster <CLUSTER_NAME> \
  --service <SERVICE_NAME> \
  --desired-count 0

# runningCount=0 になるまで待つ
watch -n 5 'aws ecs describe-services \
  --cluster <CLUSTER_NAME> \
  --services <SERVICE_NAME> \
  --query "services[0].runningCount"'
```

#### 6. Lambda でリストアを実行する

```bash
aws lambda invoke \
  --function-name <LAMBDA_NAME> \
  --payload '{
    "action":       "restore",
    "s3_key":       "save.zip",
    "source_world": "<SOURCE_WORLD_GUID>"
  }' \
  --cli-binary-format raw-in-base64-out \
  /tmp/restore_out.json

cat /tmp/restore_out.json
```

**正常応答の確認ポイント:**
```json
{
  "action": "restore",
  "destination": "/mnt/efs/Pal/Saved/SaveGames/0/<OLD_GUID>",
  "extracted": 5,
  "skipped": 0
}
```

- `extracted` ≥ 1 であること（0 の場合は zip 構造か `source_world` が不正）。
- `destination` の GUID が想定の OLD_GUID であること。

#### 7. サーバーを再起動する

```bash
aws ecs update-service \
  --cluster <CLUSTER_NAME> \
  --service <SERVICE_NAME> \
  --desired-count 1

# runningCount=1 になるまで待つ
watch -n 10 'aws ecs describe-services \
  --cluster <CLUSTER_NAME> \
  --services <SERVICE_NAME> \
  --query "services[0].runningCount"'
```

#### 8. ゲーム内で確認する

接続してワールド・拠点・アイテムが引き継がれていることを確認する。

---

## Phase 2: キャラクター移行（ゲーム依存）

キャラクターの移行はゲーム固有の手順が必要。  
→ 各ゲームのドキュメントを参照（例: [Palworld キャラ移行](palworld-save-migration.md#phase-2-ホストキャラクター引き継ぎ)）。

大まかな流れは共通:
1. Phase 1 完了後、サーバーへ接続して新キャラを作成し、「新 GUID」の `.sav` ファイルを生成させる。
2. バックアップを取って `Level.sav` と `Players/` を手元に落とす。
3. ゲーム固有ツールで旧ホスト GUID → 新 GUID へパッチをあてる。
4. パッチ済みファイルを zip にまとめて S3 へアップロードし、再度 `restore` を実行する。

---

## restore イベントリファレンス

| パラメータ | 型 | 既定値 | 説明 |
|---|---|---|---|
| `action` | string | `"backup"` | `"restore"` を指定 |
| `s3_key` | string | `"save.zip"` | バケット内の zip オブジェクトキー（バケット名は不要） |
| `source_world` | string | `"1D01670C455B39AD23DC8B8B6F1969CB"` | zip 内の世界フォルダ名（GUID） |

**Lambda がやること:**
1. S3 から zip を `/tmp` にダウンロード。
2. EFS の `SaveGames/0/` 内で `Level.sav` を含むフォルダを OLD_GUID として検出。
3. OLD_GUID フォルダの **スナップショットを S3 へ退避**（`_pre_restore_snapshot/<実行ID>/`）。
4. OLD_GUID フォルダの中身をクリア。
5. zip から `<source_world>/` 以下のファイルを OLD_GUID フォルダへ展開。
   - ルート直下の `.sav`（`Level.sav`, `LevelMeta.sav` 等）
   - `Players/*.sav`
   - `LocalData.sav` **はスキップ**（ダディケーテッドサーバー非対応）

---

## ロールバック

restore 前の状態は S3 のスナップショットに保存される。

```bash
# スナップショットの実行 ID を確認
aws s3 ls s3://<BACKUP_BUCKET>/<BACKUP_PREFIX>/_pre_restore_snapshot/

# サーバーを停止
aws ecs update-service --cluster <CLUSTER_NAME> --service <SERVICE_NAME> --desired-count 0

# EFS のワールドフォルダをクリア（Lambda の restore または手動）して
# スナップショットから各ファイルをコピーして戻す
aws s3 cp \
  s3://<BACKUP_BUCKET>/<BACKUP_PREFIX>/_pre_restore_snapshot/<EXEC_ID>/ \
  /mnt/efs/Pal/Saved/SaveGames/0/<OLD_GUID>/ \
  --recursive
  # ※ Lambda 経由でないと EFS に直接書けないため、restore Lambda を
  # スナップショットから作った zip で再実行するのが現実的

# サーバーを再起動
aws ecs update-service --cluster <CLUSTER_NAME> --service <SERVICE_NAME> --desired-count 1
```

---

## よくあるエラー

| 症状 | 原因 | 対処 |
|---|---|---|
| `"extracted": 0` | zip 内パスに `source_world/` プレフィックスがない | zip を `<source_world>/Level.sav` 構造で作り直す |
| `"extracted": 0` | `source_world` 引数の GUID が zip のフォルダ名と不一致 | `source_world` を zip のフォルダ名に合わせる |
| Lambda タイムアウト | zip が大きすぎる | zip を分割するか Lambda タイムアウトを延長 |
| EFS にファイルが書けない | Lambda が VPC 外または SG で NFS がブロック | VPC サブネット・SG を確認 |
