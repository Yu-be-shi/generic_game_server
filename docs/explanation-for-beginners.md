# リポジトリ技術解説（初学者向け）

> **対象読者**: AWS・Terraform・Python のコードは読んだことがあるが、このリポジトリの全体像がまだよくわからない方。  
> 専門用語は初出時に必ず噛み砕いた説明を添えています。巻末の[用語ミニ辞典](#5-用語ミニ辞典)も随時参照してください。

---

## 目次

1. [このリポジトリは何か](#0-このリポジトリは何か)
2. [機能別の全体像（流れで理解する）](#1-機能別の全体像流れで理解する)
3. [2スタック構成（ディレクトリマップ）](#2-2スタック構成ディレクトリマップ)
4. [サービス／ファイル別の説明](#3-サービスファイル別の説明)
   - [control-plane/（共有基盤・Discord ボット）](#3-1-control-plane共有基盤discord-ボット)
   - [game-stack/（ゲーム1本分のリソース）](#3-2-game-stack-ゲーム1本分のリソース)
   - [ルート / 共通ファイル](#3-3-ルート--共通ファイル)
5. [重要な仕組みの掘り下げ](#4-重要な仕組みの掘り下げ)
6. [用語ミニ辞典](#5-用語ミニ辞典)

---

## 0. このリポジトリは何か

一言でいうと:

> **「Discord のスラッシュコマンドだけで、遊ぶときだけ起動・誰もいないと自動停止するゲームサーバーを AWS 上に運用する仕組み」**

### 解決している問題

ゲームサーバーを EC2 などで常時起動すると、たとえば月 $60〜$100 かかります。しかし実際には 1 日のうち数時間しか使わない。このリポジトリは、**使っていないときはサーバーをゼロ（コスト $0）に近い状態で停止し、Discord コマンドで即起動できる**仕組みを作ります。

### 対応しているゲーム例

- **Palworld**（REST API で接続監視）
- **Minecraft Java**（TCP 接続で監視、ARM64 最適化も可）
- Steam サーバー全般（A2S プロトコルで監視）

---

### 前提知識: 使われている技術の簡単な説明

| 技術 | 一言説明 |
|------|---------|
| **AWS** | Amazon が提供するクラウドサービス群。サーバーや DB などをレンタル感覚で使える。 |
| **Terraform** | インフラを「コード」として管理するツール（IaC）。`terraform apply` で AWS に設定を自動反映できる。 |
| **コンテナ / Docker** | アプリをパッケージ化して「どの環境でも同じように動く」形にする技術。 |
| **AWS Fargate** | コンテナを動かすためのサーバーレスな実行環境。サーバー本体の管理が不要。 |
| **ECS** | Fargate 上でコンテナを管理するサービス。「どのコンテナを何台動かすか」を指定できる。 |
| **Lambda** | コードを「関数」単位で実行できるサービス。リクエストが来たときだけ起動し、使った分だけ課金。 |
| **VPC** | 仮想ネットワーク。AWS 上に「自分専用のプライベートなネットワーク空間」を作る。 |
| **EFS** | ネットワーク越しにマウントできる共有ストレージ（ゲームのセーブデータ保存に使う）。 |
| **S3** | オブジェクトストレージ（ファイルをバケットに格納）。セーブデータのバックアップに使う。 |
| **SSM Parameter Store** | AWS のキーバリューストア。コンテナと Lambda が「状態」を共有するための伝言板として使う。 |
| **EventBridge** | イベント配信サービス。「○○が変化したら Lambda を呼ぶ」といったルールを設定できる。 |
| **Discord Bot** | Discord のスラッシュコマンド（`/start` 等）を処理するアプリケーション。 |

---

## 1. 機能別の全体像（流れで理解する）

ユーザーが「サーバーで遊ぶ」という体験をするとき、内部では次の **7つの機能** が動きます。

---

### (A) コマンド受付

Discord でコマンドを入力すると、まず「それは正当なリクエストか」を確認し、処理します。

```
ユーザー
  ↓ /start palworld と入力
Discord サーバー
  ↓ HTTPS POST（署名付き）
API Gateway v2（AWS）
  ↓ ルーティング
Lambda: discord_control
  ├─ ed25519.py   → 署名を検証（偽リクエスト拒否）
  ├─ provider.py  → Discord の形式を解析
  ├─ index.py     → deferred ワーカー起動・ルーティング
  └─ commands/    → コマンドを処理して応答
```

関係ファイル: `control-plane/functions/discord_control/index.py`, `ed25519.py`, `provider.py`, `commands/`

> **Discord の制約**: Discord は「3 秒以内に応答せよ」というルールがあります。ECS の操作は 3 秒を超えることがあるため、Lambda は「まず仮応答を返し、処理を別の Lambda 呼び出しに委ねる」 **deferred（遅延）方式**を使っています。

---

### (B) サーバー起動

`/start` コマンドが来ると、ECS サービスの「起動台数」を 0 → 1 に変えます。

```
commands/start.py (cmd_start)
  ↓ ECS UpdateService (desired-count=1)
ECS サービス
  ↓ タスクを起動
Fargate タスク
  ├─ ゲームコンテナ  （例: palworld-server）
  └─ モニターサイドカー（auto_shutdown.sh が動く）
```

関係ファイル: `control-plane/functions/discord_control/index.py`, `game-stack/ecs.tf`

> **サイドカー**: タスクの中に「ゲーム本体」と「監視専用の付き人コンテナ」の2つが同居します。監視専用をサイドカーと呼びます。

---

### (C) 起動完了通知

ゲームが接続可能になると、Discord に「IPアドレス:ポート」が送られます。

```
auto_shutdown.sh (フェーズA)
  ↓ ポート/API をポーリング（接続可能になるまで待つ）
  ↓ SSM: /ggs/<game>/ready = "1" を書込み
EventBridge ルール (server_ready)
  ↓ SSM パラメータの変化を検知
Lambda: notify_ip
  ↓ ECS タスクの ENI → パブリック IP を取得
  ↓ Discord Webhook
ユーザーに「接続先: X.X.X.X:8211」と通知
```

関係ファイル: `game-stack/scripts/auto_shutdown.sh`, `game-stack/notify_ip.tf`, `game-stack/functions/notify_ip/notify_ip.py`

---

### (D) 自動停止

誰もいなくなると、バックアップをとってサーバーを停止します。

```
auto_shutdown.sh (フェーズB)
  ↓ プレイヤー数を定期チェック（既定: 60秒おき）
  ↓ プレイヤー = 0 が idle_timeout_minutes（例: 30分）続いたら
  ↓ EFS → S3 バックアップ（セーブデータを同期）
  ↓ ECS UpdateService (desired-count=0)
タスク STOPPED
  ↓ EventBridge ルール (ecs_stopped)
Lambda: notify_ip
  ↓ Discord に停止通知
```

関係ファイル: `game-stack/scripts/auto_shutdown.sh`, `game-stack/notify_ip.tf`

---

### (E) データ永続化

ゲームのセーブデータは **EFS**（ネットワークストレージ）に置かれ、停止のたびに **S3** にバックアップされます。

```
ゲームコンテナ
  ↓ EFS をマウント（/palworld/ 等）
  ↓ セーブデータを書き込む
停止時: auto_shutdown.sh
  ↓ aws s3 sync /palworld/ s3://バックアップバケット/
  ↓ 差分のみ転送
24時間ごと: backup_efs Lambda（定期バックアップ）
  ↓ EventBridge rate(24 hours) → Lambda → S3 sync
```

関係ファイル: `game-stack/storage.tf`, `game-stack/backup.tf`, `game-stack/functions/backup_efs/backup_efs.py`

---

### (F) コスト暴走防止（3層構造）

「サイドカーが落ちてサーバーが止まらない」という最悪ケースに備え、3重の安全網があります。

```
【第1層: ソフト停止】
  auto_shutdown.sh（サイドカー）
  → アイドル検知 → 自力で ECS を stopped（desired-count=0）

【第2層: ハード停止】
  cost_guard Lambda（1時間おきに EventBridge から呼ばれる）
  → タスクが MAX_RUNTIME_HOURS（例: 24時間）を超えていたら強制 stop_task

【第3層: コストアラート】
  AWS Budgets → SNS → notify_cost Lambda → Discord に通知
  「今月 80% 消費しました」等のアラートが来る
```

関係ファイル: `game-stack/cost_guard.tf`, `game-stack/functions/cost_guard/cost_guard.py`, `game-stack/cost_alerts.tf`, `game-stack/functions/notify_cost/notify_cost.py`

---

### (G) アップデート

`/update palworld` コマンドで、ゲームサーバーを停止せずに新バージョンに更新できます。

```
commands/update.py (cmd_update)
  ↓ auto_update Lambda を非同期 invoke
auto_update.py
  ↓ 通常サービスとは別の「ワンオフタスク」を run_task で起動
  ├─ ゲームコンテナ: UPDATE_ON_BOOT=true で SteamCMD アップデート実行
  └─ モニターサイドカー: ダミー設定（通常サービスへの誤操作防止）
  ↓ SSM: update_ready = "1" を検知したら stop_task
  ↓ Discord に「アップデート完了」通知
```

関係ファイル: `game-stack/auto_update.tf`, `game-stack/functions/auto_update/auto_update.py`

---

## 2. 2スタック構成（ディレクトリマップ）

```
generic_game_server/
│
├── control-plane/           ← ① 共有基盤（アカウントに1回だけデプロイ）
│   ├── main.tf              　 API Gateway + Discord 制御 Lambda + IAM
│   ├── network.tf           　 全ゲームで共有する VPC
│   ├── ecr.tf               　 モニター用コンテナリポジトリ
│   ├── state.tf             　 Terraform state 保存用 S3 バケット
│   ├── variables.tf / outputs.tf / versions.tf / backend.hcl
│   └── functions/
│       └── discord_control/
│           ├── index.py     　 Lambda ハンドラ・deferred ワーカー起動
│           ├── clients.py   　 boto3 クライアントの共有インスタンス
│           ├── ecs_helpers.py 　 ECS/SSM 検索・状態取得の共通ロジック
│           ├── ed25519.py   　 署名検証（純 Python 実装）
│           ├── provider.py  　 Discord 形式を抽象化
│           └── commands/    　 コマンドごとの処理本体（games.py, start.py, ...）
│
├── game-stack/              ← ② ゲーム1本分（workspace ごとに独立）
│   ├── ecs.tf               　 ECS クラスター・タスク定義・サービス
│   ├── network.tf           　 共有 VPC の参照 + ゲーム専用セキュリティグループ
│   ├── iam.tf               　 ECS タスク用 IAM ロール
│   ├── storage.tf           　 EFS（セーブデータ）
│   ├── backup.tf            　 S3 + backup Lambda
│   ├── auto_update.tf       　 アップデート Lambda
│   ├── cost_guard.tf        　 コストガード Lambda
│   ├── cost_alerts.tf       　 コスト通知・Budgets
│   ├── notify_ip.tf         　 IP 通知
│   ├── variables.tf / outputs.tf / versions.tf / backend.hcl
│   ├── functions/
│   │   ├── _shared/notifier.py    共有: Webhook 送信ユーティリティ
│   │   ├── notify_ip/             サーバー起動・停止通知
│   │   ├── notify_cost/           コストアラート転送
│   │   ├── auto_update/           ゲームアップデート
│   │   ├── cost_guard/            ハード停止バックストップ
│   │   └── backup_efs/            EFS→S3 バックアップ
│   └── scripts/
│       └── auto_shutdown.sh      モニターサイドカー本体
│
├── monitor/
│   └── Dockerfile           　 サイドカー用コンテナイメージ（事前ビルド版）
│
├── games/
│   ├── example.tfvars       　 設定テンプレート
│   └── palworld.tfvars      　 Palworld 用の実設定（.gitignore 済み）
│
└── docs/
    ├── architecture/        　 構成図（diagram.py で生成）
    ├── cost-plan.md
    ├── design-alternatives.md
    └── explanation-for-beginners.md  ← このファイル
```

### なぜ2つに分けるのか？

| | control-plane | game-stack |
|---|---|---|
| **役割** | 全ゲーム共通の基盤 | ゲーム1本の専用リソース |
| **デプロイ頻度** | アカウントに1回だけ | ゲームを追加するたびに |
| **Terraform workspace** | `default` のみ | ゲームごと（例: `palworld`） |
| **VPC** | ここで作成 | control-plane の VPC をタグ参照 |

> **デプロイ順序**: control-plane を先に `apply` して VPC を作成してから、game-stack を `apply` する必要があります。game-stack は VPC を「`ggs-shared-vpc` というタグの VPC を探す」という方法（data source）で参照するため、先に VPC がないと失敗します。

---

## 3. サービス／ファイル別の説明

---

### 3-1. control-plane/（共有基盤・Discord ボット）

#### Terraform ファイル

| ファイル | 役割 |
|---------|------|
| `versions.tf` | Terraform のバージョン制約・AWS プロバイダー・S3 backend の設定。 |
| `variables.tf` | `discord_public_key`（Discord の公開鍵）・`aws_region`・`discord_allowed_user_ids` などの入力変数定義。 |
| `network.tf` | **全ゲームで共有する VPC** を作成する。2つのパブリックサブネット・インターネットゲートウェイ・ルートテーブル・S3 VPC Endpoint（NAT なしで S3 に届かせるため）を含む。 |
| `main.tf` | Discord ボットの本体を構築する。API Gateway v2（HTTPS エンドポイント）と Lambda（`module "discord_control_lambda"` として `../modules/lambda_function` 経由で定義）を接続し、必要な IAM 権限を付与する。 |
| `ecr.tf` | モニターサイドカー用の Docker イメージを保管する ECR リポジトリを作成する（オプション。使うと起動時の `dnf install` を省略でき速くなる）。 |
| `state.tf` | Terraform の状態ファイル（`terraform.tfstate`）を保存する S3 バケットを作成する。バージョニング・暗号化・削除防止（`prevent_destroy`）付き。 |
| `outputs.tf` | `apply` 後に表示する値。`interactions_endpoint_url`（Discord Developer Portal に登録する URL）や VPC ID・サブネット ID など。 |
| `backend.hcl` | S3 backend の接続先設定（バケット名・キー・リージョン）。コードから分離しておくことで、機密情報を Git にコミットしにくくする。`.gitignore` 対象。 |

#### Python ファイル（functions/discord_control/）

| ファイル | 役割 |
|---------|------|
| `index.py` | **Lambda ハンドラ本体**。署名検証・deferred ワーカーの起動と実行を担う。Discord の「3 秒制限」に対応するため、自分自身を非同期 invoke する **deferred worker 方式**を実装している。コマンドごとの処理ロジックは `commands/` パッケージに委譲する。 |
| `clients.py` | ECS/EC2/SSM/Cost Explorer/Budgets/STS/Lambda の boto3 クライアントを一度だけ生成し、`index.py`・`ecs_helpers.py`・`commands/` 配下から共有する。 |
| `ecs_helpers.py` | ECS クラスター/サービスの検索、SSM ステータス読み取りなど、複数コマンドで共通する AWS 読み取りロジックを集約。 |
| `ed25519.py` | **Discord からのリクエストが本物かを確認する署名検証モジュール**。Ed25519 という暗号方式を外部ライブラリなしに純 Python で実装。Lambda に依存パッケージを追加せずに済む。 |
| `provider.py` | **Discord 固有の処理を切り離した抽象化レイヤー**。「署名の確認」「リクエストの解析」「レスポンスの整形」「Webhook への送信」を担う。`MESSAGING_PROVIDER=slack` にすると Slack にも切り替えられる設計。 |
| `commands/` | 9 つのスラッシュコマンド（`/games` `/start` `/stop` `/status` `/cost` `/update` `/backup` `/restore` `/switch-slot`）をコマンド単位のファイルに分割した実装。`guards.py` に `/update` `/backup` `/restore` `/switch-slot` 共通のガード処理（起動中チェック・メンテナンス中チェック・Worker Lambda 非同期 invoke）を集約し、`__init__.py` がコマンド名からのルーティングとオートコンプリートを担う。 |

#### シェルスクリプト

| ファイル | 役割 |
|---------|------|
| `scripts/register_commands.sh` | Discord にスラッシュコマンドを登録する初回セットアップ用スクリプト。`DISCORD_APP_ID`・`DISCORD_BOT_TOKEN` を環境変数でわたし、Discord API v10 に PUT して 9 コマンドを一括登録する。 |

---

### 3-2. game-stack/（ゲーム1本分のリソース）

#### Terraform ファイル（12 個）

| ファイル | 役割 | 主要 AWS リソース |
|---------|------|-----------------|
| `versions.tf` | バージョン・プロバイダー・S3 backend・デフォルトタグ設定。 | - |
| `variables.tf` | ゲームごとの設定値（24 個）を定義。`game_name`, `docker_image`, `task_cpu`, `monitor_method`, `idle_timeout_minutes`, `discord_webhook_url` など。 | - |
| `network.tf` | 共有 VPC をタグで検索して参照し、**ゲーム専用のセキュリティグループ**（ファイアウォール）を作成する。ゲームポートだけを許可する。 | `aws_security_group.game`, `aws_security_group.efs` |
| `iam.tf` | ECS タスクが AWS API を呼べるように IAM ロールと権限を定義する。「タスク実行ロール」（コンテナ起動用）と「タスクロール」（タスク内から AWS API を呼ぶ用）の2種類。 | `aws_iam_role.task_execution`, `aws_iam_role.task` |
| `storage.tf` | ゲームのセーブデータを保存する **EFS**（ネットワークストレージ）を作成する。暗号化・自動 IA/Archive 階層化・誤削除防止（`prevent_destroy`）付き。 | `aws_efs_file_system.main`, `aws_efs_mount_target.main`, `aws_efs_access_point.main` |
| `ecs.tf` | ECS の主要リソース（クラスター・タスク定義・サービス）を定義する。**2 コンテナ構成**（ゲーム本体 + モニターサイドカー）と EFS マウントを設定する。 | `aws_ecs_cluster.game`, `aws_ecs_task_definition.game`, `aws_ecs_service.game` |
| `backup.tf` | EFS セーブデータを S3 にバックアップする仕組みを作る。24 時間ごとに EventBridge が Lambda を起動して差分同期する。 | `aws_s3_bucket.backup`, `module.backup_efs_lambda`, `module.backup_schedule_trigger` |
| `auto_update.tf` | Discord `/update` コマンドから呼ばれる**アップデート Lambda** を定義する。通常サービスを止めずに専用タスクでゲームを更新する。 | `module.auto_update_lambda` |
| `cost_guard.tf` | **コストガード Lambda**（ハード停止の安全網）を定義する。1 時間ごとに起動し、実行時間が上限を超えたタスクを強制停止する。 | `module.cost_guard_lambda`, `module.cost_guard_trigger` |
| `notify_ip.tf` | サーバー起動時の IP 通知（+停止通知）を定義する（EventBridge → Lambda → Discord）。 | `module.notify_ip_lambda`, `module.server_ready_trigger`, `module.ecs_stopped_trigger` |
| `cost_alerts.tf` | コスト超過アラートを定義する（AWS Budgets → SNS → Lambda → Discord）。失敗時の SQS DLQ も含む。 | `module.notify_cost_lambda`, `aws_budgets_budget.monthly`, `aws_sns_topic.cost_alert`, `aws_sqs_queue.notify_cost_dlq` |
| `outputs.tf` | `apply` 後に表示する値。ECS クラスター名・EFS ID・バックアップバケット名・よく使う AWS CLI コマンドのサンプルなど。 | - |

#### Python ファイル（functions/）

| ファイル | 役割 |
|---------|------|
| `_shared/notifier.py` | **複数 Lambda で共有する Webhook 送信ユーティリティ**。`MESSAGING_PROVIDER` 環境変数（既定 `discord`）で Discord か Slack かを切り替える。`send_message(text)` 一発で送れる。 |
| `notify_ip/notify_ip.py` | **サーバー起動・停止を Discord に通知する Lambda**。EventBridge から2パターンで呼ばれる: SSM の `ready=1` 変化（IP 通知）と ECS タスク STOPPED（停止通知）。同じタスクへの重複通知を排除する仕組みあり。 |
| `notify_cost/notify_cost.py` | **コストアラートを Discord に転送する Lambda**。AWS Budgets の警告が SNS 経由で届き、内容を整形して Webhook で送る。 |
| `auto_update/auto_update.py` | **ゲームアップデートを行う Lambda**。`run_task` でワンオフタスクを起動し SteamCMD 更新を実行。SSM `update_ready=1` を 15 秒おきにポーリングして完了を検知する。アップデート中は SSM `maintenance=1` をセットして `/start` をブロックする。 |
| `cost_guard/cost_guard.py` | **最終安全網の Lambda**。1 時間ごとに RUNNING タスクの起動時刻を確認し、`MAX_RUNTIME_HOURS` を超えていれば `stop_task` と `update-service --desired-count 0` で強制停止する。バックストップ自身が落ちないよう例外を飲み込む設計。 |
| `backup_efs/backup_efs.py` | **EFS → S3 バックアップ Lambda**。`action=backup` でサイズ比較による差分同期。`action=restore` で S3 から EFS に戻す（ZIP 展開・Zip-Slip 対策付き）。 |

#### シェルスクリプト

| ファイル | 役割 |
|---------|------|
| `scripts/auto_shutdown.sh` | **モニターサイドカーの本体**。ゲームコンテナと同じ Fargate タスク内で動き、ゲームの「準備完了」と「プレイヤー数」を監視する。2 フェーズで動作する（詳細は[4章](#4-重要な仕組みの掘り下げ)）。Terraform がコンテナ定義にスクリプトを注入するので、イメージの再ビルドなしに更新できる。 |

#### コンテナイメージ

| ファイル | 役割 |
|---------|------|
| `monitor/Dockerfile` | サイドカー用の**事前ビルドコンテナイメージ**。`amazonlinux:2023` をベースに `iproute`・`python3`・`awscli v2` を事前インストールしておく。これを使うと起動ごとの `dnf install` をスキップでき、Ready になるまでの時間が短くなる。スクリプト自体は Terraform が注入するのでイメージを焼き直さなくてよい。 |

---

### 3-3. ルート / 共通ファイル

| ファイル・ディレクトリ | 役割 |
|-----------------------|------|
| `.terraform-version` | `1.5.6` と記載。`tfenv`（Terraform バージョン管理ツール）がこのファイルを読んで自動的に正しいバージョンを使う。 |
| `.gitignore` | `*.tfvars`（Webhook URL 等の機密）や生成物（`.terraform/`・`*.zip`）を Git の管理外にする設定。 |
| `games/example.tfvars` | ゲームを追加するときのテンプレートファイル。すべての変数の説明と記入例が書かれている。 |
| `games/palworld.tfvars` | Palworld 用の実設定。`docker_image`・`task_cpu/memory`・ポート・`monitor_method=rest`・`discord_webhook_url` 等を記載。`.gitignore` 済みで Git にはコミットされない。 |
| `docs/architecture/` | AWS 構成図（`architecture.svg/png`）と生成スクリプト（`diagram.py`）。`diagrams` ライブラリを使って Python コードで図を描く。 |
| `docs/cost-plan.md` | コスト試算・最適化戦略のドキュメント。 |
| `docs/design-alternatives.md` | 採用しなかった代替設計案の記録（なぜ今の設計を選んだかの説明）。 |
| `docs/save-migration-runbook.md` | セーブデータ移行手順書。 |

---

## 4. 重要な仕組みの掘り下げ

---

### 4-1. モニターサイドカーの2フェーズ動作

`game-stack/scripts/auto_shutdown.sh` はタスク起動からシャットダウンまで、2 フェーズで動きます。

```
タスク起動
  │
  ▼
[初期化]
  - 依存ツールを確認（aws cli, ss コマンド。なければ dnf/pip でインストール）
  - SSM: ready = "0" に初期化（前回の stale な ready=1 を消す）
  │
  ▼
[フェーズ A: 起動待ち（Ready になるまで）]
  ループ（READY_POLL_INTERVAL 秒ごと、既定 10 秒）
    → check_status() でポートや API を確認
    → 接続可能になったら:
        ・SSM: buildid を書込み（Steam ゲームのみ）
        ・SSM: ready = "1" を書込み
            → EventBridge が検知 → notify_ip Lambda → Discord に IP 通知
    → STARTUP_GRACE_MINUTES（既定 30 分）を超えたら自動停止
  │
  ▼
[フェーズ B: アイドル監視（接続中かどうかを監視）]
  ループ（CHECK_INTERVAL 秒ごと、既定 60 秒）
    → check_status() でプレイヤー数を確認
    → プレイヤー数 > 0 なら idle カウンターをリセット
    → プレイヤー数 = 0 が IDLE_MINUTES 分続いたら:
        → do_shutdown():
            ・EFS → S3 バックアップ
            ・ECS UpdateService desired-count=0
            ・exit
```

---

### 4-2. monitor_method（tcp / a2s / rest）の使い分け

ゲームによって「接続できるか」「何人いるか」の確認方法が違います。

| method | 使う場面 | 仕組み |
|--------|---------|--------|
| `tcp` | Minecraft Java など | `ss -tlnH` でポートが LISTEN 中か確認。接続数は `ss -tnH state established` でカウント。 |
| `a2s` | Steam ゲーム全般 | Steam の **A2S_INFO** プロトコルでゲームに直接問い合わせる。Python インラインコードで実装。 |
| `rest` | Palworld など | ゲームの REST API（`GET /v1/api/players`）を Python でコール。401/403 は「プレイヤー数不明」として扱う。 |

**設定の流れ**:

```
games/palworld.tfvars
  monitor_method = "rest"
      ↓ terraform apply
game-stack/variables.tf → 値を受け取る
      ↓
game-stack/ecs.tf → モニターコンテナの環境変数に MONITOR_METHOD="rest" を注入
      ↓
Fargate タスク内の auto_shutdown.sh → check_status() で $MONITOR_METHOD を読み取り、REST API を呼ぶ
```

---

### 4-3. SSM パラメータストア = コンテナ↔Lambda の伝言板

コンテナ（Fargate）と Lambda は直接通信できません。そこで **SSM Parameter Store** を「共有の掲示板」として使います。

```
名前空間: /ggs/<name_prefix>/
           例: /ggs/palworld-palworld/
```

| パラメータ名 | 書く人 | 読む人 | 意味 |
|------------|--------|--------|------|
| `ready` | auto_shutdown.sh / `/start` コマンド | notify_ip, `/status` | ゲームが接続を受け付けているか（0/1） |
| `players` | auto_shutdown.sh | `/status` | 現在のプレイヤー数 |
| `maintenance` | auto_update Lambda | `/start`, `/update` | アップデート中は起動ブロック（0/1） |
| `installed_buildid` | auto_shutdown.sh | auto_update Lambda | インストール済みの Steam ビルド ID |
| `update_ready` | auto_shutdown.sh（updateタスク時のみ）| auto_update Lambda | アップデートタスクの完了シグナル |
| `notified_task` | notify_ip Lambda | notify_ip Lambda | 通知済みタスク ARN（重複通知防止） |

---

### 4-4. ECS クラスタータグによるゲーム自動検出

Discord ボット（`commands/`）は「どんなゲームがあるか」をハードコードしていません。代わりに **ECS クラスターのタグ**を動的に読んで判断します。

```python
# ecs_helpers.py の list_game_names() イメージ
clusters = ecs.list_clusters()
for cluster_arn in clusters:
    tags = ecs.list_tags_for_resource(cluster_arn)
    if tags.get("Game"):
        # このクラスターはゲームサーバー！
        game_name = tags["Game"]                        # Discord コマンドに表示
        ssm_prefix = tags["StatusParamPrefix"]          # SSM を読む場所
        update_func = tags["AutoUpdateFunction"]        # /update で呼ぶ Lambda 名
```

| タグ名 | 値の例 | 使われる場面 |
|--------|--------|------------|
| `Game` | `palworld` | `/games`, `/start`, `/stop`, `/status` のオートコンプリート候補 |
| `StatusParamPrefix` | `/ggs/palworld-palworld` | `/status` が SSM パラメータを読む場所 |
| `AutoUpdateFunction` | `palworld-palworld-auto-update` | `/update` で呼ぶ Lambda 関数名 |

→ 新しいゲームを `game-stack` でデプロイするだけで、ボット側の再デプロイなしに `/games` のリストに追加されます。

---

### 4-5. `ignore_changes` と最新タスク定義の解決

```hcl
# game-stack/ecs.tf
resource "aws_ecs_service" "game" {
  lifecycle {
    ignore_changes = [task_definition]
  }
}
```

**なぜこうするか**: `terraform apply` するたびに ECS サービスが「現在動いているタスク」を新しいリビジョンに切り替えてしまうと、サーバーが再起動してしまいます。これを避けるために Terraform にはタスク定義の変更を無視させています。

**では `/start` はどうやって最新バージョンを使うのか**: `ecs_helpers.py` の `get_latest_task_def_arn()` が `ecs.describe_task_definition(family)` で最新の **ACTIVE** リビジョンを実行時に解決し、`run_task` 時に明示的に指定します。

---

## 5. 用語ミニ辞典

| 用語 | 説明 |
|------|------|
| **IaC（Infrastructure as Code）** | インフラの構成をコードで管理する考え方。Terraform がその代表例。 |
| **Terraform workspace** | 同じ Terraform コードを「複数の独立した環境」に適用するための機能。ゲームごとに state を分けられる。 |
| **ECS（Elastic Container Service）** | AWS のコンテナ管理サービス。どのコンテナを何台起動するかを制御する。 |
| **Fargate** | ECS のサーバーレス実行環境。EC2（仮想マシン）を自分で管理しなくてよい。 |
| **タスク定義** | ECS でどのコンテナを、どのリソース（CPU/メモリ）で動かすかを定義したテンプレート。 |
| **desired-count** | ECS サービスが「何台のタスクを維持すべきか」を示す設定値。0 にするとサーバーが停止する。 |
| **サイドカーコンテナ** | メインコンテナと同じタスク内で動く補助コンテナ。ここではゲーム監視・自動停止を担う。 |
| **EFS（Elastic File System）** | 複数のコンテナから同時にマウントできるネットワークストレージ。ゲームのセーブデータ保存に使う。 |
| **S3（Simple Storage Service）** | AWS のオブジェクトストレージ。ファイルをバケットに格納する。バックアップや Terraform state に使う。 |
| **Lambda** | コードをサーバーレスで実行する AWS サービス。起動時だけ課金され、待機中はコスト $0。 |
| **API Gateway v2** | HTTP API を作成するサービス。Discord からの HTTPS リクエストを Lambda に転送する。 |
| **EventBridge** | AWS のイベントバス。「○○が変化したら△△を呼ぶ」というルールを設定できる。 |
| **SSM Parameter Store** | AWS のキーバリューストア。コンテナと Lambda の間で状態（ready/players 等）を共有する伝言板として使う。 |
| **IAM ロール** | AWS リソースに「この操作をする権限」を与える仕組み。最小権限の原則に基づいて設定する。 |
| **セキュリティグループ** | AWS の仮想ファイアウォール。どの IP・ポートからの通信を許可するかを定義する。 |
| **SNS（Simple Notification Service）** | AWS のメッセージ配信サービス。Budgets のアラートを Lambda に届けるために使う。 |
| **SQS（Simple Queue Service）** | AWS のメッセージキューサービス。Lambda の失敗時の「デッドレターキュー（DLQ）」として使う。 |
| **ECR（Elastic Container Registry）** | AWS のコンテナイメージリポジトリ。Docker Hub の代わりに使える。 |
| **VPC Endpoint（S3）** | VPC からインターネットを経由せずに S3 にアクセスするための経路。NAT ゲートウェイなしで S3 と通信できる。 |
| **Ed25519** | 楕円曲線暗号の一種。Discord がリクエストの正当性を証明するために使う署名方式。 |
| **A2S_INFO** | Steam ゲームサーバーに問い合わせる標準プロトコル。プレイヤー数・マップ名などを取得できる。 |
| **deferred（遅延）方式** | Discord の「3 秒ルール」対応策。まず仮応答を返し、実際の処理は別の Lambda 呼び出しで非同期に行う。 |
| **backend.hcl** | Terraform の S3 backend 接続先設定を本体コード（`.tf`）から分離したファイル。`terraform init -backend-config=backend.hcl` で読み込む。 |
| **archive_file（data source）** | Terraform が Python ファイルを ZIP 化して Lambda にデプロイするための仕組み。手動パッケージング不要。 |
| **prevent_destroy** | Terraform の `lifecycle` 設定。`terraform destroy` しても削除されないようにする誤操作防止ガード。 |
| **DLQ（Dead Letter Queue）** | Lambda 等が失敗したときにメッセージを逃がすキュー。後から原因調査や再試行ができる。 |
