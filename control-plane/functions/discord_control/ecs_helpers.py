"""
ecs_helpers.py - ECS クラスター/サービス検索と SSM ステータス読み取りの共通ユーティリティ

commands/ 配下の各コマンド実装から共有される、AWS 側の読み取り専用ロジックを集約する。
"""
import logging
from datetime import datetime, timezone

from clients import ec2, ecs, ssm
from ecs_net import get_running_task_public_ip
from ssm_params import ssm_get, ssm_get_parameter

logger = logging.getLogger()


def _list_game_clusters_info() -> list:
    """
    Game タグが付いた ECS クラスターの describe_clusters 結果を返す。

    list_game_clusters / list_game_names / find_service が共通で行う
    「list_clusters → describe_clusters(include=["TAGS"]) → Game タグ抽出」を集約する。
    各エントリは {"cluster_arn": ..., "game_tag": ...}。
    取得失敗時は空リストを返す（呼び出し元は例外を気にせずフォールバックできる）。
    """
    try:
        cluster_arns = ecs.list_clusters()["clusterArns"]
    except Exception:
        logger.exception("list_clusters 失敗")
        return []

    if not cluster_arns:
        return []

    clusters_info = ecs.describe_clusters(clusters=cluster_arns, include=["TAGS"])["clusters"]
    result = []
    for c in clusters_info:
        game_tag = next((t["value"] for t in c.get("tags", []) if t["key"] == "Game"), None)
        if not game_tag:
            continue
        result.append({"cluster_arn": c["clusterArn"], "game_tag": game_tag})
    return result


def list_game_clusters() -> list:
    """
    全クラスターの中から Game タグが付いているものを返す。
    各エントリに game_tag, cluster_arn, desired_count, running_count を含む。
    """
    result = []
    for entry in _list_game_clusters_info():
        cluster_arn = entry["cluster_arn"]
        desired, running = 0, 0
        try:
            svc_arns = ecs.list_services(cluster=cluster_arn)["serviceArns"]
            if svc_arns:
                svc = ecs.describe_services(cluster=cluster_arn, services=svc_arns)["services"]
                if svc:
                    desired = svc[0].get("desiredCount", 0)
                    running = svc[0].get("runningCount", 0)
        except Exception:
            logger.exception("サービス情報の取得に失敗: %s", cluster_arn)

        result.append({
            "game_tag":     entry["game_tag"],
            "cluster_arn":  cluster_arn,
            "desired_count": desired,
            "running_count": running,
        })

    return sorted(result, key=lambda x: x["game_tag"])


def list_game_names() -> list:
    """
    ECS クラスターの Game タグからゲーム名一覧を返す（軽量版）。

    オートコンプリート用途のため describe_services は呼ばず、
    タグ取得のみで済ませる（Discord の ~3 秒 autocomplete 制限に収めるため）。
    """
    try:
        names = [entry["game_tag"] for entry in _list_game_clusters_info()]
        return sorted(names)
    except Exception:
        logger.exception("list_game_names 失敗")
        return []


def find_service(game_name: str):
    """
    game_name に対応するクラスター ARN とサービス ARN を返す。
    見つからない場合は (None, None)。

    照合優先順位:
      1. Game タグとの完全一致（大文字小文字無視）
      2. Game タグへの一意な部分一致（autocomplete 未使用で手打ちした場合の救済）
         複数候補がある場合はあいまいなため not found とする。
    """
    clusters_info = _list_game_clusters_info()
    if not clusters_info:
        return None, None

    name_lower = game_name.lower()

    exact_match   = None      # (cluster_arn, svc_arn)
    partial_matches = []      # [(cluster_arn, svc_arn)]

    for entry in clusters_info:
        cluster_arn = entry["cluster_arn"]
        game_tag    = entry["game_tag"]

        try:
            svc_arns = ecs.list_services(cluster=cluster_arn)["serviceArns"]
        except Exception:
            continue

        if not svc_arns:
            continue

        if game_tag.lower() == name_lower:
            exact_match = (cluster_arn, svc_arns[0])
            break  # 完全一致が見つかれば即確定

        if name_lower in game_tag.lower():
            partial_matches.append((cluster_arn, svc_arns[0]))

    if exact_match:
        return exact_match

    # 部分一致が一意ならフォールバック採用（複数候補は曖昧なため不採用）
    if len(partial_matches) == 1:
        logger.info("find_service: 部分一致フォールバック採用: input=%s", game_name)
        return partial_matches[0]

    if len(partial_matches) > 1:
        logger.info("find_service: 部分一致が複数あり採用不可: input=%s count=%d", game_name, len(partial_matches))

    return None, None


