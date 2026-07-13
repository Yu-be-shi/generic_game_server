"""switch_slot.py - /switch-slot game:<name> slot:<name>: セーブデータのスロットを切り替える"""
import json
import logging

import ecs_helpers
from clients import lambda_client
from commands.guards import guarded_worker_invoke, require_service
from constants import TAG_BACKUP_FUNCTION, TAG_STATUS_PARAM_PREFIX, WORKER_INVOKE_FAILURE_FOOTER

logger = logging.getLogger()


def cmd_switch_slot(game_name: str, slot: str, create_new: bool = False) -> str:
    """
    /switch-slot game:<name> slot:<name> [new:<bool>]: セーブデータのスロットを S3 経由で切り替える。

    /restore（EFS 全体を対象にした汎用ミラーリング）とは異なり、Palworld のワールド
    フォルダ（SaveGames/0/<GUID>/）だけを対象にする軽量な切り替え。EFS アクセスポイント・
    ECS タスク定義は一切変更しないため terraform apply は不要（Discord だけで完結する）。

    危険な操作のため:
      - サーバー起動中は拒否する（ゲームプロセスがセーブデータを使用中の書き込み競合を防ぐ）
      - メンテナンス中（/update 実行中）も拒否する（EFS への同時書き込み競合を防ぐ）
      - 切り替え前に現在のスロットの内容を自動的に S3 の slots/<現スロット>/ へ保存する
      - 切り替え先スロットが S3 に存在しない場合は実行せず警告する（スロット名の
        打ち間違いで意図せず新規ワールドになる事故の防止）。意図的に新規ワールドを
        作る場合は new:True を付けて明示する
    """
    # 同一スロットへの切り替えは「現ワールドを保存して同じものを書き戻す」だけの
    # 無意味な往復になる（S3 側のスロットデータを手動更新していた場合は上書きで失う）
    # ため、実行前に拒否する。active_slot 未記録（一度も切り替えていない）場合は判定しない。
    cluster_arn, _, err = require_service(game_name)
    if err:
        return err
    ssm_prefix = ecs_helpers.get_cluster_tag(cluster_arn, TAG_STATUS_PARAM_PREFIX)
    active_slot = ecs_helpers.get_active_slot(ssm_prefix) if ssm_prefix else ""
    if active_slot and active_slot == slot:
        return (
            f"ℹ️ `{slot}` はすでに使用中のスロットです。切り替えの必要はありません。\n"
            f"現在のスロットは `/status game:{game_name}` で確認できます。"
        )

    if not create_new:
        err = _reject_if_slot_missing(cluster_arn, slot, active_slot)
        if err:
            return err

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


def _reject_if_slot_missing(cluster_arn: str, slot: str, active_slot: str):
    """
    切り替え先スロットが S3 に保存されていない場合、警告メッセージを返す（issue #3）。
    存在する場合と、存在確認自体に失敗した場合は None を返す（後者は fail-open:
    存在チェックは打ち間違い事故防止の補助であり、確認経路の障害でスロット切り替え
    そのものを止めない。切り替えは旧ワールドが slots/ に保護されるため復元可能）。
    """
    worker_function = ecs_helpers.get_cluster_tag(cluster_arn, TAG_BACKUP_FUNCTION)
    if not worker_function:
        return None
    existing = _list_existing_slots(worker_function)
    if existing is None or slot in existing:
        return None

    # 現アクティブスロットは S3 未保存（一度も切り替えていない）でも実在するため一覧に含める
    known = sorted(set(existing) | ({active_slot} if active_slot else set()))
    known_text = "、".join(f"`{s}`" for s in known) if known else "（なし）"
    return (
        f"⚠️ スロット `{slot}` は保存されていません。このまま切り替えると**新規ワールド**として起動します。\n"
        f"既存のスロット: {known_text}\n"
        f"打ち間違いの場合は正しいスロット名で再実行してください。\n"
        f"新規ワールドを作る場合は `new:True` を付けて再実行してください。"
    )


def _list_existing_slots(worker_function: str):
    """
    backup_efs Lambda の list_slots アクションを同期呼び出しし、S3 に保存済みの
    スロット名一覧（list）を返す。取得に失敗した場合は None を返す。
    """
    try:
        resp = lambda_client.invoke(
            FunctionName=worker_function,
            InvocationType="RequestResponse",
            Payload=json.dumps({"action": "list_slots"}).encode("utf-8"),
        )
        if resp.get("FunctionError"):
            logger.warning("list_slots がエラーを返しました: %s", resp["FunctionError"])
            return None
        payload = json.loads(resp["Payload"].read())
        slots = payload.get("slots")
        return slots if isinstance(slots, list) else None
    except Exception:
        logger.exception("list_slots の呼び出しに失敗（スロット存在チェックをスキップ）")
        return None
