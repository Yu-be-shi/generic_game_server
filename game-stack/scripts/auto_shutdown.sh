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
#   ゲームが受付を開始したことを検知したら SSM Parameter Store に
#   ready=1 を書き込む（Discord 通知 Lambda がこれをトリガーに起動通知を送る）。
#   プレイヤー数は毎ループ SSM の players パラメータへ書き込む（/status 用）。
#
#   プレイヤーの接続が ${IDLE_MINUTES} 分間ゼロの場合に「無人」と判断し、
#   aws ecs update-service --desired-count 0 でタスクを自己停止させる。
#   接続を検知したらアイドルカウンタをリセットし、監視を継続する。
#
# 【important: essential=false について】
#   このコンテナは essential=false で定義されているため、
#   スクリプトがクラッシュしてもゲームコンテナは停止しない。
#   安全サイドに倒した設計になっている。
#
# 【改変ポイント】
#   - 無人判定ロジック（MONITOR_METHOD: tcp/a2s/rest）
#   - CHECK_INTERVAL（デフォルト 60 秒）でチェック頻度を調整
#
# 【環境変数（Terraform から自動注入）】
#   CLUSTER_NAME     - ECS クラスター名
#   SERVICE_NAME     - ECS サービス名
#   AWS_REGION       - AWS リージョン
#   MONITOR_PORT     - 監視するポート番号
#   MONITOR_PROTOCOL - "tcp" または "udp"
#   MONITOR_METHOD   - "tcp", "a2s", "rest"（未設定時は MONITOR_PROTOCOL から推定）
#   IDLE_MINUTES     - 無人タイムアウト（分）
#   CHECK_INTERVAL   - チェック間隔（秒）、デフォルト 60
#   READY_PARAM      - SSM パラメータ名（サーバー受付状態: "0"/"1"）
#   PLAYERS_PARAM    - SSM パラメータ名（現在のプレイヤー数）
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

echo "[monitor] =========================================="
echo "[monitor] 自動シャットダウン監視スクリプト 起動"
echo "[monitor] クラスター  : ${CLUSTER_NAME}"
echo "[monitor] サービス    : ${SERVICE_NAME}"
echo "[monitor] リージョン  : ${AWS_REGION}"
echo "[monitor] 監視ポート  : ${MONITOR_PORT}/${MONITOR_PROTOCOL}"
echo "[monitor] アイドル上限: ${IDLE_MINUTES} 分"
echo "[monitor] チェック間隔: ${CHECK_INTERVAL} 秒"
echo "[monitor] =========================================="

# =============================================================================
# 依存パッケージのインストール（amazonlinux:2023 ベースイメージ）
# iproute: ss コマンドの提供元
# aws-cli: aws ecs update-service / aws ssm put-parameter の実行に使用
# =============================================================================
echo "[monitor] 依存パッケージをインストール中（初回のみ時間がかかります）..."

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

echo "[monitor] セットアップ完了。監視を開始します。"

# =============================================================================
# ゲームコンテナの起動待機（起動直後の一時的な接続ゼロによる誤検知を防ぐ）
# =============================================================================
echo "[monitor] ゲームコンテナの起動を待機中（${CHECK_INTERVAL} 秒）..."
sleep "${CHECK_INTERVAL}"

# =============================================================================
# SSM ステータスパラメータの初期化
# 再起動時に前回の ready=1 をリセットし、古い「受付済み」通知の誤発火を防ぐ
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
# メイン監視ループ
# =============================================================================
idle_seconds=0
idle_limit=$((IDLE_MINUTES * 60))

echo "[monitor] 監視ループ開始（${IDLE_MINUTES} 分間接続ゼロで自動停止）"