def describe_service(cluster_arn: str, service_arn: str):
    """サービス情報を取得する"""
    try:
        result   = ecs.describe_services(cluster=cluster_arn, services=[service_arn])
        services = result.get("services", [])
        return services[0] if services else None
    except Exception:
        logger.exception("describe_services 失敗")
        return None


def get_latest_task_def_arn(family: str):
    """
    タスク定義ファミリーの最新 ACTIVE リビジョン ARN を返す。
    取得に失敗した場合は None を返す（呼び出し元がフォールバック処理を行う）。
    """
    try:
        result = ecs.describe_task_definition(taskDefinition=family)
        arn    = result.get("taskDefinition", {}).get("taskDefinitionArn")
        logger.info("最新タスク定義 ARN 取得: family=%s arn=%s", family, arn)
        return arn
    except Exception:
        logger.exception("最新タスク定義 ARN の取得に失敗: family=%s", family)
        return None


def get_running_task_info(cluster_arn: str):
    """
    実行中タスクのパブリック IP、タスク定義 ARN、タスクインスタンス ARN を返す。
    どちらかを取得できない場合は対応する要素を None にして返す。
    """
    try:
        return get_running_task_public_ip(ecs, ec2, cluster_arn)
    except Exception:
        logger.exception("タスク情報取得失敗")
        return None, None, None


def get_cluster_tag(cluster_arn: str, tag_key: str):
    """クラスターの指定タグ値を返す。なければ None。"""
    try:
        clusters = ecs.describe_clusters(clusters=[cluster_arn], include=["TAGS"])["clusters"]
        if not clusters:
            return None
        tags = {t["key"]: t["value"] for t in clusters[0].get("tags", [])}
        return tags.get(tag_key)
    except Exception:
        logger.exception("クラスタータグ取得失敗: %s", cluster_arn)
        return None


def get_ssm_status(prefix: str):
    """
    SSM からゲームサーバーの受付状態・プレイヤー数・ready=1 経過時間を読み取る。

    Returns:
        (ready, players, ready_age_seconds)
        ready=True         → ゲームが接続受付中（monitor サイドカーが確認済み）
        ready=False        → まだ初期化中（または SSM 未書込み）
        players=None       → プレイヤー数不明
        ready_age_seconds  → ready=1 になってからの経過秒数。ready=False または取得失敗時は None
    """
    ready = False
    players = None
    ready_age_seconds = None

    try:
        param = ssm_get_parameter(ssm, f"{prefix}/ready")
        if param is None:
            logger.debug("SSM ready パラメータ未取得（初回起動前か権限不足）: %s/ready", prefix)
        else:
            ready = param["Value"] == "1"
            if ready:
                last_modified     = param["LastModifiedDate"]  # timezone-aware datetime
                ready_age_seconds = (datetime.now(timezone.utc) - last_modified).total_seconds()
    except Exception:
        # ParameterNotFound（初回起動前）は想定内、それ以外はデバッグログ
        logger.debug("SSM ready パラメータ未取得（初回起動前か権限不足）: %s/ready", prefix)

    if ready:
        try:
            players_value = ssm_get(ssm, f"{prefix}/players")
            players = int(players_value) if players_value is not None else None
        except ValueError:
            logger.warning("SSM players の値が整数ではありません: %s/players", prefix)
        except Exception:
            logger.debug("SSM players パラメータ未取得: %s/players", prefix)

    return ready, players, ready_age_seconds


def get_notified_task(prefix: str):
    """
    IP 通知済みタスク ARN を SSM から読む。

    notify_ip Lambda が通知を送信した後に
    /ggs/<prefix>/notified_task へ記録するタスク ARN を返す。
    未通知（パラメータ未存在）の場合は None を返す。
    """
    try:
        return ssm_get(ssm, f"{prefix}/notified_task")
    except Exception:
        return None
