"""update.py - /update game:<name>: サーバー本体を停止したままアップデートする"""
from commands.guards import guarded_worker_invoke
from constants import TAG_AUTO_UPDATE_FUNCTION, WORKER_INVOKE_FAILURE_FOOTER


def cmd_update(game_name: str) -> str:
    """
    /update game:<name>: サーバーアップデートを実行する。

    同一タスク定義を UPDATE_ON_BOOT=true でワンオフ起動し、SteamCMD アップデートを
    実行する Worker Lambda を非同期 invoke する。通常の /start（UPDATE_ON_BOOT=false）
    の高速起動には一切影響しない。

    処理フロー:
      1. ゲームが存在するか確認
      2. サーバーが起動中なら拒否（EFS への二重書き込み防止）
      3. メンテナンス中なら拒否（二重実行防止）
      4. AutoUpdateFunction タグから Worker Lambda 名を取得
      5. Worker を非同期 invoke（Discord 3秒制限内で即応答するため Event モード）
      6. 「🔄 開始しました」を返す（完了通知は Worker から Webhook で届く）
    """
    return guarded_worker_invoke(
        game_name,
        tag_key=TAG_AUTO_UPDATE_FUNCTION,
        action_verb="アップデート",
        payload={"game_name": game_name},
        log_message=f"auto_update Worker を非同期 invoke: function={{worker_function}} game={game_name}",
        error_return=(
            f"❌ **{game_name}** のアップデート Worker の起動に失敗しました。\n"
            + WORKER_INVOKE_FAILURE_FOOTER
        ),
        error_log_message="Worker Lambda の invoke に失敗: {worker_function}",
        success_message=(
            f"🔄 **{game_name}** のアップデートを開始しました。\n"
            "SteamCMD でサーバーを更新中です。完了したら通知します（数分かかります）。\n"
            "アップデート中は `/start` できません。"
        ),
    )
