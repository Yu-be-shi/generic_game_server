# CLAUDE.md

このファイルは、リポジトリのコードを操作する際に Claude Code (claude.ai/code) へ提供するガイダンスです。

## プロジェクト概要

オンデマンドのゲームサーバー（Palworld、Minecraft など）をほぼゼロのアイドルコストで運用するための AWS インフラ。制御は Discord スラッシュコマンドのみで行う。Makefile もテストスイートも存在しない — 純粋な Terraform + Python + Bash プロジェクト。

## 構成図

![Architecture](docs/architecture/architecture.png)

再生成: `docs/architecture/.venv/bin/python docs/architecture/diagram.py`（セットアップ詳細: [docs/architecture/README.md](docs/architecture/README.md)）

## スタック

- **IaC:** Terraform 1.15.6（`.terraform-version` で強制）
- **コンピューティング:** AWS ECS Fargate（パブリックサブネット、固定コスト削減のため NAT なし）
- **ストレージ:** AWS EFS（永続セーブデータ）+ S3（バックアップ、tfstate）
- **コントロールプレーン:** Discord スラッシュコマンド → API Gateway v2 HTTP API → Lambda
- **Lambda ランタイム:** Python 3.12；外部依存なし（ed25519 検証は純粋な Python 実装）

## 2 スタック構成

### `control-plane/` — AWS アカウントに 1 度だけデプロイ

Discord ボットと**全ゲームで共有する VPC** をホスト。単一の Lambda がすべてのスラッシュコマンド（`/games`、`/start`、`/stop`、`/status`、`/cost`、`/update`、`/backup`、`/restore`、`/switch-slot`、`/launch-mode`）を処理し、ECS/SSM/Cost Explorer API を直接呼び出す。

**権限モデル（2 段階）**: 破壊的・コスト影響のあるコマンド（`/start` `/stop` `/update` `/backup` `/restore` `/switch-slot` `/launch-mode` = `constants.py` の `RESTRICTED_COMMANDS`）のみ `discord_allowed_user_ids` の許可リストで制限され、閲覧系コマンド（`/games` `/status` `/cost`）は誰でも実行できる。許可リストが未設定（空）の場合は全コマンドを全員に許可（後方互換）。新しい破壊的コマンドを追加する際は `RESTRICTED_COMMANDS` への追加を忘れないこと。

```
Discord POST → API Gateway v2 HTTP API → Lambda (index.py)
  ├─ ed25519.py: リクエスト署名の検証
  ├─ provider.py: Discord 固有のレスポンスフォーマット
  └─ ECS/SSM/Cost Explorer へディスパッチ

共有ネットワーク（network.tf）:
  └─ aws_vpc "ggs-shared-vpc" + 2 パブリックサブネット + IGW + S3 VPC Endpoint
     ↑ game-stack はタグ "ggs-shared-vpc" / "ggs-shared=true" でこれを参照する
```

注: Lambda Function URL はアカウントレベルのパブリックアクセス設定によりブロックされるため使用しない。代わりに API Gateway v2 を使用（同様のゼロ固定コストモデル）。

**デプロイ順序**: control-plane を先に apply して共有 VPC を作成してから、game-stack を apply すること。

### `game-stack/` — ゲームごとに 1 つの Terraform ワークスペース

各ゲーム（`terraform workspace new palworld`）は独立した AWS リソースを持つ：ECS クラスター/サービス、EFS、S3 バックアップバケット、および複数の Lambda。**VPC は control-plane の共有 VPC をタグルックアップで参照する**（ゲームごとに作成しない）。ゲームごとのセキュリティグループ（SG）は個別に作成し、ネットワーク的な分離は維持する。

```
ECS タスク（コンテナ 2 つ）:
  ├─ ゲームコンテナ (essential=true) — 実際のゲームサーバー
  └─ モニターサイドカー (essential=false, auto_shutdown.sh):
       1. 依存パッケージをインストール (dnf)、SSM ready=0 で初期化
       2. フェーズ A: ゲームポートが準備完了になるまでポーリング → SSM に ready=1 + buildid を書き込み
       3. フェーズ B: プレイヤー数をポーリング → idle_timeout_minutes 間アイドルなら停止
       4. EFS を S3 へバックアップ、その後 ECS サービスを停止 (desired-count=0)

EventBridge ルール:
  ├─ SSM /ggs/<prefix>/ready が "1" に変化 → notify_ip Lambda → Discord（IP を送信）
  ├─ ECS タスク STOPPED → notify_ip Lambda → Discord（停止通知）
  └─ スケジュール → cost_guard Lambda（max_task_runtime_hours でハード停止）

コストアラート:
  AWS Budgets → SNS → notify_cost Lambda → Discord webhook（+ 失敗時の SQS DLQ）
  ※ コスト系通知（notify_cost・cost_guard）は AWS アカウント ID 等を含むため、
    `admin_webhook_url` を設定すると管理者専用チャンネルへ分離できる（未設定なら一般チャンネル）
```

