#!/bin/bash
# =============================================================================
# register_commands.sh - Discord スラッシュコマンドを登録するヘルパー
# =============================================================================
# 使い方:
#   export DISCORD_APP_ID="あなたのアプリID"
#   export DISCORD_BOT_TOKEN="あなたのBotトークン"
#   bash scripts/register_commands.sh
#
# 取得場所（Discord Developer Portal）:
#   App ID    → General Information → APPLICATION ID
#   Bot Token → Bot → Reset Token で取得
#
# グローバルコマンドとして登録するため、適用まで最大1時間かかる場合がある。
# 開発中は Guild（サーバー）コマンドの方が即時反映されるが、
# 個人利用ならグローバルで問題ない。
# =============================================================================

set -euo pipefail

: "${DISCORD_APP_ID:?DISCORD_APP_ID が設定されていません}"
: "${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN が設定されていません}"

DISCORD_API="https://discord.com/api/v10"
ENDPOINT="${DISCORD_API}/applications/${DISCORD_APP_ID}/commands"

echo "============================================="
echo "Discord スラッシュコマンド登録"
echo "App ID: ${DISCORD_APP_ID}"
echo "============================================="

# コマンド定義（9つ: /games /start /stop /status /cost /update /backup /restore /switch-slot）
# game オプション（type=3, required, autocomplete）は6コマンドで完全に同一の形なので、
# python3 で生成する（説明文だけが違う定型ブロックを手で9回コピペしない）。
COMMANDS=$(python3 << 'PYEOF'
import json


def game_option(description):
    """全コマンド共通の game オプション（説明文のみ差し替え）"""
    return {
        "name": "game",
        "description": description,
        "type": 3,
        "required": True,
        "autocomplete": True,
    }


commands = [
    {"name": "games", "description": "ゲームサーバーの一覧と稼働状態を表示します", "type": 1},
    {
        "name": "start",
        "description": "ゲームサーバーを起動します",
        "type": 1,
        "options": [game_option("起動するゲーム名")],
    },
    {
        "name": "stop",
        "description": "ゲームサーバーを停止します",
        "type": 1,
        "options": [game_option("停止するゲーム名")],
    },
    {
        "name": "status",
        "description": "ゲームサーバーの稼働状態と IP アドレスを表示します",
        "type": 1,
        "options": [game_option("確認するゲーム名")],
    },
    {"name": "cost", "description": "今月の AWS コスト・予算残・月末着地予測を表示します", "type": 1},
    {
        "name": "update",
        "description": "サーバーを停止したままサーバー本体をアップデートします（UPDATE_ON_BOOT）",
        "type": 1,
        "options": [game_option("アップデートするゲーム名")],
    },
    {
        "name": "backup",
        "description": "今すぐセーブデータを EFS から S3 へバックアップします",
        "type": 1,
        "options": [game_option("バックアップするゲーム名")],
    },
    {
        "name": "restore",
        "description": "S3 の最新バックアップをセーブデータへ復元します（要停止）",
        "type": 1,
        "options": [game_option("復元するゲーム名")],
    },
    {
        "name": "switch-slot",
        "description": "セーブデータのスロットを S3 経由で切り替えます（要停止）",
        "type": 1,
        "options": [
            game_option("対象のゲーム名"),
            {
                "name": "slot",
                "description": "切り替え先のスロット名（英数字・ハイフン・アンダースコア）",
                "type": 3,
                "required": True,
            },
            {
                "name": "new",
                "description": "True で新規ワールドの作成を明示します（未保存のスロット名は既定で警告・中断）",
                "type": 5,
                "required": False,
            },
        ],
    },
]

print(json.dumps(commands, ensure_ascii=False))
PYEOF
)

# Discord API にコマンドを一括登録（PUT = 全置換）
# --max-time 30: タイムアウトを設定（一時障害でハングしないよう）
# head -n-1 は GNU coreutils 固有のため、sed '$d'（POSIX 互換）で最終行を除去する
RESPONSE=$(curl -s --max-time 30 -w "\n%{http_code}" \
  -X PUT "${ENDPOINT}" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${COMMANDS}")

HTTP_CODE=$(echo "${RESPONSE}" | tail -n1)
BODY=$(echo "${RESPONSE}" | sed '$d')

if [ "${HTTP_CODE}" = "200" ] || [ "${HTTP_CODE}" = "201" ]; then
  echo "✅ 登録成功（HTTP ${HTTP_CODE}）"
  echo ""
  echo "登録されたコマンド:"
  BODY="${BODY}" python3 << 'PYEOF' 2>/dev/null || echo "${BODY}"
import json, os
cmds = json.loads(os.environ["BODY"])
for c in cmds:
    opts = c.get('options', [])
    opt_str = ' '.join(f'<{o["name"]}>' for o in opts) if opts else ''
    print(f'  /{c["name"]} {opt_str} - {c["description"]}')
PYEOF
  echo ""
  echo "グローバルコマンドは最大1時間で反映されます。"
  echo "すぐ確認したい場合は Discord を再起動してください。"
else
  echo "❌ 登録失敗（HTTP ${HTTP_CODE}）"
  echo "${BODY}"
  exit 1
fi
