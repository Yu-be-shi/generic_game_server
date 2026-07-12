"""switch_slot.py - /switch-slot game:<name> slot:<name>: セーブデータのスロットを切り替える"""
import ecs_helpers
from commands.guards import guarded_worker_invoke, require_service
from constants import TAG_BACKUP_FUNCTION, TAG_STATUS_PARAM_PREFIX, WORKER_INVOKE_FAILURE_FOOTER


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
    # 同一スロットへの切り替えは「現ワールドを保存して同じものを書き戻す」だけの
    # 無意味な往復になる（S3 側のスロットデータを手動更新していた場合は上書きで失う）
    # ため、実行前に拒否する。active_slot 未記録（一度も切り替えていない）場合は判定しない。
    cluster_arn, _, err = require_service(game_name)
    if err:
        return err
    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, TAG_STATUS_PARAM_PREFIX)
    if ssm_prefix and ecs_helpers.get_active_slot(ssm_prefix) == slot:
        return (
            f"ℹ️ `{slot}` はすでに使用中のスロットです。切り替えの必要はありません。\n"
            f"現在のスロットは `/status game:{game_name}` で確認できます。"
        )

    return guarded_worker_invoke(
        game_name,
        tag_key=TAG_BACKUP_FUNCTION,
        action_verb="実行",
        payload={"action": "switch_slot", "slot": slot},
        log_message=(
            f"backup_efs(switch_slot) を非同期 invoke: "
            f"function={{worker_function}} game={game_name} slot={slot}"
        ),
        error_return=(
            f"❌ **{game_name}** のスロット切り替えに失敗しました。\n"
            + WORKER_INVOKE_FAILURE_FOOTER
        ),
        error_log_message="Switch slot Lambda の invoke に失敗: {worker_function}",
        success_message=(
            f"🔀 **{game_name}** のセーブデータを `{slot}` へ切り替え中です。\n"
            "切り替え前の内容は自動的に S3 の `slots/` 配下へ保存されています。\n"
            "完了または失敗すると通知が届きます（通常数秒〜数十秒）。"
        ),
    )
