#!/bin/sh
# =============================================================================
# auto_shutdown.sh - 無人検知・自動シャットダウン サイドカースクリプト
# =============================================================================
#
# 【動作の仕組み】
#   このスクリプトは ECS タスク内の「monitor」コンテナで実行される。
#   Fargate の awsvpc ネットワークモードにより、ゲームコンテナと
#   ネットワーク名前空間を共有しているため、ゲームポートへの
#   接続数を監視できる。
#
#   起動シーケンスは2段階：
#
#   [フェーズA: 受付待ち]
#     READY_POLL_INTERVAL（デフォルト10秒）間隔で readiness をチェックする。
#     _ready=1 を検知したら即座に SSM ready=1 を書き込み（Discord IP 通知が飛ぶ）。
#     STARTUP_GRACE_MINUTES（デフォルト30分）以内に受付開始しなければコスト保護
#     のため自動停止する。
#
#   [フェーズB: アイドル監視]
#     CHECK_INTERVAL（デフォルト60秒）間隔で接続数を監視する。
#     プレイヤーの接続が IDLE_MINUTES 分間ゼロの場合に「無人」と判断し、
#     aws ecs update-service --desired-count 0 でタスクを自己停止させる。
#
# 【important: essential=false について】
#   このコンテナは essential=false で定義されているため、
#   スクリプトがクラッシュしてもゲームコンテナは停止しない。
#   安全サイドに倒した設計になっている。
#
# 【環境変数（Terraform から自動注入）】
#   CLUSTER_NAME          - ECS クラスター名
#   SERVICE_NAME          - ECS サービス名
#   AWS_REGION            - AWS リージョン
#   MONITOR_PORT          - 監視するポート番号
#   MONITOR_PROTOCOL      - "tcp" または "udp"
#   MONITOR_METHOD        - "tcp", "a2s", "rest"（未設定時は MONITOR_PROTOCOL から推定）
#   IDLE_MINUTES          - 無人タイムアウト（分）
#   CHECK_INTERVAL        - フェーズB のチェック間隔（秒）、デフォルト 60
#   READY_POLL_INTERVAL   - フェーズA の高速ポーリング間隔（秒）、デフォルト 10
#   STARTUP_GRACE_MINUTES - 受付開始タイムアウト（分）、デフォルト 30
#   READY_PARAM           - SSM パラメータ名（サーバー受付状態: "0"/"1"）
#   PLAYERS_PARAM         - SSM パラメータ名（現在のプレイヤー数）
# =============================================================================

set -e

# --- 環境変数の検証（未設定の場合は即座にエラー終了）---
: "${CLUSTER_NAME:?環境変数 CLUSTER_NAME が設定されていません}"
: "${SERVICE_NAME:?環境変数 SERVICE_NAME が設定されていません}"
: "${AWS_REGION:?環境変数 AWS_REGION が設定されていません}"
: "${MONITOR_PORT:?環境変数 MONITOR_PORT が設定されていません}"
: "${IDLE_MINUTES:?環境変数 IDLE_MINUTES が設定されていません}"

MONITOR_PROTOCOL="${MONITOR_PROTOCOL:-tcp}"
CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
READY_POLL_INTERVAL="${READY_POLL_INTERVAL:-10}"
STARTUP_GRACE_MINUTES="${STARTUP_GRACE_MINUTES:-30}"

echo "[monitor] =========================================="
echo "[monitor] 自動シャットダウン監視スクリプト 起動"
echo "[monitor] クラスター  : ${CLUSTER_NAME}"
echo "[monitor] サービス    : ${SERVICE_NAME}"
echo "[monitor] リージョン  : ${AWS_REGION}"
echo "[monitor] 監視ポート  : ${MONITOR_PORT}/${MONITOR_PROTOCOL}"
echo "[monitor] アイドル上限: ${IDLE_MINUTES} 分"
echo "[monitor] チェック間隔（アイドル監視）: ${CHECK_INTERVAL} 秒"
echo "[monitor] ポーリング間隔（受付待ち）  : ${READY_POLL_INTERVAL} 秒"
echo "[monitor] 起動タイムアウト            : ${STARTUP_GRACE_MINUTES} 分"
echo "[monitor] =========================================="

# =============================================================================
# 依存パッケージのインストール（amazonlinux:2023 ベースイメージ）
# iproute: ss コマンドの提供元
# aws-cli: aws ecs update-service / aws ssm put-parameter の実行に使用
# =============================================================================
# 事前ビルドイメージ（monitor_image に依存パッケージ入りを設定した場合）は
# すでに aws・ss が存在するためインストールをスキップする。
# 素の amazonlinux:2023（既定値）の場合は従来どおり dnf でインストールする。
if command -v aws > /dev/null 2>&1 && command -v ss > /dev/null 2>&1; then
    echo "[monitor] 依存パッケージは事前インストール済みです。インストールをスキップします。"
