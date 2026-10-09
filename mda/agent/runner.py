"""Agent 运行器：连接 MCP → 运行图 → 返回最终状态（带整体时限）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mda.agent.graph import build_graph
from mda.agent.llm_factory import build_llm
from mda.agent.mcp_client import MCPToolRegistry
from mda.agent.registry import load_registry
from mda.agent.state import new_state
from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger, request_context
from mda.common.settings import Settings

log = get_logger(__name__)


async def run_agent(
    query: str,
    report_date: str,
    settings: Settings | None = None,
    *,
    known_pdfs: list[dict] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """执行一次完整日报任务。

    返回最终 AgentState（含 report_markdown / evidence / sources /
    missing_data / warnings / errors / elapsed_ms）。
    """
    settings = settings or Settings()
    started = time.perf_counter()
    mcp = MCPToolRegistry(settings)
    try:
        with request_context(request_id or "agent"):
            await mcp.connect()
            missing_required = mcp.missing_required_tools()
            if missing_required:
                raise AppError(
                    ErrorCode.MCP_CONNECTION_FAILED,
                    f"MCP 工具缺失: {missing_required}",
                    retryable=True,
                )
            llm = build_llm(settings)
            graph = build_graph(
                llm,
                mcp,
                max_agent_retries=settings.max_retries_agent,
                max_report_revisions=settings.max_report_revisions,
            )
            state = new_state(query, report_date)
            state["known_pdfs"] = known_pdfs if known_pdfs is not None else load_registry()
            state["max_agent_retries"] = settings.max_retries_agent
            state["max_report_revisions"] = settings.max_report_revisions

            final = await asyncio.wait_for(
                graph.ainvoke(state),
                timeout=settings.task_timeout_seconds,
            )
            # 图在 parse 短路（空 query 等）时 merge 未执行：把 parse 错误合并进
            # 统一 errors 通道，保证调用方（API/CLI）能看到输入校验错误。
            if final.get("errors_parse") and not final.get("errors"):
                final["errors"] = list(final["errors_parse"])
            # 修订次数用尽后仍有校验失败项：在报告中显式标记人工核查
            failures = final.get("verify_failures", [])
            if failures and final.get("report_markdown"):
                final["report_markdown"] += (
                    "\n\n---\n\n## ⚠️ 需人工核查（确定性校验失败项）\n\n"
                    + "\n".join(f"- {f}" for f in failures)
                    + "\n\n上述表述无法由本日证据支撑，请人工核实后再对外使用。\n"
                )
            final["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            log.info(
                "agent_run_finished",
                elapsed_ms=final["elapsed_ms"],
                evidence_count=len(final.get("evidence", [])),
                missing_count=len(final.get("missing_data", [])),
                error_count=len(final.get("errors", [])),
                report_len=len(final.get("report_markdown", "")),
            )
            return final
    except asyncio.TimeoutError as exc:
        raise AppError(
            ErrorCode.UPSTREAM_TIMEOUT,
            f"整体任务超过时限 {settings.task_timeout_seconds}s",
            retryable=False,
        ) from exc
    finally:
        await mcp.aclose()