すべてのリソース名は `${game_name}-${workspace}-` でプレフィックスされる。ただし game_name と workspace が同名の場合は重複を避けて縮約される（例：game_name=palworld, workspace=palworld → `palworld-cluster`。workspace=palworld2 なら `palworld-palworld2-cluster`）。

## ECS クラスタータグ（サービスディスカバリ）

コントロールプレーンの Lambda は ECS クラスタータグを検査してゲームを動的に検出する — ゲームを追加しても再デプロイ不要：

| タグ | 値 | 使用箇所 |
|-----|-------|---------|
| `Game` | ゲーム名（例：`palworld`）| `/games`、`/start`、`/stop`、`/status` のオートコンプリート |
| `StatusParamPrefix` | `/ggs/<name_prefix>` | `/status` がこのプレフィックスから SSM パラメータを読む |
| `AutoUpdateFunction` | `<name_prefix>-auto-update` | `/update` がこの名前で Lambda を呼び出す |
| `BackupFunction` | `<name_prefix>-backup-efs` | `/backup`、`/restore` がこの名前で Lambda を呼び出す |

## SSM パラメータ名前空間

すべてのゲームステータスパラメータは `/ggs/<name_prefix>/` 以下に存在する（例：`/ggs/palworld/`）：

| パラメータ | 書き込み元 | 読み取り元 | 用途 |
|-----------|--------|--------|---------|
| `ready` | モニターサイドカー、`/start` で 0 にリセット | notify_ip、`/status` | ゲームが接続受け付け中か（0/1）|
| `players` | モニターサイドカー | `/status` | 現在のプレイヤー数 |
| `notified_task` | notify_ip Lambda | `/status` | 通知済みタスク ARN（重複排除）|
| `maintenance` | auto_update Lambda | `/start`、`/update` | アップデート中の起動ブロック（0/1）|
| `installed_buildid` | auto_update Lambda（アップデート成功時）| auto_update Lambda | インストール済み Steam ビルド ID |
| `update_ready` | モニターサイドカー（アップデートタスクのみ）| auto_update Lambda | アップデートタスク完了シグナル |
| `active_slot` | notify_backup Lambda（switch_slot 完了時のミラー）| `/status`、`/switch-slot` の同一スロットガード | 使用中のセーブデータスロット名（正本は S3 の `slots/_active_slot`）|
| `launch_mode` | `/launch-mode` コマンド | `/start`、`/status` | 起動タイプ `spot`/`ondemand`（未作成 = ondemand 相当の従来動作）|

## Lambda パッケージング

Lambda ZIP ファイルは `terraform apply` のたびに `modules/lambda_package` モジュール（内部は `archive_file` データソース）によってビルドされる。手動のパッケージング手順は不要。呼び出し元が `source_dir`（ハンドラディレクトリ）と `shared_files`（`game-stack/functions/_shared/` から同梱するファイル名リスト）を指定し、各ゲームスタック Lambda（notify_ip、notify_cost、cost_guard、auto_update、backup_efs）に必要な共有モジュール（`{notifier,aws_clients,ssm_params,ecs_net}.py`）だけがバンドルされる。

control-plane の discord_control Lambda も同じ `lambda_package` モジュールを使い、`shared_dir = "${path.module}/../game-stack/functions/_shared"` で game-stack 側の `{aws_clients,ssm_params,ecs_net}.py` を直接参照してバンドルする（`source_pattern = "**/*.py"` で discord_control 配下の全 `.py`（`commands/` 含む）も列挙）。両スタックは別々の Terraform root module（別 state）のため import では共有できないが、ビルド時に同じソースファイルを zip に取り込むことでコピーを持たず単一ソース化している。`aws_clients.py`・`ssm_params.py`・`ecs_net.py` の discord_control 用コピーはもう存在しない。

## デプロイコマンド

