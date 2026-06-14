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

# コマンド定義（4つ: /games /start /stop /status）
read -r -d '' COMMANDS << 'EOF'
[
  {
    "name": "games",
    "description": "ゲームサーバーの一覧と稼働状態を表示します",
    "type": 1
  },
  {
    "name": "start",
    "description": "ゲームサーバーを起動します",
    "type": 1,
    "options": [
      {
        "name": "game",
        "description": "起動するゲーム名（例: palworld, minecraft）",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "stop",
    "description": "ゲームサーバーを停止します",
    "type": 1,
    "options": [
      {
        "name": "game",
        "description": "停止するゲーム名",
        "type": 3,
        "required": true
      }
    ]
  },
  {
    "name": "status",
    "description": "ゲームサーバーの稼働状態と IP アドレスを表示します",
    "type": 1,
    "options": [
      {
        "name": "game",
        "description": "確認するゲーム名",
        "type": 3,
        "required": true
      }
    ]
  }
]
EOF

# Discord API にコマンドを一括登録（PUT = 全置換）
RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X PUT "${ENDPOINT}" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${COMMANDS}")

HTTP_CODE=$(echo "${RESPONSE}" | tail -n1)
BODY=$(echo "${RESPONSE}" | head -n-1)

if [ "${HTTP_CODE}" = "200" ] || [ "${HTTP_CODE}" = "201" ]; then
  echo "✅ 登録成功（HTTP ${HTTP_CODE}）"
  echo ""
  echo "登録されたコマンド:"
  echo "${BODY}" | python3 -c "
import json, sys
cmds = json.load(sys.stdin)
for c in cmds:
    opts = c.get('options', [])
    opt_str = ' '.join(f'<{o[\"name\"]}>' for o in opts) if opts else ''
    print(f'  /{c[\"name\"]} {opt_str} - {c[\"description\"]}')
" 2>/dev/null || echo "${BODY}"
  echo ""
  echo "グローバルコマンドは最大1時間で反映されます。"
  echo "すぐ確認したい場合は Discord を再起動してください。"
else
  echo "❌ 登録失敗（HTTP ${HTTP_CODE}）"
  echo "${BODY}"
  exit 1
fi
