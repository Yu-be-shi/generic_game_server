"""restore.py - /restore game:<name>: S3 の最新バックアップを EFS へミラーリングする"""
from commands.guards import guarded_worker_invoke


def cmd_restore(game_name: str) -> str:
    """
    /restore game:<name>: S3 上の最新バックアップを EFS へミラーリングする（S3→EFS）。

    危険な操作のため:
      - サーバー起動中は拒否する（ゲームプロセスが EFS を使用中の書き込み競合を防ぐ）
      - メンテナンス中（/update 実行中）も拒否する（EFS への同時書き込み競合を防ぐ）
      - 実行前に現在の EFS 内容を丸ごと S3 の _pre_restore_snapshot/ へ退避してから上書きする
      - S3 に存在しないファイルの削除は行わない（追加・上書きのみの安全側動作）
    """
    return guarded_worker_invoke(
        game_name,
        tag_key="BackupFunction",
        action_verb="実行",
        payload={"action": "restore_all"},
        log_message=lambda worker_function: (
            f"backup_efs(restore_all) を非同期 invoke: function={worker_function} game={game_name}"
        ),
        error_return=(
            f"❌ **{game_name}** の復元実行に失敗しました。\n"
            "IAM 権限または Lambda 設定を確認してください。"
        ),
        error_log_message=lambda worker_function: f"Restore Lambda の invoke に失敗: {worker_function}",
        success_message=lambda worker_function: (
            f"♻️ **{game_name}** の復元（S3→EFS）を開始しました。\n"
            "実行前の内容は自動的に S3 の `_pre_restore_snapshot/` へ退避済みです。\n"
            "完了通知は届きません。結果を確認したい場合は "
            f"CloudWatch Logs `/aws/lambda/{worker_function}` を参照してください。"
        ),
    )
