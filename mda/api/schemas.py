"""API 请求/响应 Schema（任务书 9）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReportRequest(BaseModel):
    query: str = Field(..., max_length=500, description="用户自然语言问题")
    report_date: str = Field("", description="报告日期 YYYY-MM-DD；空则由请求时刻决定（不硬编码）")


class DependencyStatus(BaseModel):
    server: str
    connected: bool
    tools: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    error: str = ""


class HealthDependenciesResponse(BaseModel):
    status: str  # ok | degraded | down
    mcp_servers: list[DependencyStatus] = Field(default_factory=list)
    llm_configured: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    note: str = ""


class ReportResponse(BaseModel):
    request_id: str
    status: str  # success | partial | error
    report_markdown: str = ""
    sources: list[dict] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    error: dict | None = None
