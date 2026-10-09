"""新闻收集节点：两阶段执行（任务书 7.4）。

第一阶段：search 检索（按计划中的多个检索词，去重合并）。
第二阶段：对 top-N 文章 fetch_article 抓正文（限制并发/数量，避免抓取失控）。
"""

from __future__ import annotations

from mda.agent.state import AgentState
from mda.common.logging import get_logger
from mda.common.models import ToolResult

log = get_logger(__name__)

MAX_SEARCH_QUERIES = 3
MAX_FETCH_ARTICLES = 3


async def run_news_collection(
    state: AgentState, mcp, *, queries: list[str] | None = None
) -> tuple[list[dict], list[dict], list[str]]:
    """执行新闻两阶段收集。返回 (news_list, errors, missing)。"""
    plan = state.get("plan", {})
    queries = queries or (plan.get("news_queries") or [])[:MAX_SEARCH_QUERIES]
    days = int(plan.get("date_range_days", 7))
    errors: list[dict] = []
    missing: list[str] = []
    warnings: list[str] = []

    # 第一阶段：检索
    articles: list[dict] = []
    seen_urls: set[str] = set()
    for query in queries:
        result: ToolResult = await mcp.call("search", {"query": query, "days": days})
        if result.status == "error":
            errors.append(
                {
                    "tool": "search",
                    "error_code": result.error.code.value if result.error else "UNKNOWN",
                    "message": result.error.message if result.error else "unknown",
                    "retryable": bool(result.error and result.error.retryable),
                    "context": {"query": query},
                }
            )
            continue
        data = result.data if isinstance(result.data, dict) else {}
        for art in data.get("articles", []) or []:
            url = art.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            articles.append(art)
        if result.warnings:
            warnings.extend(result.warnings)

    # 第二阶段：正文抓取（top-N）
    fetched = 0
    for art in articles:
        if fetched >= MAX_FETCH_ARTICLES:
            break
        url = art.get("url", "")
        if not url:
            continue
        fetched += 1
        result = await mcp.call("fetch_article", {"url": url})
        if result.status == "error":
            errors.append(
                {
                    "tool": "fetch_article",
                    "error_code": result.error.code.value if result.error else "UNKNOWN",
                    "message": result.error.message if result.error else "unknown",
                    "retryable": bool(result.error and result.error.retryable),
                    "context": {"url": url},
                }
            )
            continue
        content = result.data if isinstance(result.data, dict) else {}
        art["content"] = content.get("content", "")
        art["author"] = content.get("author", "")
        art["final_url"] = content.get("final_url", url)
        art["content_extracted"] = bool(art["content"])
        if result.warnings:
            art["extraction_warnings"] = result.warnings

    if not articles:
        missing.append(
            f"未检索到与 {state.get('subject') or plan.get('subject') or '主题'} "
            "相关的近期新闻（数据缺失）"
        )
    return articles, errors, missing


async def collect_news(state: AgentState, mcp) -> dict:
    news, errors, missing = await run_news_collection(state, mcp)
    return {"news": news, "errors_news": errors, "missing_news": missing}
