# 汎用ゲームサーバー基盤

ECS Fargate + EFS による固定費ゼロのゲームサーバー基盤。  
Discord スラッシュコマンドでサーバーの起動・停止・状態確認ができる。

---

## 構成概要

```
generic_game_server/
├── game-stack/          # ゲームサーバー本体（ゲームごとに workspace を作成）
├── control-plane/       # Discord コントロールプレーン（1回だけ apply）
└── games/               # ゲームごとの設定ファイル（.tfvars）
    └── example.tfvars   # コピーして使うテンプレート
```

**コスト構造:**
- **game-stack**: ECS タスクが停止中のときは料金ゼロ。起動中のみ Fargate 料金が発生する。
- **control-plane**: API Gateway v2 HTTP API + Lambda。無料枠で動くため固定費ゼロ。
- **EFS**: 最初の 5 GB は無料。小規模利用では無視できる額。

**自動停止:** 接続がなくなってから `idle_timeout_minutes` 後にサーバーが自動停止する。放置しても費用は発生しない。

---

## セットアップ（初回のみ）

### 前提条件
- AWS CLI の設定済み（`aws configure` 実行済み）
- Terraform 1.5 以上インストール済み（`tfenv` 推奨。`.terraform-version` で自動選択）
- Discord アカウントと Developer Portal へのアクセス
- Discord Bot Token（Step 1 で発行）
- AWS Billing コンソールで `Project` / `Game` をコスト配分タグとして有効化済み（Cost Explorer でゲーム別フィルタに使用）

---

### Step 1: Discord アプリを作成する

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. 「New Application」をクリック → アプリ名を入力（例: `Game Server Bot`）
3. **左メニュー → Bot** を開く → 「Add Bot」をクリック
   - 「TOKEN」の「Reset Token」でトークンを発行 → **どこかにメモ**（後で使う）
4. **左メニュー → General Information** を開く
   - `APPLICATION ID` をメモ（後で使う）
   - `PUBLIC KEY` をメモ（control-plane の変数として使う）
5. **左メニュー → OAuth2 → URL Generator**
   - Scopes: `applications.commands` にチェック
   - 生成された URL をブラウザで開く → ボットをサーバーに招待する

---

### Step 2: control-plane をデプロイする

> **⚠️ デプロイ順序:** control-plane を **必ず先に** apply すること。
> game-stack は共有 VPC を `"ggs-shared-vpc"` タグで参照するため、
> control-plane の VPC が存在しないと game-stack の apply が失敗する。

```bash
cd generic_game_server/control-plane

# backend.hcl.example をコピーしてバケット名を記入（初回のみ）
cp backend.hcl.example backend.hcl
# backend.hcl を開いて bucket = "<your-state-bucket>" を実際のバケット名に変更

# 初期化（S3 リモートバックエンドを使用）
terraform init -backend-config=backend.hcl

# Public Key を変数として渡して apply
terraform apply \
  -var="discord_public_key=<Step 1 でメモした PUBLIC KEY>" \
  -var="aws_region=ap-northeast-1"

# 任意: 操作できるユーザーを自分だけに制限する場合
# terraform apply \
#   -var="discord_public_key=<PUBLIC KEY>" \
#   -var='discord_allowed_user_ids=["あなたのDiscordユーザーID"]'
```

> **Discord ユーザー ID の確認方法:** Discord の「設定 → 詳細設定 → 開発者モードをON」→ 自分のアイコンを右クリック → 「IDをコピー」

Apply が完了すると `interactions_endpoint_url` が出力される。

---

### Step 3: Discord に Interactions Endpoint URL を登録する

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. アプリを選択 → **General Information**
3. `INTERACTIONS ENDPOINT URL` の欄に `interactions_endpoint_url`（Step 2 の apply 出力値）を貼り付けて **Save Changes**
   - 保存に成功すれば、Discord が PING → Lambda が PONG で応答済みの証拠
   - 保存できない場合は署名検証に失敗している → `discord_public_key` の値を確認する

