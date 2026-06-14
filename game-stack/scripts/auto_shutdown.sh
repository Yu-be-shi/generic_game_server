#!/bin/sh
# =============================================================================
# auto_shutdown.sh - 無人検知・自動シャットダウン サイドカースクリプト
# =============================================================================
#
# 【動作の仕組み】
#   このスクリプトは ECS タスク内の「monitor」コンテナで実行される。
#   Fargate の awsvpc ネットワークモードにより、ゲームコンテナと
#   ネットワーク名前空間を共有しているため、ss コマンドでゲームポートへの
#   接続数を監視できる。
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
#   - 無人判定ロジック（現在は TCP 接続数カウント）
#   - UDP ゲーム向けに A2S_INFO クエリを実装する場合（下記コメント参照）
#   - CHECK_INTERVAL（デフォルト 60 秒）でチェック頻度を調整
#
# 【環境変数（Terraform から自動注入）】
#   CLUSTER_NAME     - ECS クラスター名
#   SERVICE_NAME     - ECS サービス名
#   AWS_REGION       - AWS リージョン
#   MONITOR_PORT     - 監視するポート番号
#   MONITOR_PROTOCOL - "tcp" または "udp"
#   IDLE_MINUTES     - 無人タイムアウト（分）
#   CHECK_INTERVAL   - チェック間隔（秒）、デフォルト 60
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
# aws-cli: aws ecs update-service の実行に使用
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
# メイン監視ループ
# =============================================================================
idle_seconds=0
idle_limit=$((IDLE_MINUTES * 60))

echo "[monitor] 監視ループ開始（${IDLE_MINUTES} 分間接続ゼロで自動停止）"

while true; do

    # =========================================================================
    # 接続数の取得
    # =========================================================================
    if [ "${MONITOR_PROTOCOL}" = "tcp" ]; then
        # ----------------------------------------------------------------
        # TCP 接続数監視（精度高・推奨）
        # ss コマンドで指定ポートへの ESTABLISHED 接続数をカウントする。
        # awsvpc モードでネットワーク名前空間を共有しているため、
        # サイドカーからゲームコンテナのポートを透過的に監視できる。
        # ----------------------------------------------------------------
        conn_count=$(ss -tnH state established "( sport = :${MONITOR_PORT} )" 2>/dev/null | wc -l | tr -d ' \t')

    else
        # ----------------------------------------------------------------
        # UDP: Steam A2S_INFO クエリでプレイヤー数を取得
        #
        # Palworld / Valheim / ARK 等 Steam ベースのゲームは A2S プロトコルに
        # 対応しており、クエリポートにリクエストを送ると現在のプレイヤー数が
        # 取得できる。MONITOR_PORT には Palworld のクエリポート（27015）を設定。
        #
        # 安全側の設計:
        #   - A2S が「0 人」と応答 → アイドル加算（本当に無人なので停止候補）
        #   - タイムアウト / 無応答 / パースエラー → conn_count=1 で停止しない
        #     理由: 起動直後はサーバーが A2S に応答しないため誤停止を防ぐ。
        #     プレイヤーが切断して放置している危険なケースでは A2S は必ず 0 を返す。
        # ----------------------------------------------------------------
        conn_count=$(python3 - <<'PYEOF' 2>/dev/null
import socket, struct, sys

PORT = int("${MONITOR_PORT}")
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
            # 期待しないレスポンス → 稼働中扱い
            print(1)
            return

        # ヘッダ 4B + type 1B + protocol 1B = offset 6 からサーバー名（null終端）開始
        pos = 6
        # null 終端文字列を 4 つスキップ: name, map, folder, game
        for _ in range(4):
            end = data.index(b'\x00', pos)
            pos = end + 1

        # AppID (2B little endian) をスキップ
        pos += 2

        # players バイト（現在のプレイヤー数）
        if pos < len(data):
            print(data[pos])
        else:
            print(1)  # パース失敗 → 稼働中扱い

    except (socket.timeout, OSError):
        # タイムアウト / 接続不可 → サーバー起動中の可能性あり。停止しない
        print(1)
    except Exception:
        print(1)
    finally:
        s.close()

query_players("127.0.0.1", PORT)
PYEOF
)
        # python3 コマンド自体が失敗した場合のフォールバック
        if ! echo "${conn_count}" | grep -qE '^[0-9]+$'; then
            echo "[monitor] 警告: A2S クエリの実行に失敗しました。稼働中として扱います。"
            conn_count=1
        fi
    fi

    # =========================================================================
    # 無人判定とアイドルカウンタの管理
    # =========================================================================
    if [ "${conn_count}" -gt 0 ] 2>/dev/null; then
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
