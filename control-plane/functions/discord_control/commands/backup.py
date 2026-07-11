"""backup.py - /backup game:<name>: 今すぐ EFS→S3 のバックアップを実行する"""
from commands.guards import guarded_worker_invoke
from constants import CLOUDWATCH_LOGS_REFERENCE, TAG_BACKUP_FUNCTION, WORKER_INVOKE_FAILURE_FOOTER


def cmd_backup(game_name: str) -> str:
    """
    /backup game:<name>: 今すぐ EFS → S3 のバックアップを実行する。

    通常は「サーバー停止直前」と「毎日1回の定期実行」で自動的に行われるが、
    それを待たずに任意のタイミングで手動実行したい場合に使う。
    ファイルを読むだけの処理のため、サーバー起動中でも実行可能（定期バックアップと同じ扱い）。
    """
    return guarded_worker_invoke(
        game_name,
        tag_key=TAG_BACKUP_FUNCTION,
        require_stopped=False,
        payload={"action": "backup"},
        log_message=f"backup_efs(backup) を非同期 invoke: function={{worker_function}} game={game_name}",
        error_return=(
            f"❌ **{game_name}** のバックアップ実行に失敗しました。\n"
            + WORKER_INVOKE_FAILURE_FOOTER
        ),
        error_log_message="Backup Lambda の invoke に失敗: {worker_function}",
        success_message=(
            f"💾 **{game_name}** のバックアップ（EFS→S3）を開始しました。\n"
            "完了通知は届きません（数秒〜数十秒で完了します）。"
            + CLOUDWATCH_LOGS_REFERENCE
        ),
    )
