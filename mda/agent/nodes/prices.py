"""价格收集节点（任务书 6.5/7.4）。

- 按计划中的商品清单调用 get_trend + get_price。
- 锂产品没有可用报价时明确记录缺失（DATA_UNAVAILABLE），
  绝不用铜/镍价格替代锂价而不作说明。
- 商品数量上限 2，避免无节制调用。
"""

from __future__ import annotations

from mda.agent.state import AgentState
from mda.common.logging import get_logger
from mda.common.models import ToolResult
from mda.servers.lme_price.commodities import COMMODITY_ALIASES

log = get_logger(__name__)

MAX_COMMODITIES = 2
TREND_DAYS = 30


def normalize_commodity_input(name: str) -> str:
    """防御性归一化：LLM 可能输出带说明文字的商品名（如
    'lithium carbonate futures (GFEX, CNY/tonne)'），从中提取已知别名。"""
    key = (name or "").strip().lower()
    if key in COMMODITY_ALIASES:
        return COMMODITY_ALIASES[key]
    for alias, canonical in COMMODITY_ALIASES.items():
        if len(alias) >= 3 and alias in key:
            return canonical
    return name


async def run_price_collection(state: AgentState, mcp) -> tuple[list[dict], list[dict], list[str]]:
    plan = state.get("plan", {})
    missing: list[str] = []
    errors: list[dict] = []
    results: list[dict] = []

    if not plan.get("need_prices"):
        return results, errors, missing

    commodities = (plan.get("price_commodities") or [])[:MAX_COMMODITIES]
    if not commodities:
        missing.append("计划未指定价格商品，价格数据缺失")
        return results, errors, missing

    report_date = state.get("report_date", "")
    for raw_commodity in commodities:
        commodity = normalize_commodity_input(raw_commodity)
        entry: dict = {"commodity": commodity, "requested_as": raw_commodity}
        trend: ToolResult = await mcp.call(
            "get_trend", {"commodity": commodity, "days": TREND_DAYS}
        )
        if trend.status == "error":
            code = trend.error.code.value if trend.error else "UNKNOWN"
            errors.append(
                {
                    "tool": "get_trend",
                    "error_code": code,
                    "message": trend.error.message if trend.error else "unknown",
                    "retryable": bool(trend.error and trend.error.retryable),
                    "context": {"commodity": commodity},
                }
            )
            if code in ("DATA_UNAVAILABLE", "UNSUPPORTED_COMMODITY", "AUTH_REQUIRED"):
                missing.append(
                    f"商品 {commodity} 价格不可用: "
                    f"{trend.error.message if trend.error else 'unknown'}"
                )
        else:
            entry["trend"] = (
                trend.data.model_dump() if hasattr(trend.data, "model_dump") else dict(trend.data)  # type: ignore[call-overload]
            )
            if trend.warnings:
                entry["trend_warnings"] = trend.warnings

        if report_date:
            snapshot: ToolResult = await mcp.call(
                "get_price", {"commodity": commodity, "date": report_date}
            )
            if snapshot.status == "error":
                errors.append(
                    {
                        "tool": "get_price",
                        "error_code": snapshot.error.code.value if snapshot.error else "UNKNOWN",
                        "message": snapshot.error.message if snapshot.error else "unknown",
                        "retryable": bool(snapshot.error and snapshot.error.retryable),
                        "context": {"commodity": commodity, "date": report_date},
                    }
                )
            else:
                entry["snapshot"] = (
                    snapshot.data.model_dump()
                    if hasattr(snapshot.data, "model_dump")
                    else dict(snapshot.data)  # type: ignore[call-overload]
                )
                if snapshot.warnings:
                    entry["snapshot_warnings"] = snapshot.warnings

        if "trend" in entry or "snapshot" in entry:
            results.append(entry)

    return results, errors, missing


async def collect_prices(state: AgentState, mcp) -> dict:
    results, errors, missing = await run_price_collection(state, mcp)
    return {
        "prices": results,
        "errors_prices": errors,
        "missing_prices": missing,
    }
