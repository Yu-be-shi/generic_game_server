"""
commands - Discord スラッシュコマンドのディスパッチとオートコンプリート

各コマンドの実装は同パッケージ内のファイル（games.py, start.py, ...）に分割されている。
共通のガード処理・ヘルパーは guards.py に集約。
"""
import ecs_helpers
from constants import GAME_NAME_REQUIRED, SLOT_NAME_REQUIRED

from commands.backup import cmd_backup
from commands.cost import cmd_cost
from commands.games import cmd_games
from commands.restore import cmd_restore
from commands.start import cmd_start
from commands.status import cmd_status
from commands.stop import cmd_stop
from commands.switch_slot import cmd_switch_slot
from commands.update import cmd_update


def autocomplete_choices(focused: str) -> list:
    """
    オートコンプリート候補を返す。
    ECS の Game タグからゲーム名を取得し、入力値で部分一致フィルタする。
    先頭一致を優先してソート（先頭一致 → それ以外の含む → アルファベット順）。
    """
    game_names   = ecs_helpers.list_game_names()
    partial_lower = focused.lower()
    starts   = [g for g in game_names if g.lower().startswith(partial_lower)]
    contains = [g for g in game_names if partial_lower in g.lower() and not g.lower().startswith(partial_lower)]
    # provider.autocomplete() 側で AUTOCOMPLETE_LIMIT 件に切り詰めるため、ここでは切り詰めない
    return starts + contains


# game 引数を取らないコマンド（引数なしでそのまま呼び出す）
_NO_GAME_HANDLERS = {
    "games": cmd_games,
    "cost":  cmd_cost,
}

# game 引数を必須とするコマンド（未指定なら共通メッセージで弾く）
_GAME_HANDLERS = {
    "start":   cmd_start,
    "stop":    cmd_stop,
    "status":  cmd_status,
    "update":  cmd_update,
    "backup":  cmd_backup,
    "restore": cmd_restore,
}


def dispatch_command(command: str, options: dict) -> str:
    """コマンド名でハンドラに振り分け、メッセージ本文（str）を返す"""
    game_name = options.get("game", "").strip()

    if command in _NO_GAME_HANDLERS:
        return _NO_GAME_HANDLERS[command]()

    if command in _GAME_HANDLERS:
        return _GAME_HANDLERS[command](game_name) if game_name else GAME_NAME_REQUIRED

    if command == "switch-slot":
        # slot 引数も必須のため専用ハンドリング（テーブルには乗せない）
        slot = options.get("slot", "").strip()
        if not game_name:
            return GAME_NAME_REQUIRED
        if not slot:
            return SLOT_NAME_REQUIRED
        return cmd_switch_slot(game_name, slot)

    return f"不明なコマンド: `/{command}`"