```bash
# === control-plane（1 度だけ）===
cd control-plane
cp .env.example .env   # 初回のみ。TF_VAR_discord_public_key・DISCORD_APP_ID 等を記入する（.env は .gitignore 済み）
terraform init -backend-config=backend.hcl
set -a && source .env && set +a
terraform apply
# outputs.interactions_endpoint_url を Discord Developer Portal にコピー

# スラッシュコマンドを登録（apply 後に 1 度だけ。.env の DISCORD_APP_ID/DISCORD_BOT_TOKEN を使う）
bash scripts/register_commands.sh

# === game-stack（ゲームごと）===
cd game-stack
terraform init -backend-config=backend.hcl
terraform workspace new palworld          # または: terraform workspace select palworld
terraform apply -var-file=../games/palworld.tfvars

# ゲームスタックを削除（⚠ 必ず terraform workspace show で対象を確認してから実行）
# workspace はゲームごとに state が分離されており他ゲームには影響しない。
# 共有 VPC（control-plane 管理）は data source 参照のみで game-stack destroy では消えない。
terraform workspace select palworld
terraform destroy -var-file=../games/palworld.tfvars
terraform workspace select default && terraform workspace delete palworld
```

### ゲームの冬眠（EFS 課金ゼロ化） と 復元

#### 冬眠手順（長期間遊ばないゲームの課金をゼロにする）

```bash
# 1. S3 バックアップが最新であることを確認（直前に停止 → モニターが自動同期済みのはず）
aws s3 ls s3://<backup-bucket>/<name-prefix>/ --region ap-northeast-1

# 2. storage.tf の prevent_destroy ブロックを一時的にコメントアウト
#    （EFS の誤削除防止ガードを外す）
# game-stack/storage.tf の lifecycle { prevent_destroy = true } をコメントアウト

# 3. 対象 workspace を確認してから destroy
terraform workspace show  # 必ず確認！
terraform workspace select <game>
terraform destroy -var-file=../games/<game>.tfvars
# → EFS が削除され課金がゼロになる。データは S3 に残存（Glacier IR へ自動降格）。

# 4. storage.tf の prevent_destroy を元に戻す（コミット）
terraform workspace select default && terraform workspace delete <game>
```

#### 復元手順（冬眠からの再開 / EFS 再作成後のリストア）

S3 からの復元は同一手順で、冬眠からの復帰・ストレージクラス変更（one_zone ↔ regional）・障害復旧に使い回せる。

```bash
# 1. terraform apply で空の EFS を再作成
terraform workspace new <game>  # または select
terraform apply -var-file=../games/<game>.tfvars

# 2. 一時的な復元タスクで S3 → 新 EFS に同期
#    既存の backup_efs Lambda の逆処理として手動 aws s3 sync を使う方法:
#    a) ECS Exec や一時タスクで EFS マウント済みのコンテナを起動
#    b) aws s3 sync s3://<backup-bucket>/<prefix>/ <efs-mount-path>/ --region ap-northeast-1
#    例（一時的な復元タスク起動、EFS がマウントされているコンテナ内で実行）:
aws s3 sync s3://palworld-backup-<account_id>/palworld/ /palworld/ --region ap-northeast-1

# 3. 以降は通常どおり Discord /start でゲームサーバーを起動
```

## 手動サーバー操作（Discord が使えない場合）

```bash
# 起動
aws ecs update-service --cluster palworld-cluster \
  --service palworld-service --desired-count 1 --region ap-northeast-1

# 停止
aws ecs update-service --cluster palworld-cluster \
  --service palworld-service --desired-count 0 --region ap-northeast-1

# 状態確認
aws ecs describe-services --cluster palworld-cluster \
  --services palworld-service --region ap-northeast-1 \
  --query "services[0].{Status:status,Running:runningCount,Desired:desiredCount}"

# ゲームサーバー + モニターのログをストリーミング
aws logs tail /ecs/palworld --follow --region ap-northeast-1

# Lambda ログをストリーミング
aws logs tail /aws/lambda/game-server-discord-control --follow --region ap-northeast-1
aws logs tail /aws/lambda/palworld-notify-ip --follow --region ap-northeast-1
aws logs tail /aws/lambda/palworld-auto-update --follow --region ap-northeast-1

# SSM ステータスパラメータを読む
aws ssm get-parameters-by-path --path /ggs/palworld --region ap-northeast-1
```

### コスト通知の疎通テスト

