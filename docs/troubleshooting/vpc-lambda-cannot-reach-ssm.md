# /switch-slot が無反応のまま失敗する（VPC 内 Lambda から SSM に到達できない）

## 事象

Discord `/switch-slot` を実行すると「切り替え中です」と表示されるが、いつまで待っても切り替わらない。CloudWatch Logs（`/aws/lambda/<name_prefix>-backup-efs`）には約 87 秒の実行の末に以下が記録される:

```
[ERROR] ConnectTimeoutError: Connect timeout on endpoint URL: "https://ssm.ap-northeast-1.amazonaws.com/"
```

非同期呼び出しのため Lambda が自動で 2 回リトライし（計 3 回失敗）、ユーザーには何も通知されない。2026-07-12 に初回実行で発覚。

## 原因

`backup_efs` Lambda は EFS をマウントするため VPC アタッチ型だが、共有 VPC には **S3 の Gateway エンドポイントしかなく、NAT ゲートウェイも他のインターフェースエンドポイントもない**（固定コストゼロ方針のため）。

VPC アタッチ型 Lambda の ENI にはパブリック IP が付与されないので、パブリックサブネットでも**インターネット・S3 以外の AWS API には一切到達できない**。`switch_slot` はアクティブスロット名を SSM で管理する設計だったため、最初の SSM 呼び出し（boto3 の接続リトライで約 87 秒）でタイムアウトして異常終了していた。

`/backup` や restore 系アクションは S3 と EFS しか使わないため、この制約が露見しなかった（設計時から存在した潜在バグ）。

## 対応

アクティブスロットの状態管理を SSM パラメータから **S3 オブジェクト**（`<backup_prefix>/slots/_active_slot`）へ移行した。S3 は Gateway エンドポイント経由で到達できるため VPC 内 Lambda から問題なく読み書きできる。あわせて不要になった SSM の IAM 権限（`ssm:GetParameter`/`ssm:PutParameter`）を backup Lambda ロールから削除した。

失敗した実行は SSM 読み取り（処理の最初のステップ）で死んでいるため、**EFS のワールドデータには一切影響がない**。

## 再発防止

- `backup_efs` Lambda（および今後 VPC に入れる Lambda）に新機能を足すときは、**S3 と EFS 以外の外部通信（SSM・Discord webhook 等）を行わない**こと。状態は S3 に、通知は VPC 外の Lambda（notify_ip / notify_cost 等）に委譲する。
- 完了・失敗の Discord 通知が無いことが発覚を遅らせた。通知機能の追加は Issue #3 で管理する。
