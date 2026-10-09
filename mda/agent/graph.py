"""LangGraph 主流程（任务书 7.1）。

图结构：
parse_request → plan_tasks → [collect_news ∥ collect_resources ∥ collect_prices]
  → merge_evidence → validate_evidence
  → (retryable 错误且未超限: targeted_retry → merge_evidence)
  → generate_report → verify_report
  → (校验失败且未超限: revise_report → verify_report) → END

预算控制：Agent 补检索最多 MAX_RETRIES_AGENT 次，报告修订最多
MAX_REPORT_REVISIONS 次；不会形成无限 ReAct 循环。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph

from mda.agent.mcp_client import MCPToolRegistry
from mda.agent.nodes.news import run_news_collection
from mda.agent.nodes.prices import run_price_collection
from mda.agent.nodes.report import generate_report_node, revise_report_node
from mda.agent.nodes.resources import run_resource_collection
from mda.agent.nodes.validate import merge_evidence_node, validate_evidence_node
from mda.agent.planner import Planner
from mda.agent.state import AgentState
from mda.agent.verify import verify_report_node
from mda.common.errors import ErrorCode
from mda.common.logging import get_logger

log = get_logger(__name__)

RETRYABLE_TOOLS = {
    "news": {"search", "fetch_article"},
    "resources": {"extract_resources"},
    "prices": {"get_price", "get_trend"},
}


async def parse_request_node(state: AgentState) -> dict:
    query = (state.get("user_query") or "").strip()
    warnings: list[str] = list(state.get("warnings", []))
    if not query:
        return {
            "errors_parse": [
                {
                    "tool": "parse_request",
                    "error_code": ErrorCode.INVALID_ARGUMENT.value,
                    "message": "用户问题为空",
                    "retryable": False,
                    "context": {},
                }
            ]
        }
    if len(query) > 500:
        warnings.append("用户问题超过 500 字符，已截断")
        query = query[:500]
    report_date = (state.get("report_date") or "").strip()
    if not report_date:
        from datetime import date

        report_date = date.today().isoformat()
    return {
        "user_query": query,
        "subject": query,
        "report_date": report_date,
        "warnings": warnings,
    }


async def plan_tasks_node(state: AgentState, planner: Planner) -> dict:
    plan = await planner.build_plan(state)
    return {"plan": plan, "subject": plan.get("subject", state.get("subject", ""))}


async def collect_news_node(state: AgentState, mcp: MCPToolRegistry) -> dict:
    news, errors, missing = await run_news_collection(state, mcp)
    return {"news": news, "errors_news": errors, "missing_news": missing}


async def collect_resources_node(state: AgentState, mcp: MCPToolRegistry) -> dict:
    results, errors, missing = await run_resource_collection(state, mcp)
    return {
        "resources": results,
        "errors_resources": errors,
        "missing_resources": missing,
    }


async def collect_prices_node(state: AgentState, mcp: MCPToolRegistry) -> dict:
    results, errors, missing = await run_price_collection(state, mcp)
    return {"prices": results, "errors_prices": errors, "missing_prices": missing}


async def merge_node(state: AgentState) -> dict:
    return merge_evidence_node(state)


async def validate_node(state: AgentState) -> dict:
    return validate_evidence_node(state)


async def targeted_retry_node(state: AgentState, mcp: MCPToolRegistry) -> dict:
    """只重试存在可重试错误的分支（任务书 7.5）。"""
    errors = state.get("errors", [])
    retryable_tools = {e.get("tool") for e in errors if e.get("retryable")}
    updates: dict[str, Any] = {}

    if retryable_tools & RETRYABLE_TOOLS["news"]:
        news, errs, missing = await run_news_collection(state, mcp)
        updates.update(news=news, errors_news=errs, missing_news=missing)
    if retryable_tools & RETRYABLE_TOOLS["resources"]:
        results, errs, missing = await run_resource_collection(state, mcp)
        updates.update(resources=results, errors_resources=errs, missing_resources=missing)
    if retryable_tools & RETRYABLE_TOOLS["prices"]:
        results, errs, missing = await run_price_collection(state, mcp)
        updates.update(prices=results, errors_prices=errs, missing_prices=missing)

    updates["retry_count"] = state.get("retry_count", 0) + 1
    updates["warnings"] = list(state.get("warnings", [])) + [
        f"已执行第 {updates['retry_count']} 次针对可重试错误的补检索"
    ]
    return updates


def route_after_parse(state: AgentState) -> str:
    """输入非法（空问题）时短路，不再执行任何外部调用。"""
    if state.get("errors_parse"):
        return END
    return "plan_tasks"


def route_after_validate(state: AgentState) -> str:
    errors = state.get("errors", [])
    has_retryable = any(e.get("retryable") for e in errors)
    max_retries = state.get("max_agent_retries", 1)
    if has_retryable and state.get("retry_count", 0) < max_retries:
        return "targeted_retry"
    return "generate_report"


def route_after_verify(state: AgentState) -> str:
    failures = state.get("verify_failures", [])
    max_revisions = state.get("max_report_revisions", 1)
    if failures and state.get("revision_count", 0) < max_revisions:
        return "revise_report"
    return END


def build_graph(
    llm: BaseChatModel,
    mcp: MCPToolRegistry,
    *,
    max_agent_retries: int = 1,
    max_report_revisions: int = 1,
):
    """构建并编译 LangGraph 状态图。"""
    planner = Planner(llm)

    async def _plan(s: AgentState) -> dict:
        return await plan_tasks_node(s, planner)

    async def _collect_news(s: AgentState) -> dict:
        return await collect_news_node(s, mcp)

    async def _collect_resources(s: AgentState) -> dict:
        return await collect_resources_node(s, mcp)

    async def _collect_prices(s: AgentState) -> dict:
        return await collect_prices_node(s, mcp)

    async def _retry(s: AgentState) -> dict:
        return await targeted_retry_node(s, mcp)

    async def _generate(s: AgentState) -> dict:
        return await generate_report_node(s, llm)

    async def _revise(s: AgentState) -> dict:
        return await revise_report_node(s, llm)

    graph = StateGraph(AgentState)

    graph.add_node("parse_request", parse_request_node)  # type: ignore[arg-type]
    graph.add_node("plan_tasks", _plan)  # type: ignore[arg-type]
    graph.add_node("collect_news", _collect_news)  # type: ignore[arg-type]
    graph.add_node("collect_resources", _collect_resources)  # type: ignore[arg-type]
    graph.add_node("collect_prices", _collect_prices)  # type: ignore[arg-type]
    graph.add_node("merge_evidence", merge_node)  # type: ignore[arg-type]
    graph.add_node("validate_evidence", validate_node)  # type: ignore[arg-type]
    graph.add_node("targeted_retry", _retry)  # type: ignore[arg-type]
    graph.add_node("generate_report", _generate)  # type: ignore[arg-type]
    graph.add_node("verify_report", verify_report_node)  # type: ignore[arg-type]
    graph.add_node("revise_report", _revise)  # type: ignore[arg-type]

    graph.set_entry_point("parse_request")
    graph.add_conditional_edges(
        "parse_request",
        route_after_parse,
        {"plan_tasks": "plan_tasks", END: END},
    )
    # 并行 fan-out
    graph.add_edge("plan_tasks", "collect_news")
    graph.add_edge("plan_tasks", "collect_resources")
    graph.add_edge("plan_tasks", "collect_prices")
    # fan-in
    graph.add_edge("collect_news", "merge_evidence")
    graph.add_edge("collect_resources", "merge_evidence")
    graph.add_edge("collect_prices", "merge_evidence")
    graph.add_edge("merge_evidence", "validate_evidence")
    graph.add_conditional_edges(
        "validate_evidence",
        route_after_validate,
        {"targeted_retry": "targeted_retry", "generate_report": "generate_report"},
    )
    graph.add_edge("targeted_retry", "merge_evidence")
    graph.add_edge("generate_report", "verify_report")
    graph.add_conditional_edges(
        "verify_report",
        route_after_verify,
        {"revise_report": "revise_report", END: END},
    )
    graph.add_edge("revise_report", "verify_report")

    compiled = graph.compile()
    return compiled
