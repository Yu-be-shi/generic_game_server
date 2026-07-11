"""switch_slot.py - /switch-slot game:<name> slot:<name>: セーブデータのスロットを切り替える"""
from commands.guards import guarded_worker_invoke
from constants import CLOUDWATCH_LOGS_REFERENCE, TAG_BACKUP_FUNCTION, WORKER_INVOKE_FAILURE_FOOTER


def cmd_switch_slot(game_name: str, slot: str) -> str:
    """
    /switch-slot game:<name> slot:<name>: セーブデータのスロットを S3 経由で切り替える。

    /restore（EFS 全体を対象にした汎用ミラーリング）とは異なり、Palworld のワールド
    フォルダ（SaveGames/0/<GUID>/）だけを対象にする軽量な切り替え。EFS アクセスポイント・
    ECS タスク定義は一切変更しないため terraform apply は不要（Discord だけで完結する）。

    危険な操作のため:
      - サーバー起動中は拒否する（ゲームプロセスがセーブデータを使用中の書き込み競合を防ぐ）
      - メンテナンス中（/update 実行中）も拒否する（EFS への同時書き込み競合を防ぐ）
      - 切り替え前に現在のスロットの内容を自動的に S3 の slots/<現スロット>/ へ保存する
      - 切り替え先スロットが未使用の場合は空のワールドとして起動する（新規ワールド扱い）
    """
    return guarded_worker_invoke(
        game_name,
        tag_key=TAG_BACKUP_FUNCTION,
        action_verb="実行",
        payload={"action": "switch_slot", "slot": slot},
        log_message=lambda worker_function: (
            f"backup_efs(switch_slot) を非同期 invoke: "
            f"function={worker_function} game={game_name} slot={slot}"
        ),
        error_return=(
            f"❌ **{game_name}** のスロット切り替えに失敗しました。\n"
            + WORKER_INVOKE_FAILURE_FOOTER
        ),
        error_log_message=lambda worker_function: f"Switch slot Lambda の invoke に失敗: {worker_function}",
        success_message=lambda worker_function: (
            f"🔀 **{game_name}** のセーブデータを `{slot}` へ切り替え中です。\n"
            "切り替え前の内容は自動的に S3 の `slots/` 配下へ保存されています。\n"
            "完了通知は届きません。"
            + CLOUDWATCH_LOGS_REFERENCE.format(worker_function=worker_function)
        ),
    )
