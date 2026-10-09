"""FastAPI 入口（任务书 9）。

- GET  /health             API 自身健康
- GET  /health/dependencies 三个 MCP Server 连接性 + 工具清单 + LLM 配置
- POST /report            同步执行 Agent 任务（长耗时由整体时限约束）
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mda.agent.mcp_client import MCPToolRegistry
from mda.agent.runner import run_agent
from mda.api.schemas import (
    DependencyStatus,
    HealthDependenciesResponse,
    ReportRequest,
    ReportResponse,
)
from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger, setup_logging
from mda.common.settings import Settings, get_settings

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(title="矿权日报 Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "mining-daily-agent-api"}

    @app.get("/health/dependencies", response_model=HealthDependenciesResponse)
    async def health_dependencies() -> HealthDependenciesResponse:
        mcp = MCPToolRegistry(settings)
        statuses: list[DependencyStatus] = []
        all_ok = True
        try:
            await mcp.connect()
        except AppError as exc:
            return HealthDependenciesResponse(
                status="down",
                mcp_servers=[
                    DependencyStatus(server=s, connected=False, error=exc.message)
                    for s in ("mining-news", "mineral-pdf", "lme-price")
                ],
                llm_configured=settings.llm_configured,
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
                note="MCP 连接失败",
            )
        try:
            missing = mcp.missing_required_tools()
            for server in ("mining-news", "mineral-pdf", "lme-price"):
                tools = sorted(mcp._server_tools.get(server, []))
                miss = missing.get(server, [])
                connected = server in mcp._server_tools or not miss
                if miss:
                    all_ok = False
                statuses.append(
                    DependencyStatus(
                        server=server,
                        connected=connected,
                        tools=tools,
                        missing_tools=miss,
                    )
                )
            return HealthDependenciesResponse(
                status="ok" if all_ok else "degraded",
                mcp_servers=statuses,
                llm_configured=settings.llm_configured,
                llm_provider=settings.llm_provider,
                llm_model=settings.llm_model,
                note=(
                    "LLM 已配置"
                    if settings.llm_configured
                    else "LLM_API_KEY 未配置（/report 将返回 MODEL_CONFIG_MISSING）"
                ),
            )
        finally:
            await mcp.aclose()

    @app.post("/report", response_model=ReportResponse)
    async def report(req: ReportRequest) -> JSONResponse:
        request_id = uuid.uuid4().hex[:12]
        if not settings.llm_configured:
            return JSONResponse(
                status_code=503,
                content=ReportResponse(
                    request_id=request_id,
                    status="error",
                    error={
                        "code": ErrorCode.MODEL_CONFIG_MISSING.value,
                        "message": "未配置 LLM_API_KEY，无法生成报告",
                        "retryable": False,
                    },
                    elapsed_ms=0,
                ).model_dump(),
            )
        try:
            final = await run_agent(
                req.query,
                req.report_date,
                settings,
                request_id=request_id,
            )
        except AppError as exc:
            return JSONResponse(
                status_code=503,
                content=ReportResponse(
                    request_id=request_id,
                    status="error",
                    error={
                        "code": exc.code.value,
                        "message": exc.message,
                        "retryable": exc.retryable,
                    },
                    elapsed_ms=0,
                ).model_dump(),
            )
        # 输入非法（空 query 等）：图已短路，返回 400 而非"部分报告"
        parse_errors = [
            e
            for e in final.get("errors", [])
            if e.get("tool") == "parse_request"
            and e.get("error_code") == ErrorCode.INVALID_ARGUMENT.value
        ]
        if parse_errors:
            return JSONResponse(
                status_code=400,
                content=ReportResponse(
                    request_id=request_id,
                    status="error",
                    error={
                        "code": ErrorCode.INVALID_ARGUMENT.value,
                        "message": parse_errors[0]["message"],
                        "retryable": False,
                    },
                    elapsed_ms=final.get("elapsed_ms", 0),
                ).model_dump(),
            )
        markdown = final.get("report_markdown", "")
        status = "success"
        if final.get("verify_failures") or not markdown:
            status = "partial"
        warnings = list(final.get("warnings", []))
        if final.get("verify_failures"):
            warnings.append(
                "报告确定性校验失败项（需人工核查）: " + "; ".join(final["verify_failures"])
            )
        # 保存报告（可挂载持久化）
        try:
            reports_dir = settings.resolved_dir(settings.reports_dir)
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{request_id}.md").write_text(
                markdown or "（报告生成失败）", encoding="utf-8"
            )
        except OSError as exc:  # noqa: BLE001
            log.warning("report_save_failed", reason=str(exc))
        resp = ReportResponse(
            request_id=request_id,
            status=status,
            report_markdown=markdown,
            sources=final.get("sources", []),
            missing_data=final.get("missing_data", []),
            warnings=warnings,
            elapsed_ms=final.get("elapsed_ms", 0),
        )
        log.info(
            "report_api_done",
            request_id=request_id,
            status=status,
            elapsed_ms=resp.elapsed_ms,
        )
        return JSONResponse(content=resp.model_dump())

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("mda.api.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