else
    echo "[monitor] 依存パッケージをインストール中（事前ビルドイメージを使うと省略できます）..."

    if dnf install -y --quiet iproute python3 aws-cli 2>&1; then
        echo "[monitor] dnf インストール完了"
    else
        echo "[monitor] dnf で aws-cli のインストールに失敗。pip3 経由を試みます..."
        dnf install -y --quiet iproute python3 python3-pip 2>&1
        pip3 install awscli --quiet
        echo "[monitor] pip3 インストール完了"
    fi

    # AWS CLI の動作確認
    if ! command -v aws > /dev/null 2>&1; then
        echo "[monitor] エラー: AWS CLI が見つかりません。監視を中断します。"
        exit 1
    fi
fi

echo "[monitor] セットアップ完了。"

# =============================================================================
# SSM ステータスパラメータの初期化（install 直後・受付待ちフェーズ開始前）
# 再起動時に前回の ready=1 をリセットし、古い「受付済み」表示・誤通知を防ぐ
# READY_PARAM が未設定の場合はスキップ（SSM 連携なしで動作）
# =============================================================================
_server_ready=0
if [ -n "${READY_PARAM:-}" ]; then
    echo "[monitor] SSM ステータスパラメータを初期化中（ready=0）..."
    aws ssm put-parameter \
        --name "${READY_PARAM}" \
        --value "0" \
        --type String \
        --overwrite \
        --region "${AWS_REGION}" \
        --no-cli-pager > /dev/null 2>&1 \
        || echo "[monitor] 警告: SSM ready の初期化に失敗しました（権限または READY_PARAM=${READY_PARAM} を確認）"
fi

# =============================================================================
# 共通関数定義
# =============================================================================

# ------------------------------------------------------------------
# check_status: _method に応じた readiness・プレイヤー数チェック
# 設定する変数:
#   _ready      0=初期化中 / 1=受付開始済み
#   conn_count  プレイヤー数（-1=不明）
# ------------------------------------------------------------------
check_status() {
    _method="${MONITOR_METHOD:-}"
    if [ -z "${_method}" ]; then
        if [ "${MONITOR_PROTOCOL}" = "tcp" ]; then
            _method="tcp"
        else
            _method="a2s"
        fi
    fi

    _ready=0
    conn_count=0

    case "${_method}" in

        tcp)
        # TCP 接続数監視（精度高・推奨）
        _listen=$(ss -tlnH "( sport = :${MONITOR_PORT} )" 2>/dev/null | wc -l | tr -d ' \t')
        if [ "${_listen:-0}" -gt 0 ] 2>/dev/null; then
            _ready=1
        fi
        conn_count=$(ss -tnH state established "( sport = :${MONITOR_PORT} )" 2>/dev/null | wc -l | tr -d ' \t')
        ;;

        a2s)
        # Steam A2S_INFO クエリでプレイヤー数を取得
        _a2s_result=$(python3 - <<'PYEOF' 2>/dev/null
import os, socket, sys

PORT = int(os.environ["MONITOR_PORT"])
TIMEOUT = 5

def query_players(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(TIMEOUT)
    try:
        req = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00'
        s.sendto(req, (host, port))
        data, _ = s.recvfrom(1400)

        if len(data) >= 9 and data[4:5] == b'\x41':
            challenge = data[5:9]
            req2 = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00' + challenge
            s.sendto(req2, (host, port))
            data, _ = s.recvfrom(1400)

        if len(data) < 6 or data[4:5] != b'\x49':
            print("0:0")
            return

        pos = 6
        for _ in range(4):
            end = data.index(b'\x00', pos)
            pos = end + 1
        pos += 2
        if pos < len(data):
            print("1:{}".format(data[pos]))
        else:
            print("1:0")

    except (socket.timeout, OSError):
        print("0:0")
    except Exception:
        print("0:0")
    finally:
        s.close()

query_players("127.0.0.1", PORT)
PYEOF
) || _a2s_result="0:0"
        if ! echo "${_a2s_result}" | grep -qE '^[01]:[0-9]+$'; then
            _a2s_result="0:0"
        fi
        _ready=$(echo "${_a2s_result}" | cut -d: -f1)
        conn_count=$(echo "${_a2s_result}" | cut -d: -f2)
        echo "[monitor] A2S クエリ: ready=${_ready} conn_count=${conn_count}"
        ;;

        rest)
        # Palworld REST API でプレイヤー数を取得
        _rest_result=$(python3 - <<'PYEOF' 2>/dev/null
import os, sys, json, base64
import urllib.request, urllib.error

PORT = int(os.environ.get("REST_API_PORT", "8212"))
PASSWORD = os.environ.get("REST_API_PASSWORD", "")
TIMEOUT = 5

url = "http://127.0.0.1:{}/v1/api/players".format(PORT)
credentials = base64.b64encode("admin:{}".format(PASSWORD).encode()).decode()
req = urllib.request.Request(
    url,
    headers={
        "Authorization": "Basic {}".format(credentials),
        "User-Agent": "GameServerBot/1.0",
    },
    method="GET",
)
try:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
        print("1:{}".format(len(data.get("players", []))))
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        print("1:-1")
    else:
        print("0:0")
except Exception:
    print("0:0")
PYEOF
) || _rest_result="0:0"
        if ! echo "${_rest_result}" | grep -qE '^[01]:(-1|[0-9]+)$'; then
            _rest_result="0:0"
        fi
        _ready=$(echo "${_rest_result}" | cut -d: -f1)
        conn_count=$(echo "${_rest_result}" | cut -d: -f2)
        echo "[monitor] REST API クエリ: ready=${_ready} conn_count=${conn_count}"
        ;;

        *)
        echo "[monitor] 警告: 未知の MONITOR_METHOD=${_method}。無人状態とみなします。"
        conn_count=0
        ;;

    esac
}

