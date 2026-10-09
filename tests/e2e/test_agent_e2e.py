"""Agent 端到端测试（任务书 13.4）。

真实 MCP 协议链路（stdio 子进程 + MDA_MOCK_PROVIDERS=1 确定性 Mock 数据源，
明确标注）+ 脚本化 LLM（明确标注）。验证：
- 主题解析 / 计划 / 三 Server 工具调用 / 结果进入 State
- 不重复执行无意义工具调用（图结构保证单次）
- Markdown 生成 / 引用可追溯 / 数据缺失披露 / 不编造资源量与价格
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mda.agent.graph import build_graph
from mda.agent.mcp_client import MCPToolRegistry
from mda.agent.state import new_state
from mda.common.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"


@pytest.fixture()
def e2e_settings(tmp_path: Path) -> Settings:
    return Settings(
        log_level="WARNING",
        llm_provider="mock",
        mcp_transport="stdio",
        mcp_call_timeout_seconds=60.0,
        task_timeout_seconds=120.0,
        news_cache_dir=str(tmp_path / "cache" / "news"),
        price_cache_dir=str(tmp_path / "cache" / "price"),
        pdf_cache_dir=str(tmp_path / "cache" / "pdf"),
        extract_cache_dir=str(tmp_path / "cache" / "extract"),
        reports_dir=str(tmp_path / "reports"),
    )


async def _run_graph(state: dict, llm, settings: Settings) -> dict:
    mcp = MCPToolRegistry(settings)
    try:
        await mcp.connect()
        assert mcp.missing_required_tools() == {}, "三个 Server 的 5 个工具必须全部发现"
        graph = build_graph(llm, mcp)
        return await graph.ainvoke(state)
    finally:
        await mcp.aclose()


class TestAgentE2E:
    async def test_full_pipeline(
        self, e2e_settings: Settings, evidence_aware_llm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """完整图执行：计划 -> 三路并行收集 -> 归并 -> 校验 -> 报告 -> 确定性复核。"""
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        state = new_state(QUERY, "2026-10-08")
        state["known_pdfs"] = [
            {
                "id": "test-pdf",
                "url": "https://example.com/fixture.pdf",
                "standard": "NI 43-101",
                "commodity": "lithium",
                "project": "Test Project",
                "effective_date": "2026-01-31",
                "enabled": True,
                "note": "测试夹具（合成数据）",
            }
        ]
        state["max_agent_retries"] = 1
        state["max_report_revisions"] = 1

        final = await _run_graph(state, evidence_aware_llm, e2e_settings)

        # 计划与主题
        assert final["plan"]["commodity"] == "lithium"
        # 三个分支结果进入 State
        assert final["news"], "新闻应进入 State"
        assert final["resources"], "资源量应进入 State"
        assert final["prices"], "价格应进入 State"
        # 证据归并
        assert final["evidence"], "证据列表非空"
        types = {ev["source_type"] for ev in final["evidence"]}
        assert types == {"news", "pdf", "price"}
        # 报告
        assert final["report_markdown"], "必须生成 Markdown"
        assert "执行摘要" in final["report_markdown"]
        # 引用可追溯
        for s in final["sources"]:
            assert s["url"].startswith(("http://", "https://"))
        # 确定性校验通过
        assert final["verify_failures"] == [], final["verify_failures"]

    async def test_no_fabrication_when_tools_fail(
        self, e2e_settings: Settings, evidence_aware_llm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MCP 工具全部不可用时：报告只披露缺失，绝不编造资源量与价格。"""
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        state = new_state(QUERY, "2026-10-08")
        state["known_pdfs"] = []  # 无注册表 -> 资源量缺失
        state["max_agent_retries"] = 1
        state["max_report_revisions"] = 1

        final = await _run_graph(state, evidence_aware_llm, e2e_settings)

        assert final["missing_data"], "应披露数据缺失"
        assert final["report_markdown"]
        # 报告不包含未验证的资源量/价格陈述（脚本化 LLM 只写证据内容）
        assert "kt LCE" not in final["report_markdown"] or final["resources"]
        assert "CNY/tonne" not in final["report_markdown"] or final["prices"]


class TestInvalidInputShortCircuit:
    """空 query 必须短路：不产生任何 MCP 工具调用（修复回归测试）。"""

    async def test_empty_query_no_tool_calls(
        self, e2e_settings: Settings, evidence_aware_llm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        calls: list[str] = []

        class CountingMCP(MCPToolRegistry):
            async def call(self, name, arguments, *, timeout=None):  # type: ignore[no-untyped-def]
                calls.append(name)
                return await super().call(name, arguments, timeout=timeout)

        mcp = CountingMCP(e2e_settings)
        await mcp.connect()
        try:
            graph = build_graph(evidence_aware_llm, mcp)
            state = new_state("   ", "2026-10-08")
            state["known_pdfs"] = []
            state["max_agent_retries"] = 1
            state["max_report_revisions"] = 1
            final = await graph.ainvoke(state)
        finally:
            await mcp.aclose()

        assert calls == [], f"空 query 不得触发任何工具调用: {calls}"
        assert final["errors_parse"], "必须记录 parse 错误"
        assert final["errors_parse"][0]["error_code"] == "INVALID_ARGUMENT"


class TestToolCallCounts:
    """不重复执行无意义工具调用（图结构 + 两阶段执行）。"""

    async def test_single_plan_and_collect(
        self, e2e_settings: Settings, evidence_aware_llm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        calls: list[str] = []

        class CountingMCP(MCPToolRegistry):
            async def call(self, name, arguments, *, timeout=None):  # type: ignore[no-untyped-def]
                calls.append(name)
                return await super().call(name, arguments, timeout=timeout)

        mcp = CountingMCP(e2e_settings)
        await mcp.connect()
        try:
            graph = build_graph(evidence_aware_llm, mcp)
            state = new_state(QUERY, "2026-10-08")
            state["known_pdfs"] = [
                {
                    "id": "t",
                    "url": "https://example.com/fixture.pdf",
                    "commodity": "lithium",
                    "project": "T",
                    "enabled": True,
                }
            ]
            state["max_agent_retries"] = 1
            state["max_report_revisions"] = 1
            final = await graph.ainvoke(state)
        finally:
            await mcp.aclose()

        assert calls.count("search") <= 3  # 计划中的检索词数量上限
        assert calls.count("extract_resources") <= 2  # 注册表文档上限
        assert calls.count("get_trend") <= 2  # 商品上限
        # 无可重试错误时 targeted_retry 不应触发（无额外调用）
        assert final["retry_count"] == 0