---

### Step 4: スラッシュコマンドを登録する

```bash
cd generic_game_server/control-plane

export DISCORD_APP_ID="<Step 1 でメモした APPLICATION ID>"
export DISCORD_BOT_TOKEN="<Step 1 でメモした Bot TOKEN>"

bash scripts/register_commands.sh
```

グローバルコマンドとして登録されるため、最大1時間で全サーバーに反映される。  
すぐに確認したい場合は Discord を再起動する。

---

### Step 5: ゲームサーバーを作成する

ゲームごとに tfvars ファイルを作成して `terraform workspace` で管理する。

```bash
# 1. example.tfvars をコピーして編集
cp generic_game_server/games/example.tfvars generic_game_server/games/palworld.tfvars
# エディタで palworld.tfvars を開いて設定値を記入する

# 2. game-stack へ移動
cd generic_game_server/game-stack

# 3. backend.hcl.example をコピーしてバケット名を記入（初回のみ）
cp backend.hcl.example backend.hcl
# backend.hcl を開いて bucket = "<your-state-bucket>" を control-plane で作成した
# バケット名（terraform output -raw tf_state_bucket_name で確認）に変更

# 4. 初期化（S3 リモートバックエンドを使用）
terraform init -backend-config=backend.hcl

# 5. Palworld 用の workspace を作成
terraform workspace new palworld

# 6. デプロイ（初回は desired_count=1 で起動、または 0 で停止状態で作成）
terraform apply -var-file=../games/palworld.tfvars
```

---

## 日常運用

### Discord コマンド（推奨）

| コマンド | 説明 |
|---|---|
| `/games` | 全ゲームの稼働状態を一覧表示 |
| `/start game:palworld` | Palworld サーバーを起動 |
| `/stop game:palworld` | Palworld サーバーを停止 |
| `/status game:palworld` | 現在のIP アドレスを確認 |
| `/cost` | 今月の AWS コストと予算残高を確認 |
| `/update game:palworld` | サーバーを最新バージョンにアップデート（停止中の場合のみ実行可） |
| `/backup game:palworld` | 今すぐ EFS→S3 のバックアップを実行 |
| `/restore game:palworld` | S3→EFS へ最新バックアップをミラーリング（停止中のみ実行可） |
| `/switch-slot game:palworld slot:world2` | セーブデータのスロットを切り替え（停止中のみ実行可） |

> コマンドはあなただけに見えるメッセージ（エフェメラル）で応答するため、他のメンバーには表示されない。

### サーバー起動の流れ

1. Discord で `/start game:palworld` を実行
2. Lambda が ECS サービスの `desiredCount` を 1 に変更
3. 30秒〜1分後にタスクが起動し、IPアドレスが Discord チャンネルに自動通知される
4. 通知を見逃した場合は `/status game:palworld` で確認できる

### 自動停止の仕組み

- 全プレイヤーが切断してから `idle_timeout_minutes` 分後に自動停止
- 停止は ECS サービスの `desiredCount` を 0 にすることで実現（データは EFS に保存済み）
- 次の起動時にデータはそのまま継続される

---

## ゲームを追加する

新しいゲームは以下の手順で追加する。**Discord 側の再設定は不要**（Lambda が ECS の `Game` タグで自動発見する）。

```bash
# 1. tfvars を作成
cp generic_game_server/games/example.tfvars generic_game_server/games/minecraft.tfvars
# エディタで minecraft.tfvars を編集

# 2. 新しい workspace を作成してデプロイ
cd generic_game_server/game-stack
terraform workspace new minecraft
terraform apply -var-file=../games/minecraft.tfvars

# 3. Discord で確認
# /games を実行すると新しいゲームが一覧に表示される
```

### 主要な設定値（games/xxx.tfvars）

