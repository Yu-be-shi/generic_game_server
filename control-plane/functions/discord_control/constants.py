"""
constants.py - discord_control 全体で共有する定数

CLAUDE.md の「ECS クラスタータグ」「SSM パラメータ名前空間」表に対応する値、
および複数ファイルで重複していた定型メッセージ断片・数値をここに集約する。
値そのものは変更しない（意味を変えず名前を与えるだけ）。
"""

# --- ECS クラスタータグ（CLAUDE.md「ECS クラスタータグ」表を参照）---
TAG_GAME = "Game"
TAG_STATUS_PARAM_PREFIX = "StatusParamPrefix"
TAG_AUTO_UPDATE_FUNCTION = "AutoUpdateFunction"
TAG_BACKUP_FUNCTION = "BackupFunction"

# --- SSM パラメータ接尾辞（/ggs/<prefix> 以下、CLAUDE.md「SSM パラメータ名前空間」表を参照）---
SSM_SUFFIX_READY = "/ready"
SSM_SUFFIX_PLAYERS = "/players"
SSM_SUFFIX_MAINTENANCE = "/maintenance"
SSM_SUFFIX_NOTIFIED_TASK = "/notified_task"

# --- worker 系コマンド（/update・/backup・/restore・/switch-slot）の定型メッセージ断片 ---
GAME_NAME_REQUIRED = "ゲーム名を指定してください。"
SLOT_NAME_REQUIRED = "スロット名を指定してください。"
WORKER_INVOKE_FAILURE_FOOTER = "IAM 権限または Lambda 設定を確認してください。"

# --- Discord 仕様上の上限（provider.py と commands/__init__.py の双方で参照）---
AUTOCOMPLETE_LIMIT = 25  # オートコンプリート候補数の上限