AWS Budgets の実際のしきい値到達を待たずに、SNS → Lambda(`notify_cost`) → Discord/Slack の通知経路を検証できる。

```bash
# 疎通テスト送信（Discord/Slack にテストメッセージが届けば経路は正常）
aws sns publish \
  --topic-arn arn:aws:sns:ap-northeast-1:<account_id>:palworld-cost-alert \
  --subject "コスト通知 疎通テスト" \
  --message "これはテストです。Discord に届けば SNS→Lambda→webhook は正常です。" \
  --region ap-northeast-1

# DLQ 確認（Lambda 障害時のメッセージ蓄積を確認）
aws sqs get-queue-attributes \
  --queue-url $(aws sqs get-queue-url --queue-name palworld-notify-cost-dlq --region ap-northeast-1 --query QueueUrl --output text) \
  --attribute-names ApproximateNumberOfMessages \
  --region ap-northeast-1
```

## セーブデータの切り替え（`save_slot`）

同じ `game_name`（= 同じ ECS クラスター/Lambda/Discord上のゲーム名）のまま、複数のセーブデータを切り替えて使いたい場合は `save_slot` 変数を使う。同時に複数のセーブデータを起動することはできない（1 サービスにつき 1 セーブデータ）。

```bash
# 1. サーバーを完全停止（起動中に切り替えると不整合の恐れがあるため必ず停止する）
#    Discord /stop、または: aws ecs update-service --desired-count 0 ...

# 2. tfvars に save_slot を追加（未設定/空文字列 = 従来どおり game_name 直下のセーブデータ）
#    例: games/palworld.tfvars に save_slot = "world2" を追記

# 3. apply（EFS アクセスポイントが新しいパス用に再作成され、タスク定義も新リビジョンになる。
#    ECS サービスは lifecycle { ignore_changes = [task_definition] } のためすぐには切り替わらない）
terraform apply -var-file=../games/palworld.tfvars

# 4. Discord /start で起動（起動時に最新の ACTIVE タスク定義リビジョンを解決するため、
#    新しいセーブデータのマウントで起動する）
```

`save_slot` は EFS 上のセーブデータ保存先（`/${game_name}/${save_slot}`）と S3 バックアップの保存先プレフィックスのみを切り替える。ECS クラスター名・Lambda 名・SSM 名前空間・Discord 上のゲーム名は変化しないため、コントロールプレーン側の変更は不要。元のセーブデータは EFS 上の別ディレクトリにそのまま残るので、`save_slot` を戻せば復元できる。

## セーブデータの手動バックアップ・復元（`/backup`、`/restore`）

`game-stack/functions/backup_efs/backup_efs.py` の Lambda（`<name_prefix>-backup-efs`）を Discord から直接呼び出せる。通常の自動バックアップ（停止直前・毎日1回）を待たずに、任意のタイミングで EFS⇔S3 間のコピーを行いたい場合に使う。

- **`/backup game:<name>`** — 今すぐ EFS→S3 のバックアップを実行（`action=backup`）。ファイルを読むだけなのでサーバー起動中でも実行可。
- **`/restore game:<name>`** — S3→EFS へ丸ごとミラーリング（`action=restore_all`）。破壊的操作のため **サーバー起動中は拒否**される。実行前に現在の EFS 内容を自動的に S3 の `_pre_restore_snapshot/<exec_id>/` へ退避してから上書きする。S3 に存在しないファイルの削除は行わない（追加・上書きのみの安全側動作）。

どちらも `InvocationType="Event"`（非同期）で呼び出すため、Discord には「開始しました」とすぐ返る。完了・失敗の結果は、backup_efs Lambda が S3 の `<backup_prefix>/_events/` に書いた結果 JSON を S3 イベント通知経由で `notify_backup` Lambda（VPC 外）が拾い、Discord webhook へ通知する（VPC 内の backup_efs からは webhook に直接届かないため。`notify_backup.tf` 参照）。コストは Lambda 実行時間分のみで、無料枠内に収まる想定（実行数秒〜数十秒）。

## Discord だけでのセーブデータ切り替え（`/switch-slot`）

上記の `save_slot` 変数（tfvars 編集 + `terraform apply` が必要）とは別に、**Terraform を一切触らずに Discord だけでセーブデータを切り替える**軽量な方法として `/switch-slot game:<name> slot:<name>` を用意している。

