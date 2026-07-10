"""
commands - Discord スラッシュコマンドのディスパッチとオートコンプリート

各コマンドの実装は同パッケージ内のファイル（games.py, start.py, ...）に分割されている。
共通のガード処理・ヘルパーは guards.py に集約。
"""
import ecs_helpers

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
    return (starts + contains)[:25]  # Discord 上限 25 件


def dispatch_command(command: str, options: dict) -> str:
    """コマンド名でハンドラに振り分け、メッセージ本文（str）を返す"""
    game_name = options.get("game", "").strip()

    if command == "games":
        return cmd_games()
    elif command == "start":
        return cmd_start(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "stop":
        return cmd_stop(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "status":
        return cmd_status(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "cost":
        return cmd_cost()
    elif command == "update":
        return cmd_update(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "backup":
        return cmd_backup(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "restore":
        return cmd_restore(game_name) if game_name else "ゲーム名を指定してください。"
    elif command == "switch-slot":
        slot = options.get("slot", "").strip()
        if not game_name:
            return "ゲーム名を指定してください。"
        if not slot:
            return "スロット名を指定してください。"
        return cmd_switch_slot(game_name, slot)
    else:
        return f"不明なコマンド: `/{command}`"
