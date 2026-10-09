"""AgentState 定义（任务书 7.2）。

并行节点（collect_news / collect_resources / collect_prices）各自只写
自己的字段（news / resources / prices），避免无控制地写入同一字段。
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # 输入
    user_query: str
    report_date: str  # YYYY-MM-DD
    known_pdfs: list[dict[str, Any]]  # 可信 NI 43-101 PDF 注册表
    max_agent_retries: int
    max_report_revisions: int

    # 解析与计划
    subject: str
    intent: dict[str, Any]
    plan: dict[str, Any]

    # 工具结果（各并行节点独占写入，互不冲突）
    news: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    prices: list[dict[str, Any]]
    # 各并行节点独占的错误通道（merge 时合并为 errors）
    errors_parse: list[dict[str, Any]]
    errors_news: list[dict[str, Any]]
    errors_resources: list[dict[str, Any]]
    errors_prices: list[dict[str, Any]]
    # 各并行节点独占的缺失通道（merge 时合并为 missing_data）
    missing_news: list[str]
    missing_resources: list[str]
    missing_prices: list[str]

    # 证据与状态（merge 后写入）
    evidence: list[dict[str, Any]]
    missing_data: list[str]
    errors: list[dict[str, Any]]
    warnings: list[str]

    # 预算控制
    retry_count: int  # targeted_retry 已执行次数（上限 MAX_RETRIES_AGENT）
    revision_count: int  # 报告修订次数（上限 MAX_REPORT_REVISIONS）

    # 输出
    report_markdown: str
    sources: list[dict[str, Any]]
    verify_failures: list[str]
    elapsed_ms: int


def new_state(user_query: str, report_date: str) -> AgentState:
    return AgentState(
        user_query=user_query,
        report_date=report_date,
        known_pdfs=[],
        max_agent_retries=1,
        max_report_revisions=1,
        subject="",
        intent={},
        plan={},
        news=[],
        resources=[],
        prices=[],
        errors_parse=[],
        errors_news=[],
        errors_resources=[],
        errors_prices=[],
        missing_news=[],
        missing_resources=[],
        missing_prices=[],
        evidence=[],
        missing_data=[],
        errors=[],
        warnings=[],
        retry_count=0,
        revision_count=0,
        report_markdown="",
        sources=[],
        verify_failures=[],
        elapsed_ms=0,
    )