- 対象は Palworld のワールドフォルダ（`Pal/Saved/SaveGames/0/<GUID>/`）のみ。EFS 全体（ゲーム本体のインストールデータ含む）は対象にしない。EFS アクセスポイントや ECS タスク定義も変更しないため、apply は不要。
- 処理内容: ①現在アクティブなスロット名を S3（`<backup_prefix>/slots/_active_slot` オブジェクト）から取得 → ②今のワールドフォルダの中身を S3 の `<backup_prefix>/slots/<現スロット>/` へ保存（保護）→ ③フォルダの中身を削除（フォルダ名・GUID は残すため、サーバー設定の書き換えは不要）→ ④S3 の `slots/<切替先スロット>/` にデータがあれば書き戻す（無ければ空のまま＝次回起動時に新規ワールド）→ ⑤S3 の `_active_slot` を更新。
- 権限は既存の `backup_efs` Lambda が持つ EFS 読み書き・S3 読み書きのみで足りる。ECS タスク定義の登録や `iam:PassRole` のような強い権限は付与していない。アクティブスロットの状態を SSM ではなく S3 で持つのは、backup_efs Lambda が VPC 内（NAT なし・S3 Gateway エンドポイントのみ）で動作し SSM に到達できないため。
- `/restore` と同様、**サーバー起動中は拒否**される（`/stop` してから実行する）。
- 切り替え先スロットが S3 に保存されていない場合は実行前に警告して中断する（スロット名の打ち間違いで意図せず新規ワールドになる事故の防止。存在チェックは backup_efs Lambda の `list_slots` アクションを同期 invoke して行う）。意図的に新規ワールドを作る場合は `new:True` オプションを付けて明示する。

`save_slot`（Terraform）との違い: `save_slot` はどのゲームタイプにも使える汎用的な仕組みで完全なディレクトリ分離ができるが apply が必要。`/switch-slot` はPalworld専用だが Discord だけで完結する。切り替え頻度が高いなら `/switch-slot`、EFSごと完全に分離したい・他ゲームタイプで使いたいなら `save_slot` を使う。

## Fargate Spot 起動モード（`/launch-mode`）

`/launch-mode game:<name> mode:<spot|ondemand>` で次回 `/start` からの起動タイプを切り替えられる（mode 省略で現在値を表示。`/status` にも表示される）。Spot は通常 Fargate の約 7 割引だが、AWS 都合で稀に中断される。

- **仕組み**: 設定は SSM `/ggs/<prefix>/launch_mode` に保持され、`/start` が `update_service(capacityProviderStrategy=..., forceNewDeployment=True)` で反映する。パラメータ未作成なら従来どおり（ondemand 相当）。
- **Terraform は起動モードを管理しない**: `ecs.tf` のサービスは `launch_type = "FARGATE"`（新規作成時の初期値）のまま、`ignore_changes` に `launch_type` と `capacity_provider_strategy` を含めている。⚠ これを「修正」して外すと、/start が strategy を設定した後の plan で launch_type のドリフトが検出され稼働中にサービス再作成が計画される。クラスターには `aws_ecs_cluster_capacity_providers` で FARGATE / FARGATE_SPOT を関連付け済み（default strategy は auto_update の `run_task(launchType="FARGATE")` と競合させないため未設定）。
- **中断時の挙動**: desiredCount=1 のまま ECS が自動で代替タスクを起動する（Spot キャパシティ待ち）。notify_ip が「⚡ Spot 中断・自動再起動中」を通知し、代替タスクの受付開始時に新しい IP が通知される。**IP は変わる**。中断時は停止前の EFS→S3 同期がスキップされるが、セーブデータ本体は EFS にあり無事（毎日の定期バックアップも継続）。Spot 枯渇が長引く場合は `/launch-mode mode:ondemand` → `/stop` → `/start` で通常起動に戻す。
- **向くゲーム / 向かないゲーム**: オートセーブが頻繁な協力サバイバル系（Palworld・Minecraft 等）は中断コスト＝数分のダウン + IP 変更 + 直近オートセーブ以降の巻き戻りで済むため好適。対戦セッション型など途中切断が致命的なゲームには不向き。

## 新しいゲームの追加

1. `games/example.tfvars` を `games/<game>.tfvars` にコピーして必要な値を入力する。
2. ゲームタイプに応じて `monitor_method` を設定する：
   - `"tcp"` — TCP ゲーム（例：Minecraft Java）
   - `"a2s"` — A2S_INFO 対応の Steam ゲーム（汎用 Steam）
   - `"rest"` — Palworld（REST API `/v1/api/players` を使用）
