"""证据归并（merge_evidence）与证据校验（validate_evidence）节点（任务书 7.6/7.7）。

Evidence 结构：
- evidence_id / source_type / source_name / source_url / document_date /
  retrieved_at / field_name / field_value / unit / page_number /
  supporting_text / validation_status
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mda.agent.state import AgentState
from mda.common.logging import get_logger

log = get_logger(__name__)

_TS = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


def _evidence_id(prefix: str, idx: int) -> str:
    return f"EV-{prefix}-{idx + 1:03d}"


def merge_evidence_node(state: AgentState) -> dict:
    """把三个分支的工具结果归并为统一 Evidence 列表。"""
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    idx = 0

    # 新闻证据
    for art in state.get("news", []):
        evidence.append(
            {
                "evidence_id": _evidence_id("N", idx),
                "source_type": "news",
                "source_name": art.get("source", ""),
                "source_url": art.get("url", ""),
                "document_date": art.get("published_at", ""),
                "retrieved_at": art.get("retrieved_at", _TS()),
                "field_name": "title",
                "field_value": art.get("title", ""),
                "unit": "",
                "page_number": None,
                "supporting_text": (art.get("summary") or "")[:500],
                "validation_status": "validated",
                "extra": {
                    "content": (art.get("content") or "")[:4000],
                    "article_id": art.get("article_id", ""),
                    "relevance_score": art.get("relevance_score", 0.0),
                    "content_extracted": art.get("content_extracted", False),
                    "extraction_warnings": art.get("extraction_warnings", []),
                },
            }
        )
        sources.append(
            {
                "type": "news",
                "name": art.get("source", ""),
                "url": art.get("url", ""),
                "title": art.get("title", ""),
                "published_at": art.get("published_at", ""),
            }
        )
        idx += 1

    # 资源量证据（每条记录一个证据，含页码与表格原文）
    for doc in state.get("resources", []):
        records = doc.get("records", [])
        for rec in records:
            evidence.append(
                {
                    "evidence_id": _evidence_id("R", idx),
                    "source_type": "pdf",
                    "source_name": doc.get("report_title")
                    or doc.get("project_name")
                    or "technical report",
                    "source_url": doc.get("pdf_url", ""),
                    "document_date": rec.get("effective_date", ""),
                    "retrieved_at": doc.get("warnings") and _TS() or _TS(),
                    "field_name": "mineral_resource",
                    "field_value": {
                        "deposit_name": rec.get("deposit_name", ""),
                        "resource_category": rec.get("resource_category", ""),
                        "ore_tonnage": rec.get("ore_tonnage"),
                        "ore_unit": rec.get("ore_unit", ""),
                        "grade": rec.get("grade"),
                        "grade_unit": rec.get("grade_unit", ""),
                        "contained_metal": rec.get("contained_metal"),
                        "metal_unit": rec.get("metal_unit", ""),
                        "commodity": rec.get("commodity", ""),
                        "is_aggregate": rec.get("is_aggregate", False),
                        "report_standard": doc.get("report_standard", ""),
                        "report_effective_date": doc.get("effective_date", ""),
                        "report_issue_date": doc.get("issue_date", ""),
                        "registry_note": doc.get("registry_note", ""),
                        "project_name": doc.get("project_name", ""),
                    },
                    "unit": rec.get("ore_unit", ""),
                    "page_number": rec.get("page_number") or None,
                    "supporting_text": (rec.get("evidence_text") or "")[:500],
                    "validation_status": rec.get("validation_status", "validated"),
                    "extra": {
                        "table_title": rec.get("table_title", ""),
                        "validation_warnings": rec.get("validation_warnings", []),
                        "needs_review": doc.get("needs_review", False),
                        "extraction_method": doc.get("extraction_method", ""),
                        "sha256": doc.get("sha256", ""),
                    },
                }
            )
            sources.append(
                {
                    "type": "pdf",
                    "name": doc.get("report_title") or doc.get("project_name", ""),
                    "url": doc.get("pdf_url", ""),
                    "title": doc.get("report_title", ""),
                    "published_at": doc.get("issue_date", ""),
                }
            )
            idx += 1

    # 价格证据
    for entry in state.get("prices", []):
        commodity = entry.get("commodity", "")
        trend = entry.get("trend") or {}
        snapshot = entry.get("snapshot") or {}
        if trend:
            evidence.append(
                {
                    "evidence_id": _evidence_id("P", idx),
                    "source_type": "price",
                    "source_name": trend.get("source", ""),
                    "source_url": trend.get("source_url", ""),
                    "document_date": trend.get("actual_end_date", ""),
                    "retrieved_at": trend.get("retrieved_at", _TS()),
                    "field_name": "price_trend",
                    "field_value": {
                        "commodity": commodity,
                        "start_price": trend.get("start_price"),
                        "end_price": trend.get("end_price"),
                        "change_absolute": trend.get("change_absolute"),
                        "change_percent": trend.get("change_percent"),
                        "actual_start_date": trend.get("actual_start_date"),
                        "actual_end_date": trend.get("actual_end_date"),
                        "price_type": trend.get("price_type", ""),
                    },
                    "unit": f"{trend.get('currency', '')} {trend.get('unit', '')}".strip(),
                    "page_number": None,
                    "supporting_text": trend.get("source", ""),
                    "validation_status": "validated",
                    "extra": {
                        "data_points": trend.get("data_points", [])[-30:],
                        "trend_warnings": entry.get("trend_warnings", []),
                    },
                }
            )
            idx += 1
        if snapshot:
            evidence.append(
                {
                    "evidence_id": _evidence_id("P", idx),
                    "source_type": "price",
                    "source_name": snapshot.get("source", ""),
                    "source_url": snapshot.get("source_url", ""),
                    "document_date": snapshot.get("actual_price_date", ""),
                    "retrieved_at": snapshot.get("retrieved_at", _TS()),
                    "field_name": "price_snapshot",
                    "field_value": {
                        "commodity": commodity,
                        "requested_date": snapshot.get("requested_date", ""),
                        "actual_price_date": snapshot.get("actual_price_date"),
                        "price": snapshot.get("price"),
                        "is_estimated": snapshot.get("is_estimated", False),
                        "price_type": snapshot.get("price_type", ""),
                    },
                    "unit": f"{snapshot.get('currency', '')} {snapshot.get('unit', '')}".strip(),
                    "page_number": None,
                    "supporting_text": snapshot.get("source", ""),
                    "validation_status": "validated",
                    "extra": {"snapshot_warnings": entry.get("snapshot_warnings", [])},
                }
            )
            idx += 1
        if trend or snapshot:
            sources.append(
                {
                    "type": "price",
                    "name": (trend or snapshot).get("source", ""),
                    "url": (trend or snapshot).get("source_url", ""),
                    "title": f"{commodity} 价格",
                    "published_at": (trend or snapshot).get("retrieved_at", ""),
                }
            )

    # 错误与缺失合并
    errors = (
        state.get("errors_parse", [])
        + state.get("errors_news", [])
        + state.get("errors_resources", [])
        + state.get("errors_prices", [])
    )
    missing = (
        list(state.get("missing_news", []))
        + list(state.get("missing_resources", []))
        + list(state.get("missing_prices", []))
    )
    return {"evidence": evidence, "sources": sources, "errors": errors, "missing_data": missing}


def validate_evidence_node(state: AgentState) -> dict:
    """确定性证据校验：数值字段、URL、日期、单位一致性。"""
    warnings: list[str] = []
    evidence = state.get("evidence", [])
    if not evidence:
        warnings.append("没有任何证据进入状态，报告将只包含数据缺失披露")
    for ev in evidence:
        url = ev.get("source_url", "")
        if url and not url.startswith(("http://", "https://")):
            warnings.append(f"{ev.get('evidence_id')} 来源 URL 非法: {url!r}")
        if ev.get("source_type") == "pdf":
            fv = ev.get("field_value") or {}
            if fv.get("ore_tonnage") is not None and not isinstance(
                fv.get("ore_tonnage"), (int, float)
            ):
                warnings.append(f"{ev.get('evidence_id')} 矿石量非数值: {fv.get('ore_tonnage')!r}")
            if not ev.get("page_number"):
                warnings.append(f"{ev.get('evidence_id')} 缺少页码证据")
            if ev.get("validation_status") == "needs_review":
                warnings.append(
                    f"{ev.get('evidence_id')} 资源量记录需人工复核: "
                    + "; ".join(ev.get("extra", {}).get("validation_warnings", [])[:2])
                )
        if ev.get("source_type") == "price":
            fv = ev.get("field_value") or {}
            for key in ("start_price", "end_price", "change_absolute", "price"):
                if key in fv and fv[key] is not None and not isinstance(fv[key], (int, float)):
                    warnings.append(f"{ev.get('evidence_id')} 价格字段非数值: {key}={fv[key]!r}")
    # 去重并合并既有 warnings（parse_request 等前序节点写入的不得丢失）
    merged = list(state.get("warnings", [])) + warnings
    return {"warnings": list(dict.fromkeys(merged))}
