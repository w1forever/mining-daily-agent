"""资源量收集节点（任务书 5.5/7.4）。

PDF 来源只允许两个渠道：
1. 证据注册表（data/known_pdfs.json，内含已实测验证的 NI 43-101 PDF）；
2. 检索证据中的可信链接（本实现不启用新闻->PDF 自动发现，避免抓取失控）。

绝不信任 LLM 生成的 PDF URL。目标矿区（如 Pilbara 的 Pilgangoora）只有
JORC 报告时，明确披露目标报告缺失，并可用注册表中的真实 NI 43-101 报告
完成工具能力验收（在日报中显式标注其非目标矿区）。
"""

from __future__ import annotations

from mda.agent.state import AgentState
from mda.common.logging import get_logger
from mda.common.models import ToolResult

log = get_logger(__name__)

MAX_PDF_DOCUMENTS = 2


def pick_documents(registry: list[dict], commodity: str | None) -> list[dict]:
    """从注册表按矿种挑选文档（矿种优先，其次按验证时间）。"""
    docs = [d for d in registry if d.get("enabled", True)]
    if commodity:
        matched = [d for d in docs if d.get("commodity") == commodity]
        docs = matched + [d for d in docs if d.get("commodity") != commodity]
    return docs[:MAX_PDF_DOCUMENTS]


async def run_resource_collection(
    state: AgentState, mcp
) -> tuple[list[dict], list[dict], list[str]]:
    plan = state.get("plan", {})
    missing: list[str] = []
    errors: list[dict] = []
    results: list[dict] = []

    if not plan.get("need_resources"):
        return results, errors, missing

    registry = state.get("known_pdfs", [])
    commodity = plan.get("commodity")
    docs = pick_documents(registry, commodity)
    if not docs:
        missing.append(
            f"未在证据注册表中找到 {commodity or '目标矿种'} 的适用 NI 43-101 报告，"
            "目标矿区资源量数据缺失"
        )
        return results, errors, missing

    # 目标矿区披露：注册表文档与查询主体不同矿区时显式标注
    subject = str(state.get("subject") or plan.get("subject") or "")
    for doc in docs:
        result: ToolResult = await mcp.call("extract_resources", {"pdf_url": doc["url"]})
        if result.status == "error":
            errors.append(
                {
                    "tool": "extract_resources",
                    "error_code": result.error.code.value if result.error else "UNKNOWN",
                    "message": result.error.message if result.error else "unknown",
                    "retryable": bool(result.error and result.error.retryable),
                    "context": {"pdf_url": doc["url"]},
                }
            )
            if result.error and result.error.code.value in (
                "DOCUMENT_NOT_FOUND",
                "RESOURCE_TABLE_NOT_FOUND",
                "INVALID_URL",
            ):
                missing.append(
                    f"PDF 资源量抽取失败（不可重试）: {doc['url']} - {result.error.message}"
                )
            continue
        data = result.data
        if data is None:
            continue
        entry = data.model_dump() if hasattr(data, "model_dump") else dict(data)  # type: ignore[call-overload]
        entry["registry_id"] = doc.get("id", "")
        entry["registry_note"] = doc.get("note", "")
        entry["project_name"] = doc.get("project", "")
        if (
            subject
            and doc.get("project")
            and subject.lower() not in (str(doc.get("project", "")).lower())
        ):
            missing.append(
                f"目标矿区（{subject}）无适用 NI 43-101 报告；"
                f"本次抽取的是能力验收报告 {doc.get('project')}（{doc.get('standard')}，"
                f"生效日 {doc.get('effective_date', '')}），非目标矿区数据。"
            )
        results.append(entry)
        if result.warnings:
            entry["tool_warnings"] = result.warnings
    return results, errors, missing


async def collect_resources(state: AgentState, mcp) -> dict:
    results, errors, missing = await run_resource_collection(state, mcp)
    return {
        "resources": results,
        "errors_resources": errors,
        "missing_resources": missing,
    }
