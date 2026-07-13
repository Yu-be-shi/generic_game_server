# コストプラン

このスタックの費用構造と、月次コストの見積もりをまとめる。  
**数値は 2026年6月 時点の ap-northeast-1（東京）の概算。AWS 公式料金表で再確認すること。**

---

## 設計思想: 固定費ゼロ

| 使わない構成 | 理由 |
|---|---|
| ALB（Application Load Balancer） | ~$19/月の固定費が発生するため不採用 |
| NAT Gateway | ~$45/月の固定費が発生するため不採用 |
| Route 53 ホストゾーン | $0.50/月 + クエリ課金、IP 通知で代替 |
| 常時起動の EC2 | 停止時でも一部課金。Fargate は停止時に完全無料 |

代わりに **ECS タスクへ直接パブリック IP** を付与して通信を実現する。  
課金の主役は「**起動中の Fargate タスク**」だけで、停止中はほぼ無料。

---

## リソース別コスト

### 常時発生（固定費）

| リソース | 課金単位 | 目安 |
|---|---|---|
| **EFS**（セーブデータ + サーバー本体） | $0.36/GB-月 | 現在 ~3.6GB → **~$1.30/月**。12ヶ月無料枠（5GB）内なら $0 |
| **S3**（tfstate + バックアップ） | $0.025/GB-月 | 通常バックアップ ~4GB → **~$0.10/月** |
| **Lambda**（Discord 制御・通知・バックアップ） | 実行課金 | 無料枠（100万回/月）内に収まる → **$0** |
| **CloudWatch Logs** | $0.033/GB-月 | 数十MB 程度 → **$0** |
| **CloudWatch ダッシュボード** | 3 個/アカウントまで無料、以降 $3.00/個-月 | 1〜2 ゲーム運用 → **$0** |
| **CloudWatch カスタムメトリクス**（PlayersOnline） | 10 個まで無料、以降 $0.30/個-月（データ存在時間で按分） | ゲームごと 1 個・停止中はデータなし → **$0** |
| **AWS Budgets** | 無料 | $0 |
| **VPC / SG / IGW** | 無料 | $0 |
| **小計** | | **~$1.40/月**（無料枠切れ後） |

> EFS の無料枠（5GB × 12ヶ月）は AWS アカウント作成から 12 ヶ月が経過すると失効する。
> サーバー本体（~3.5GB）+ セーブ（~0.1GB）で合計 3.6GB なので、
> 無料枠内は $0、切れると **$1.30/月** が固定費として加わる。

### 従量費用（Fargate タスク起動中のみ）

2vCPU / 4GB メモリ + パブリック IPv4 の場合（ap-northeast-1、`example.tfvars` 標準設定）:

| 項目 | 単価 | 1時間のコスト |
|---|---|---|
| vCPU（2個） | $0.05056/vCPU-h | $0.10112 |
| メモリ（4GB） | $0.00553/GB-h | $0.02212 |
| パブリック IPv4 | $0.005/h | $0.00500 |
| **合計** | | **~$0.129/時間**（≈ ¥19/時間） |

> メモリを増やす場合（Palworld 8GB 推奨など）: `task_memory = 8192` なら $0.155/h（≈ ¥23/h）

### 月額シミュレーション（Fargate 起動時間別）

2vCPU / 4GB 構成（$0.129/h）で試算:

| プレイスタイル | 起動時間/月 | Fargate 費用 | 固定費合計 | **月額合計** |
|---|---|---|---|---|
| たまに（週1回×2時間） | 8時間 | $1.03 | $1.40 | **~$2.5** |
| 週3回×2時間 | 24時間 | $3.10 | $1.40 | **~$4.5** |
| 毎日2時間 | 60時間 | $7.74 | $1.40 | **~$9** |
| 毎日3時間 | 90時間 | $11.61 | $1.40 | **~$13** ← 予算上限 |
| 毎日4時間 | 120時間 | $15.48 | $1.40 | **~$17** ⚠️ 超過 |
| 常時起動（24h） | 720時間 | $92.88 | $1.40 | **~$94** ⚠️ 大幅超過 |

**予算 $13/月 で遊べる上限: 約90時間/月（≈ 毎日3時間）**

---

## 予算ガード

AWS Budgets で月 $13 の上限を設定済み（`palworld.tfvars` の `budget_limit_usd = 13.0`）。  
実際の支出が以下の閾値を超えると **Discord の Webhook チャンネルに通知**が届く。

| 閾値 | 説明 |
|---|---|
| 20% ($2.60) | 今月の費用が発生し始めたサイン |
| 50% ($6.50) | 月の中間チェック |
| 80% ($10.40) | 残り ~$2.60。今月は控えめに |
| 100% ($13.00) | 予算上限に到達。サーバー停止を検討 |

> Budgets はリアルタイムではなく通常 4〜8時間遅延がある。通知が来る頃には
> すでに閾値を超えている場合があるため、注意して運用する。

---

## コスト削減の指針

### 1. 遊ばない時はサーバーを止める

自動停止機能（`idle_timeout_minutes = 30`）が設定済み。全プレイヤーが切断してから
30分後に自動で `desiredCount=0` になる。**接続したまま放置しないこと。**

手動停止:
```bash
# AWS CLI
aws ecs update-service \
  --cluster palworld-palworld-cluster \
  --service palworld-palworld-service \
  --desired-count 0

# または Discord コマンド
/stop game:palworld
```

### 2. `task_memory` を必要十分値に下げる

`task_memory` はゲームの実際のメモリ使用量に合わせて設定する。
まず 4096（4GB）で起動し、CloudWatch で使用率が高ければ増やす。

```bash
# 直近のメモリ使用率を確認
aws cloudwatch get-metric-statistics \
  --namespace AWS/ECS \
  --metric-name MemoryUtilization \
  --dimensions Name=ClusterName,Value=palworld-palworld-cluster \
              Name=ServiceName,Value=palworld-palworld-service \
  --start-time $(date -u -d '-7 days' '+%Y-%m-%dT%H:%M:%S') \
  --end-time $(date -u '+%Y-%m-%dT%H:%M:%S') \
  --period 3600 \
  --statistics Average \
  --output table
```

参考: `task_memory = 4096`（4GB）→ `task_memory = 8192`（8GB）に増やすと
メモリ費用が約2倍になるが、Palworld の安定性が向上することがある（月90時間で +$1.49）。

### 3. S3 バックアップのライフサイクルポリシー

`game-stack/backup.tf` にライフサイクルポリシー（`aws_s3_bucket_lifecycle_configuration`）が
Terraform で定義済み:

- カレントバージョン: 30 日後 → **STANDARD_IA**（標準の約 56% 安）
- 非カレントバージョン: 7 日後 → **GLACIER_IR**（さらに約 68% 安）
- 非カレントバージョン: 30 日で完全削除

追加の設定変更は不要。コスト削減は自動で行われる。

---

## 複数ゲームを同時起動する場合の注意

Fargate タスクはゲームごとに独立して課金される。  
Palworld（~$0.20/h）+ Minecraft（~$0.10/h）を同時起動すると約2倍のコストになる。  
予算は共有なので、複数ゲームで遊ぶ場合は `budget_limit_usd` を調整すること。

---

## 参考リンク

- [AWS Fargate 料金（東京）](https://aws.amazon.com/jp/fargate/pricing/)
- [Amazon EFS 料金](https://aws.amazon.com/jp/efs/pricing/)
- [Amazon S3 料金](https://aws.amazon.com/jp/s3/pricing/)
- [AWS パブリック IPv4 料金](https://aws.amazon.com/jp/vpc/pricing/)
