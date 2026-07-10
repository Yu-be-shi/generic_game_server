"""cost.py - /cost: 今月の AWS コスト・予算残・月末予測を返す"""
import logging
from datetime import date, timedelta

from clients import budgets, ce, sts_client

logger = logging.getLogger()


def cmd_cost() -> str:
    """
    /cost: 今月の AWS コスト・月末着地予測・予算と残額を返す。

    Cost Explorer / Budgets はグローバルサービスのため us-east-1 クライアントを使用。
    コスト配分タグの有効化は不要（アカウント全体合計を集計）。
    """
    today       = date.today()
    month_start = today.replace(day=1)

    lines = [f"💰 **今月の AWS コスト** ({month_start.isoformat()} 〜 {today.isoformat()})\n"]
    lines.append(_cost_mtd_line(today))
    forecast_line = _cost_forecast_line(today)
    if forecast_line:
        lines.append(forecast_line)
    lines.extend(_cost_budget_lines())

    return "\n".join(lines)


def _cost_mtd_line(today: date) -> str:
    """今月累計 (MTD) の1行を返す（取得失敗時もエラー文言の1行を返す）。"""
    month_start = today.replace(day=1)
    tomorrow    = today + timedelta(days=1)
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": month_start.isoformat(), "End": tomorrow.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        result_by_time = resp.get("ResultsByTime", [])
        if not result_by_time:
            return "使用額（MTD）: 取得失敗"
        total  = result_by_time[0]["Total"]["UnblendedCost"]
        amount = float(total["Amount"])
        unit   = total["Unit"]
        return f"使用額（MTD）: **${amount:.2f} {unit}**"
    except Exception:
        logger.exception("Cost Explorer: MTD 取得失敗")
        return "使用額（MTD）: 取得失敗"


def _cost_forecast_line(today: date):
    """
    月末着地予測の1行を返す。
    履歴が少ない（新アカウント等）と DataUnavailableException が送出されるため None（サイレントスキップ）。
    """
    if today.month == 12:
        next_month_first = date(today.year + 1, 1, 1)
    else:
        next_month_first = date(today.year, today.month + 1, 1)

    try:
        forecast_resp = ce.get_cost_forecast(
            TimePeriod={"Start": today.isoformat(), "End": next_month_first.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        forecast_total  = forecast_resp.get("Total", {})
        forecast_amount = float(forecast_total.get("Amount", 0))
        forecast_unit   = forecast_total.get("Unit", "USD")
        return f"月末着地予測: **${forecast_amount:.2f} {forecast_unit}**"
    except Exception:
        # 履歴不足等で予測が取れない場合はサイレントスキップ
        logger.info("Cost Explorer: 予測取得失敗（履歴不足の可能性）")
        return None


def _cost_budget_lines() -> list:
    """予算と残額の行リストを返す（未設定なら1行、取得失敗なら空リスト）。"""
    try:
        account_id  = sts_client.get_caller_identity()["Account"]
        b_resp      = budgets.describe_budgets(AccountId=account_id)
        budget_list = b_resp.get("Budgets", [])
        if not budget_list:
            return ["（予算未設定）"]

        lines = [""]  # 空行
        for b in budget_list:
            name       = b.get("BudgetName", "")
            limit      = float(b.get("BudgetLimit", {}).get("Amount", 0))
            limit_unit = b.get("BudgetLimit", {}).get("Unit", "USD")
            actual     = float(b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0))
            remaining  = limit - actual
            lines.append(
                f"📊 **{name}**\n"
                f"  予算: ${limit:.2f} / 使用: ${actual:.2f} / 残: ${remaining:.2f} {limit_unit}"
            )
        return lines
    except Exception:
        logger.exception("Budgets: 取得失敗")
        return []
