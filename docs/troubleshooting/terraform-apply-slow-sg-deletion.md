# terraform apply が SG 削除で 15〜20 分ブロックされる

## 事象

セキュリティグループ（SG）の置き換え・削除を含む `terraform apply` / `terraform destroy` が、SG の削除ステップで 15〜20 分程度進まなくなる。Terraform は `DependencyViolation` を受けて削除をリトライし続け、その間出力が止まったように見える。

2026-07-12 の `name_prefix` リネーム apply（全リソース置き換え）で実測・原因特定した。SG を触らない日常の apply（Lambda コード変更・tfvars 変更等）では発生しない。

## 原因

`backup_efs` Lambda は EFS にアクセスするため VPC アタッチ型であり、実行時に AWS Lambda サービスが VPC 内へ ENI（`AWS Lambda VPC ENI-<function名>`）を作成する。この ENI は **Lambda 関数を削除しても即座には解放されず、AWS 側の非同期処理で最大 20 分程度「in-use」のまま残る**。

ENI が SG にアタッチされている間はその SG を削除できないため、Terraform が SG 削除をリトライし続けてブロックする。Terraform やこのプロジェクト構成のバグではなく、AWS Lambda の仕様（Hyperplane ENI のガベージコレクション遅延）。

確認コマンド:

```bash
aws ec2 describe-network-interfaces --region ap-northeast-1 \
  --filters "Name=group-name,Values=<削除待ちのSG名>" \
  --query "NetworkInterfaces[].{Status:Status,Desc:Description}"
# Description が "AWS Lambda VPC ENI-..." で Status が in-use なら本事象
```

## 対応

- **待つのが正解**。ENI は AWS 側で自動解放され、その後 SG 削除が成功して apply は正常完了する（実測: 約 17 分）。手動での ENI デタッチ/削除は Lambda 管理の ENI に対しては基本的にできない。
- apply が SG 削除のタイムアウトで**失敗しても破壊的ではない**。ENI 解放後（20 分程度待ってから）同じ apply を再実行すれば残りが収束する。
- 長時間 apply はバックグラウンド実行にし、SG に依存しない作業（S3 データ移行・ドキュメント更新等）を並行して進めると待ち時間を有効に使える。

## 再発防止

- VPC アタッチ型 Lambda（backup_efs）の SG 置き換え・削除を伴う変更（リネーム、destroy、SG 定義変更）を計画する際は、**apply に +15〜20 分かかる前提**でスケジュールする。
- そもそも SG の置き換えが必要な変更かを plan の `# forces replacement` で事前確認する。SG を触らない設計にできるならそちらを優先する。
- 関連 Issue: [#1](https://github.com/Yu-be-shi/generic_game_server/issues/1)
