"""backup.py - /backup game:<name>: 今すぐ EFS→S3 のバックアップを実行する"""
from commands.guards import guarded_worker_invoke


def cmd_backup(game_name: str) -> str:
    """
    /backup game:<name>: 今すぐ EFS → S3 のバックアップを実行する。

    通常は「サーバー停止直前」と「毎日1回の定期実行」で自動的に行われるが、
    それを待たずに任意のタイミングで手動実行したい場合に使う。
    ファイルを読むだけの処理のため、サーバー起動中でも実行可能（定期バックアップと同じ扱い）。
    """
    return guarded_worker_invoke(
        game_name,
        tag_key="BackupFunction",
        require_stopped=False,
        payload={"action": "backup"},
        log_message=lambda worker_function: (
            f"backup_efs(backup) を非同期 invoke: function={worker_function} game={game_name}"
        ),
        error_return=(
            f"❌ **{game_name}** のバックアップ実行に失敗しました。\n"
            "IAM 権限または Lambda 設定を確認してください。"
        ),
        error_log_message=lambda worker_function: f"Backup Lambda の invoke に失敗: {worker_function}",
        success_message=lambda worker_function: (
            f"💾 **{game_name}** のバックアップ（EFS→S3）を開始しました。\n"
            "完了通知は届きません（数秒〜数十秒で完了します）。結果を確認したい場合は "
            f"CloudWatch Logs `/aws/lambda/{worker_function}` を参照してください。"
        ),
    )