while true; do

    # =========================================================================
    # 接続数の取得
    # 各メソッドは _ready（0=初期化中 / 1=受付開始済み）と
    # conn_count（プレイヤー数、-1=不明）を設定する
    # MONITOR_METHOD が設定されていない場合は MONITOR_PROTOCOL から後方互換で推定
    # =========================================================================
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
        # ----------------------------------------------------------------
        # TCP 接続数監視（精度高・推奨）
        # ss コマンドでポートリッスン確認と ESTABLISHED 接続数をカウントする。
        # awsvpc モードでネットワーク名前空間を共有しているため、
        # サイドカーからゲームコンテナのポートを透過的に監視できる。
        # ----------------------------------------------------------------
        _listen=$(ss -tlnH "( sport = :${MONITOR_PORT} )" 2>/dev/null | wc -l | tr -d ' \t')
        if [ "${_listen:-0}" -gt 0 ] 2>/dev/null; then
            _ready=1
        fi
        conn_count=$(ss -tnH state established "( sport = :${MONITOR_PORT} )" 2>/dev/null | wc -l | tr -d ' \t')
        ;;

        a2s)
        # ----------------------------------------------------------------
        # Steam A2S_INFO クエリでプレイヤー数を取得
        #
        # 応答あり → ready=1, conn_count=プレイヤー数
        # 応答なし（まだ起動中）→ ready=0, conn_count=0
        # ----------------------------------------------------------------
        _a2s_result=$(python3 - <<'PYEOF' 2>/dev/null
import os, socket, sys

PORT = int(os.environ["MONITOR_PORT"])
TIMEOUT = 5

def query_players(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(TIMEOUT)
    try:
        # --- Step 1: A2S_INFO リクエスト（チャレンジなし）---
        req = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00'
        s.sendto(req, (host, port))
        data, _ = s.recvfrom(1400)

        # チャレンジ応答（0x41 = 'A'）が返ってきた場合は再送
        if len(data) >= 9 and data[4:5] == b'\x41':
            challenge = data[5:9]
            req2 = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00' + challenge
            s.sendto(req2, (host, port))
            data, _ = s.recvfrom(1400)

        # --- Step 2: A2S_INFO レスポンスをパース（0x49 = 'I'）---
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
        # ----------------------------------------------------------------
        # Palworld REST API でプレイヤー数を取得（Palworld 推奨方式）
        #
        # REST_API_ENABLED=true（thijsvanloef イメージのデフォルト）が前提。
        # awsvpc でネットワーク名前空間を共有しているため 127.0.0.1 に到達できる。
        #
        # 応答パターン:
        #   HTTP 200          → ready=1, conn_count=プレイヤー数
        #   HTTP 401/403      → ready=1, conn_count=-1（認証エラー。受付は開始している）
        #   タイムアウト/接続不可 → ready=0, conn_count=0（起動中）
        # ----------------------------------------------------------------
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
        # 認証エラーでもサーバーは受付開始している（パスワード設定の問題）
        print("1:-1")
    else:
        print("0:0")   # その他 HTTP エラー → 起動中とみなす
except Exception:
    print("0:0")       # タイムアウト/接続不可 → 起動中とみなす
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

    # =========================================================================
    # SSM へステータスを書き込む（READY_PARAM / PLAYERS_PARAM が設定されている場合）
    # =========================================================================

    # 初回受付開始を検知したら ready=1 を一度だけ書き込む
    # → EventBridge がこれを検知して notify_ip Lambda が Discord へ通知する
    if [ "${_ready}" = "1" ] && [ "${_server_ready}" = "0" ]; then
        _server_ready=1
        if [ -n "${READY_PARAM:-}" ]; then
            echo "[monitor] ゲームサーバーの受付開始を検知。SSM へ ready=1 を書き込みます。"
            aws ssm put-parameter \
                --name "${READY_PARAM}" \
                --value "1" \
                --type String \
                --overwrite \
                --region "${AWS_REGION}" \
                --no-cli-pager > /dev/null 2>&1 \
                || echo "[monitor] 警告: SSM ready の書き込みに失敗しました（権限を確認）"
        fi
    fi

    # 毎ループ players を更新（/status コマンドが参照）
    if [ -n "${PLAYERS_PARAM:-}" ]; then
        aws ssm put-parameter \
            --name "${PLAYERS_PARAM}" \
            --value "${conn_count:-0}" \
            --type String \
            --overwrite \
            --region "${AWS_REGION}" \
            --no-cli-pager > /dev/null 2>&1 || true
    fi

    # =========================================================================
    # 無人判定とアイドルカウンタの管理
    # =========================================================================
    if [ "${_ready}" = "0" ]; then
        # まだ起動中 → アイドルカウンタを加算しない（起動中の誤検知防止）
        echo "[monitor] サーバー初期化中。アイドルカウンタを一時停止します。"

    elif [ "${conn_count}" = "-1" ]; then
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
            echo "[monitor] ========================================"
            echo "[monitor] 無人タイムアウト到達。自動シャットダウンを実行します..."
            echo "[monitor] aws ecs update-service --desired-count 0"
            echo "[monitor] ========================================"

            # -------------------------------------------------------
            # 停止前バックアップ: EFS → S3 同期
            # BACKUP_BUCKET が設定されている場合のみ実行する
            # -------------------------------------------------------
            if [ -n "${BACKUP_BUCKET:-}" ]; then
                echo "[monitor] 停止前バックアップ開始: ${EFS_MOUNT_PATH} -> s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/"
                if aws s3 sync "${EFS_MOUNT_PATH}/" "s3://${BACKUP_BUCKET}/${BACKUP_PREFIX}/" \
                    --region "${AWS_REGION}" \
                    --no-progress \
                    2>&1; then
                    echo "[monitor] バックアップ完了"
                else
                    # バックアップ失敗でもサーバー停止は継続する（安全設計）
                    echo "[monitor] 警告: バックアップ同期に失敗しました。停止処理は継続します。"
                fi
            fi

            # ECS Service の desired_count を 0 に設定
            # タスクロールの ecs:UpdateService 権限を使って実行する
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

            # コンテナを正常終了（essential=false のためゲームコンテナは継続）
            exit 0
        fi
    fi

    sleep "${CHECK_INTERVAL}"
done