3. コスト最適化オプションを決める（コメントアウト済みの設定、後から変更困難なものもある）：
   - **CPU アーキテクチャ** — ゲームイメージが linux/arm64 を**ネイティブ提供**する場合のみ `task_cpu_architecture = "ARM64"` で約 20% 削減。x86 専用 Steam ゲームには設定しない（box64/FEX エミュレーションは効果相殺・不安定）。確認: `docker manifest inspect <image> | grep -A2 linux/arm64`
   - **EFS ストレージクラス** — 重要度が低いゲームには `efs_storage_class = "one_zone"` で約 45% 削減。**作成後の変更不可**（変更時は destroy → S3 復元が必要）。regional を選ぶと EFS Archive 自動階層化（90 日）も有効。
4. `terraform workspace new <game> && terraform apply -var-file=../games/<game>.tfvars`
5. コントロールプレーンは ECS クラスターの `Game` タグからゲームを自動検出する — コントロールプレーンの変更は不要。

## 主要設計判断

- **ALB/NAT なし:** Fargate に直接パブリック IP を割り当てることで固定コストを月約 $60 削減。セキュリティグループでインバウンドをゲームポートのみに制限。
- **API Gateway v2（Function URL ではなく）:** Lambda Function URL はアカウントレベルのパブリックアクセスブロックの無効化が必要で S3 に影響する。API Gateway v2 HTTP API は同じコストプロファイル（固定費なし）。
- **2 コンテナタスク:** モニターサイドカー（`essential=false`）はゲームコンテナを停止させずに自己終了でき、外部トリガーなしで ECS サービスを停止する。
- **ECS サービスはタスク定義の変更を無視:** `lifecycle { ignore_changes = [task_definition] }` により `terraform apply` でサービスが実行するタスク定義リビジョンは変更されない。代わりに `/start` は実行時に `ecs.describe_task_definition(family)` で最新の ACTIVE リビジョンを解決する。
- **純粋 Python ed25519:** Lambda レイヤーの複雑さを回避。`ed25519.py` は外部ライブラリなしで署名検証を実装。
- **共有 notifier モジュール:** `game-stack/functions/_shared/notifier.py` がメッセージング抽象化。`MESSAGING_PROVIDER=slack` を設定し Slack Incoming Webhook URL を `discord_webhook_url` として提供することで Slack に切り替え可能。スラッシュコマンドレスポンスの場合は `control-plane/functions/discord_control/` の `provider.py` も更新する。
- **コストガードの多層構造:** サイドカーのアイドル検知（ソフト）→ `cost_guard` Lambda のハード停止 → AWS Budgets アラート。3 つの独立した層でコストの暴走を防ぐ。
- **Palworld は ARM64 非対応（X86_64 のまま）:** Palworld 専用サーバーは SteamCMD 配布の x86_64 バイナリのみで ARM64 ネイティブビルドが存在しない。Graviton で動かすには box64/FEX エミュレーションが必要で、オーバーヘッドが約 20% 削減分を相殺し安定性も低下するため採用しない。ARM64 は `docker manifest inspect` でネイティブ arm64 対応が確認できるゲーム（Minecraft Java 等）にのみ設定する。
- **EFS ストレージクラスと階層化:** `efs_storage_class = "regional"`（既定）は複数 AZ 冗長 + 30 日 IA 移行 + 90 日 Archive 移行の 3 段階で自動コスト逓減。`"one_zone"` は単一 AZ で約 45% 安だが Archive 非対応で IA 止まり、かつ作成後変更不可。長期間プレイしないゲームは terraform destroy で EFS 課金をゼロにできる（S3 バックアップから復元可能）。
- **`/update` は update_service ではなく run_task を使用:** auto_update Lambda は `ecs.run_task` で `UPDATE_ON_BOOT=true` を設定した単発タスクを実行する。このタスクのモニターサイドカーは EventBridge 非対象の SSM パラメータ（`update_ready`）とダミーサービス名にリダイレクトされ、余分な IP 通知や誤ったサービス停止を防ぐ。

## 状態管理

両スタックとも S3 リモートバックエンドを使用。アカウント固有の設定は `backend.hcl` ファイルに記載（コミットしない）。`games/*.tfvars` ファイル（`example.tfvars` を除く）は `.gitignore` 済み — Webhook URL を含むアカウント固有の値が含まれるため。
