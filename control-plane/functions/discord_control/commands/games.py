"""games.py - /games: 全ゲームサーバーの一覧と稼働状態を返す"""
import ecs_helpers


def cmd_games() -> str:
    """/games: 全ゲームの一覧と稼働状態を返す"""
    clusters = ecs_helpers.list_game_clusters()

    if not clusters:
        return (
            "🔍 **ゲームサーバーが見つかりませんでした**\n"
            "`game-stack` はデプロイされていますか？"
        )

    lines = ["**🎮 ゲームサーバー一覧**\n"]
    for c in clusters:
        desired = c["desired_count"]
        running = c["running_count"]

        if desired > 0 and running > 0:
            icon = "🟢"
            stat = "稼働中"
        elif desired > 0 and running == 0:
            icon = "🟡"
            stat = "起動中..."
        else:
            icon = "⚫"
            stat = "停止中"

        lines.append(f"{icon} **{c['game_tag']}**: {stat}")

    lines.append("\n*`/status game:<name>` で IP を確認できます*")
    return "\n".join(lines)