# ------------------------------------------------------------------
# write_players: SSM players パラメータを更新（PLAYERS_PARAM が設定されている場合）
# ------------------------------------------------------------------
write_players() {
    if [ -n "${PLAYERS_PARAM:-}" ]; then
        aws ssm put-parameter \
            --name "${PLAYERS_PARAM}" \
            --value "${conn_count:-0}" \
            --type String \
            --overwrite \
            --region "${AWS_REGION}" \
            --no-cli-pager > /dev/null 2>&1 || true
    fi
}

# ------------------------------------------------------------------
# write_buildid: インストール済み Steam buildid を SSM に保存
# STEAM_APP_ID が設定されている場合のみ実行（非 Steam 系はスキップ）
# appmanifest_<appid>.acf を EFS から探して buildid を抽出し
# BUILDID_PARAM に書き込む。不在・抽出失敗は警告のみ（fail-open）。
# ------------------------------------------------------------------
write_buildid() {
    [ -z "${STEAM_APP_ID:-}" ]  && return 0
    [ -z "${BUILDID_PARAM:-}" ] && return 0

    local manifest
    manifest=$(find "${EFS_MOUNT_PATH:-/}" \
        -name "appmanifest_${STEAM_APP_ID}.acf" 2>/dev/null | head -n1)

    if [ -z "${manifest}" ]; then
        echo "[monitor] buildid スキップ: appmanifest_${STEAM_APP_ID}.acf が見つかりません（初回 install 前？）"
        return 0
    fi

    local buildid
    buildid=$(python3 - "${manifest}" << 'PYEOF'
import re, sys
try:
    content = open(sys.argv[1]).read()
    m = re.search(r'"buildid"\s+"(\d+)"', content)
    print(m.group(1) if m else "", end="")
except Exception:
    print("", end="")
PYEOF
)

    if [ -z "${buildid}" ]; then
        echo "[monitor] 警告: appmanifest_${STEAM_APP_ID}.acf から buildid を抽出できませんでした"
        return 0
    fi

    echo "[monitor] installed_buildid=${buildid} を SSM ${BUILDID_PARAM} に書き込みます"
    aws ssm put-parameter \
        --name "${BUILDID_PARAM}" \
        --value "${buildid}" \
        --type String \
        --overwrite \
        --region "${AWS_REGION}" \
        --no-cli-pager > /dev/null 2>&1 \
        || echo "[monitor] 警告: buildid の SSM 書き込みに失敗しました（IAM 権限を確認）"
}

# ------------------------------------------------------------------
# do_shutdown: 停止前バックアップ → ECS desired-count=0 → exit
# ------------------------------------------------------------------
do_shutdown() {
    echo "[monitor] ========================================"
    echo "[monitor] 自動シャットダウンを実行します..."
    echo "[monitor] aws ecs update-service --desired-count 0"
    echo "[monitor] ========================================"

    if [ -n "${BACKUP_BUCKET:-}" ]; then
        echo "[monitor] 停止前バックアップ開始: ${EFS_MOUNT_PATH} -> s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/"
        if aws s3 sync "${EFS_MOUNT_PATH}/" "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/" \
            --region "${AWS_REGION}" \
            --no-progress \
            2>&1; then
            echo "[monitor] バックアップ完了"
        else
            echo "[monitor] 警告: バックアップ同期に失敗しました。停止処理は継続します。"
        fi
    fi

    if aws ecs update-service \
        --cluster "${CLUSTER_NAME}" \
        --service "${SERVICE_NAME}" \
        --desired-count 0 \
        --region "${AWS_REGION}" \
        --no-cli-pager \
        > /dev/null 2>&1; then
        echo "[monitor] ECS サービスを停止しました（desired_count=0）。"
        echo "[monitor] ECS がタスクを停止するまでしばらくお待ちください。"
    else
        echo "[monitor] エラー: update-service の実行に失敗しました。IAM 権限を確認してください。"
    fi

    exit 0
}