```hcl
game_name    = "palworld"          # ゲームを識別する名前（小文字英数字とハイフン）
aws_region   = "ap-northeast-1"

# コンテナ設定
docker_image = "thijsvanloef/palworld-server-docker:latest"
task_cpu     = 2048   # 256/512/1024/2048
task_memory  = 4096   # 512/1024/2048/3072/4096

# ゲームポート（複数指定可）
game_ports = [
  { port = 8211,  protocol = "udp", description = "Palworld ゲームポート" },
  { port = 27015, protocol = "udp", description = "Palworld クエリポート" },
]

# データの保存先（コンテナ内のパス）
efs_mount_path = "/palworld"

# 無人検知の方式: "tcp"（Minecraft等）/ "a2s"（汎用 Steam）/ "rest"（Palworld）
monitor_method       = "rest"
monitor_port         = 8211
idle_timeout_minutes = 10

# Discord Webhook URL（IP アドレスの通知先）
discord_webhook_url = "https://discord.com/api/webhooks/..."
```

> **`monitor_method` の選択が重要:** ゲームタイプに合った方式を選ぶと無人検知の精度が上がる。
> - `"tcp"` — Minecraft Java 版など TCP ゲーム（`ss` コマンドで接続数カウント）
> - `"a2s"` — Valheim・CS2 など A2S_INFO 対応の Steam ゲーム（Steam クエリプロトコル）
> - `"rest"` — Palworld（REST API `/v1/api/players` でプレイヤー数取得）

---

## ワークスペース管理

```bash
cd generic_game_server/game-stack

# 現在の workspace を確認
terraform workspace list

# workspace を切り替えて状態を確認
terraform workspace select palworld
terraform show

# ゲームを完全に削除する場合
terraform workspace select palworld
terraform destroy -var-file=../games/palworld.tfvars
terraform workspace select default
terraform workspace delete palworld
```

---

## コスト管理

- AWS Budgets で月$13の上限を設定済み（20/50/80/100% でアラート通知）
- コスト通知は Discord の Webhook チャンネルに届く
- 複数のゲームが同時に起動すると費用が倍増するため注意
- 詳細なコスト試算・削減手順 → [`docs/cost-plan.md`](docs/cost-plan.md)

## セーブデータ移行

ローカル/Co-op のセーブをダディケーテッドサーバーへ移したい場合は、バックアップ Lambda の `restore` アクションを使う。

- 汎用手順書 → [`docs/save-migration-runbook.md`](docs/save-migration-runbook.md)
- Palworld 固有の手順（ホストキャラ GUID 変換 / PlM 形式対応） → [`docs/palworld-save-migration.md`](docs/palworld-save-migration.md)

---

## トラブルシューティング

### Discord コマンドが反応しない
- `interactions_endpoint_url`（`terraform output -raw interactions_endpoint_url` で確認）が Interactions Endpoint URL に正しく登録されているか確認
- Lambda の CloudWatch Logs を確認: `aws logs tail /aws/lambda/game-server-discord-control --follow --region ap-northeast-1`

### サーバーが起動しない
- ECS サービスのイベントを確認: `aws ecs describe-services --cluster <cluster-name> --services <service-name>`
- EFS マウントターゲットが2つ存在するか確認
- タスク定義のコンテナイメージが正しいか確認

### IP 通知が届かない
- Lambda の CloudWatch Logs を確認: `aws logs tail /aws/lambda/<game>-<workspace>-notify-ip --follow`
- EventBridge ルールが有効になっているか確認
- `discord_webhook_url` が正しいか確認

### EFS の権限エラー（Permission denied）
- EFS アクセスポイントの `posix_user.uid/gid` がコンテナのユーザーと一致しているか確認
- デフォルトは uid/gid = 1000。コンテナイメージによって異なる場合はゲームイメージのドキュメントを参照し、`storage.tf` の `posix_user` を直接変更する

