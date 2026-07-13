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
SSM_SUFFIX_ACTIVE_SLOT = "/active_slot"
SSM_SUFFIX_LAUNCH_MODE = "/launch_mode"

# --- 起動モード（/launch-mode コマンド・/start の capacityProviderStrategy 分岐）---
LAUNCH_MODE_SPOT = "spot"
LAUNCH_MODE_ONDEMAND = "ondemand"

# --- コマンド単位の権限制御（index.py の許可リストチェックで参照）---
# 破壊的・コスト影響のあるコマンドのみ ALLOWED_USER_IDS による制限を適用する。
# ここに無いコマンド（/games /status /cost = 閲覧系）は誰でも実行できる。
# 新しい破壊的コマンドを追加したら必ずこのセットにも追加すること。
RESTRICTED_COMMANDS = {
    "start",
    "stop",
    "update",
    "backup",
    "restore",
    "switch-slot",
    "launch-mode",
}

# --- 応答の可視性（index.py の deferred 応答で参照）---
# ここに含まれるコマンドの結果は実行者のみに表示（ephemeral）。それ以外は
# チャンネル全員に表示される（/start の起動報告などをメンバー全員が見られるように）。
# 権限拒否などコマンド実行前のエラー応答は常に実行者のみ表示。
EPHEMERAL_COMMANDS = {
    "cost",  # 金額・予算情報はチャンネルに常時公開しない
}

# --- worker 系コマンド（/update・/backup・/restore・/switch-slot）の定型メッセージ断片 ---
GAME_NAME_REQUIRED = "ゲーム名を指定してください。"
SLOT_NAME_REQUIRED = "スロット名を指定してください。"
WORKER_INVOKE_FAILURE_FOOTER = "IAM 権限または Lambda 設定を確認してください。"

# --- Discord 仕様上の上限（provider.py と commands/__init__.py の双方で参照）---
AUTOCOMPLETE_LIMIT = 25  # オートコンプリート候補数の上限
