# 設計代替案メモ

このドキュメントは現状の設計トレードオフと代替アプローチを記録する。
採用・非採用の理由を明示し、将来の判断材料とする。

---

## 1. Terraform State ロック

### 現状

`control-plane/` と `game-stack/` のどちらも DynamoDB ロックテーブルを使用していない。
単一ユーザー・ローカルからのみ apply するという運用前提のため意図的なトレードオフ。

### 代替案: S3 ネイティブロック（TF 1.10+）

Terraform 1.10 以降で利用可能な S3 バックエンドのネイティブロック機能。
DynamoDB テーブルを追加リソースなしで、S3 バケットだけでロックを実現できる。

```hcl
# backend.hcl
bucket         = "<your-state-bucket>"
key            = "control-plane/terraform.tfstate"
region         = "ap-northeast-1"
use_lockfile   = true    # S3 バケットの条件付き書き込みでロック（TF 1.10+）
```

**メリット**
- DynamoDB テーブル不要（追加コスト・管理ゼロ）
- 複数端末から apply する場合の競合防止
- `.terraform-version` を `1.10.x` 以上にするだけで有効化できる

**デメリット**
- TF 1.10 未満では無効（現在の `.terraform-version` を更新する必要がある）
- S3 条件付き書き込みは強整合性だが、バケットバージョニング有効を推奨

**採用判断**: 単一開発者のうちは現状で十分。複数端末や CI から apply する場合に検討。

---

## 2. ed25519 署名検証の実装

### 現状

`control-plane/functions/discord_control/ed25519.py` に純 Python 実装の Ed25519 署名検証を持つ。
外部 Lambda レイヤーやコンテナイメージへの依存を避けるための設計。

### 代替案: `cryptography` ライブラリ

AWS SDK for Python に付属する `cryptography` パッケージ（または Lambda 提供の SDK レイヤー）を使えば、
自前実装を `Ed25519PublicKey.verify()` 1 行に置き換えられる。

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

def verify(public_key_hex: str, message: bytes, signature_hex: bytes) -> bool:
    pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    try:
        pub_key.verify(bytes.fromhex(signature_hex), message)
        return True
    except Exception:
        return False
```

**メリット**
- 自前暗号実装の監査・メンテナンス負担が消える
- NIST/FIPS 準拠のライブラリを利用できる

**デメリット**
- Lambda に `cryptography` を含める手段が必要（ZIP に bundling または Lambda レイヤー）
- `cryptography` は C 拡張を含むため、アーキテクチャ（x86_64/arm64）に合わせたビルドが必要

**採用判断**: `ed25519.py` は 200 行程度で十分に読みやすく、テスト済み。
Lambda をコンテナイメージに移行する機会があれば `cryptography` 入りベースイメージを検討する。

---

## 3. /update コマンド（auto_update Lambda）の実装方式

### 現状

`auto_update.py` は `ecs.run_task()` でアップデートタスクを起動し、完了まで Lambda 内でポーリングする
（最大 720 秒、12 分間 Lambda を占有）。アップデートに 15 分以上かかる場合はタイムアウトする。

```
/update → Lambda 起動 → run_task → ポーリング (60秒×12回) → 完了/タイムアウト
```

### 代替案: AWS Step Functions の待機ステート

Step Functions の `waitForTaskToken` パターンを使えば、Lambda を占有せずに
ECS タスク完了を待機できる。

```
/update → Lambda (run_task) → Step Functions State Machine 起動
  → ECS タスク: 完了時に SendTaskSuccess を呼ぶ
  → State Machine が完了を受け取り次の Lambda を呼ぶ（通知等）
```

**メリット**
- Lambda の 15 分タイムアウト制限から解放される
- Lambda の課金が待機時間分なくなる（ECS タスク分は変わらない）
- アップデート失敗時のリトライや分岐が State Machine で宣言的に書ける

**デメリット**
- Step Functions State Machine の追加 Terraform リソース
- ECS タスク（auto_shutdown.sh）から Step Functions へのコールバック実装が必要
- 複雑さが増す（現状のシンプルな Lambda ポーリングに対し過剰かもしれない）

**採用判断**: アップデートが 12 分を超えることが常態化する場合に検討。
現状のゲーム（Palworld）では数分以内に完了するため現状維持。

---

## 4. EFS バックアップの差分検知方式

### 現状

`backup_efs.py` はローカルファイルと S3 オブジェクトの **サイズのみ** を比較して差分を判定する。
サイズが同じでも内容が異なるファイル（例: タイムスタンプ付きセーブデータ）は
アップロードされない可能性がある。

```python
def _is_unchanged(local_path, s3_key) -> bool:
    head = s3.head_object(Bucket=BACKUP_BUCKET, Key=s3_key)
    return head["ContentLength"] == local_path.stat().st_size  # サイズのみ比較
```

### 代替案 A: ETag + mtime の併用

S3 オブジェクトの ETag（MD5 相当）とローカルファイルの mtime を比較することで、
サイズが同じでも変更されたファイルを検出できる。

```python
import hashlib

def _is_unchanged(local_path, s3_key) -> bool:
    head = s3.head_object(Bucket=BACKUP_BUCKET, Key=s3_key)
    # ETag は通常 MD5（マルチパートアップロードの場合は異なる）
    local_md5 = hashlib.md5(local_path.read_bytes()).hexdigest()
    return f'"{local_md5}"' == head.get("ETag", "")
```

**注意**: ファイルサイズが大きい場合（数百 MB）は MD5 計算が Lambda の CPU 時間を消費する。

### 代替案 B: AWS Backup

EFS の AWS Backup ジョブを定期実行する。増分バックアップを自動で行い、
WORM（Write Once Read Many）ポリシーや保持期間の管理が AWS 側で完結する。

**メリット**
- Lambda コード不要、EFS Backup Plan を Terraform で宣言するだけ
- 増分バックアップで効率的（変更されたブロックのみ転送）
- ポイントインタイムリカバリが使える

**デメリット**
- バックアップストレージコストが別途発生（$0.05/GB/月）
- リストアは AWS コンソール or CLI のみ（Lambda から直接は難しい）
- 現状の `backup_efs.py` で実装しているリストア機能（/tmp 経由の zip 展開）を代替できない

**採用判断**: 
- セーブデータは数 MB〜数十 MB で、サイズが同一のまま変化するケースはゲームの仕様次第。
  Palworld のセーブデータはサイズが変わるため現状のサイズ比較で概ね機能する。
- AWS Backup はコストが増えるが、長期保持や規制対応が必要になった時に移行を検討。
- mtime 比較は将来の改善候補（head_object に LastModified があるため対応可能）。