### game-stack の apply が「VPC not found」で失敗する
- control-plane を先に apply していない場合に発生する。共有 VPC（`ggs-shared-vpc` タグ）は control-plane が作成する
- `cd control-plane && terraform apply` を先に実行してから game-stack を apply する

---

## ファイル構成（詳細）

```
generic_game_server/
├── .gitignore                          # Terraform state、tfvars（example以外）を除外
├── README.md                           # このファイル
│
├── games/                              # ゲームごとの設定（機密情報を含むため git 管理外）
│   └── example.tfvars                  # テンプレート（コピーして使う）
│
├── game-stack/                         # ゲームサーバー本体の Terraform スタック
│   ├── versions.tf                     # プロバイダーバージョン指定
│   ├── variables.tf                    # 変数定義（バリデーション付き）
│   ├── network.tf                      # SG / データソース（共有 VPC 参照）/ locals
│   ├── storage.tf                      # EFS / アクセスポイント / マウントターゲット
│   ├── backup.tf                       # S3 バックアップバケット / バックアップ Lambda / EventBridge
│   ├── iam.tf                          # タスク実行ロール / タスクロール
│   ├── ecs.tf                          # ECS クラスター / タスク定義 / サービス
│   ├── cost_alerts.tf                   # コスト通知 Lambda / SNS / Budgets
│   ├── notify_ip.tf                     # IP通知 Lambda
│   ├── auto_update.tf                  # 手動アップデート Worker Lambda
│   ├── cost_guard.tf                   # 長時間稼働強制停止バックストップ Lambda
│   ├── outputs.tf                      # 接続情報 / 管理コマンド
│   ├── functions/
│   │   ├── _shared/notifier.py         # Discord/Slack Webhook 共有モジュール
│   │   ├── notify_ip/notify_ip.py      # タスク起動時の IP 通知 Lambda
│   │   ├── notify_cost/notify_cost.py  # コスト超過アラート通知 Lambda
│   │   ├── backup_efs/backup_efs.py    # EFS → S3 バックアップ Lambda
│   │   ├── auto_update/auto_update.py  # Steam アップデート Worker Lambda
│   │   └── cost_guard/cost_guard.py    # 長時間稼働強制停止 Lambda
│   └── scripts/
│       └── auto_shutdown.sh            # 無人検知・自動停止サイドカースクリプト
│
├── control-plane/                      # Discord ボットの Terraform スタック（1回だけ apply）
│   ├── versions.tf
│   ├── variables.tf                    # discord_public_key / aws_region
│   ├── main.tf                         # API Gateway v2 + Lambda + IAM
│   ├── network.tf                      # 全ゲーム共有 VPC / サブネット / IGW / S3 VPC Endpoint
│   ├── state.tf                        # Terraform state 管理用 S3 バケット
│   ├── ecr.tf                          # モニターサイドカー用 ECR リポジトリ（オプション）
│   ├── outputs.tf                      # interactions_endpoint_url（Portal への登録値）
│   ├── functions/
│   │   └── discord_control/
│   │       ├── index.py                # Lambda ハンドラ・deferred ワーカー起動
│   │       ├── clients.py              # boto3 クライアントの共有インスタンス
│   │       ├── ecs_helpers.py          # ECS/SSM 検索・状態取得の共通ロジック
│   │       ├── ed25519.py              # 署名検証（外部ライブラリ不要）
│   │       ├── provider.py             # Discord 固有プロトコル抽象層
│   │       └── commands/               # コマンド単位の実装（9 コマンド）
│   └── scripts/
│       └── register_commands.sh        # スラッシュコマンド一括登録ヘルパー
│
├── monitor/
│   └── Dockerfile                      # モニターサイドカー事前ビルド用イメージ（オプション）
│
└── docs/
    ├── cost-plan.md                    # コスト試算・削減指針
    ├── save-migration-runbook.md       # セーブデータ移行汎用手順書
    └── palworld-save-migration.md      # Palworld 固有の移行手順
```