# =============================================================================
# フェーズA: 受付待ち（高速ポーリング）
# ゲームが接続受付を開始するまで READY_POLL_INTERVAL 秒間隔でチェックする。
# 受付確認後すぐに SSM ready=1 を書込み → EventBridge → notify_ip → Discord 通知。
# STARTUP_GRACE_MINUTES 以内に受付開始しなければコスト保護のため自動停止。
# =============================================================================
startup_grace_seconds=$((STARTUP_GRACE_MINUTES * 60))
startup_elapsed=0

echo "[monitor] フェーズA 開始（受付待ち・${READY_POLL_INTERVAL} 秒間隔・タイムアウト ${STARTUP_GRACE_MINUTES} 分）"

while true; do
    check_status
    write_players

    if [ "${_ready}" = "1" ]; then
        echo "[monitor] ゲームサーバーの受付開始を検知！（起動から約 ${startup_elapsed} 秒）"
        _server_ready=1
        # Steam 系ゲームの場合: appmanifest から installed buildid を読んで SSM に保存する。
        # ready=1 より先に書くことで、Worker Lambda が stop_task する前に確実に永続化される。
        write_buildid
        if [ -n "${READY_PARAM:-}" ]; then
            echo "[monitor] SSM へ ready=1 を書き込みます → Discord IP 通知が送信されます"
            aws ssm put-parameter \
                --name "${READY_PARAM}" \
                --value "1" \
                --type String \
                --overwrite \
                --region "${AWS_REGION}" \
                --no-cli-pager > /dev/null 2>&1 \
                || echo "[monitor] 警告: SSM ready の書き込みに失敗しました（権限を確認）"
        fi
        break
    fi

    startup_elapsed=$((startup_elapsed + READY_POLL_INTERVAL))
    if [ "${startup_elapsed}" -ge "${startup_grace_seconds}" ]; then
        echo "[monitor] 起動タイムアウト（${STARTUP_GRACE_MINUTES} 分）: ゲームが受付を開始しませんでした。自動停止します。"
        do_shutdown
    fi

    remaining_startup=$((startup_grace_seconds - startup_elapsed))
    echo "[monitor] 受付待ち: ${startup_elapsed} 秒経過 / タイムアウトまで ${remaining_startup} 秒"
    sleep "${READY_POLL_INTERVAL}"
done

# =============================================================================
# フェーズB: アイドル監視（既存ロジック）
# 受付開始後、接続ゼロが IDLE_MINUTES 分続いたら自動停止する。
# =============================================================================
idle_seconds=0
idle_limit=$((IDLE_MINUTES * 60))

echo "[monitor] フェーズB 開始（アイドル監視・${IDLE_MINUTES} 分間接続ゼロで自動停止）"

while true; do

    check_status
    write_players

    if [ "${conn_count}" = "-1" ]; then
        # REST API 認証エラー等 → プレイヤー数不明。カウンタ変更なし
        echo "[monitor] 警告: プレイヤー数が不明（REST API 認証エラー？ADMIN_PASSWORD を確認）。アイドルカウンタを変更しません。"

    elif [ "${conn_count}" -gt 0 ] 2>/dev/null; then
        # 接続あり → アイドルカウンタをリセット
        if [ "${idle_seconds}" -gt 0 ]; then
            echo "[monitor] プレイヤーを検知。アイドルカウンタをリセット（接続数: ${conn_count}）"
        fi
        idle_seconds=0

    else
        # 接続なし → アイドルカウンタを加算
        idle_seconds=$((idle_seconds + CHECK_INTERVAL))
        remaining=$((idle_limit - idle_seconds))

        echo "[monitor] 無人状態: ${idle_seconds} 秒 / ${idle_limit} 秒（残り ${remaining} 秒）"

        if [ "${idle_seconds}" -ge "${idle_limit}" ]; then
            echo "[monitor] 無人タイムアウト到達。"
            do_shutdown
        fi
    fi

    sleep "${CHECK_INTERVAL}"
done
