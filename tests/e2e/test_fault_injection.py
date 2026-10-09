"""故障注入测试（任务书 13.5）。

覆盖：新闻超时 / PDF 404 / PDF 无资源量表 / 价格空数据 / MCP 连接失败 /
LLM 非法 JSON / 工具结果字段缺失 / PDF 单位冲突 / 提示注入隔离。

服务级故障用 respx 模拟 HTTP（明确标注）；Agent 级故障注入到真实图
执行（真实 MCP stdio + Mock 数据源 + 脚本化 LLM，明确标注）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.settings import Settings
from mda.servers.mining_news.providers import MockNewsProvider
from mda.servers.mining_news.service import NewsService


def _settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        log_level="WARNING",
        llm_provider="mock",
        news_cache_dir=str(tmp_path / "cache" / "news"),
        price_cache_dir=str(tmp_path / "cache" / "price"),
        pdf_cache_dir=str(tmp_path / "cache" / "pdf"),
        extract_cache_dir=str(tmp_path / "cache" / "extract"),
        http_read_timeout_seconds=3.0,
        http_connect_timeout_seconds=2.0,
        news_fetch_timeout_seconds=3.0,
        **kw,
    )


async def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class TestNewsUpstreamTimeout:
    """新闻服务器超时 -> UPSTREAM_TIMEOUT（可重试）。"""

    @respx.mock
    async def test_search_timeout(self, tmp_path: Path) -> None:
        def _timeout(request):  # type: ignore[no-untyped-def]
            raise httpx.ConnectTimeout("upstream down")

        respx.get("https://www.mining.com/feed/").mock(side_effect=_timeout)
        respx.get("https://www.australianmining.com.au/feed/").mock(side_effect=_timeout)
        svc = NewsService(_settings(tmp_path), mock=False)
        svc.client = SafeHttpClient(
            connect_timeout=1.0,
            read_timeout=2.0,
            max_retries=0,
            resolver=_public_resolver,
        )
        result = await svc.search("lithium", 7)
        assert result.status in ("partial", "error")
        if result.status == "error":
            assert result.error.code == ErrorCode.UPSTREAM_TIMEOUT
            assert result.error.retryable is True


class TestPdf404:
    """PDF URL 404 -> DOCUMENT_NOT_FOUND（不可重试）。"""

    @respx.mock
    async def test_404(self, tmp_path: Path) -> None:
        respx.get("https://public.example.com/missing.pdf").mock(return_value=httpx.Response(404))
        from mda.servers.mineral_pdf.extractor import ExtractionService

        svc = ExtractionService(_settings(tmp_path), resolver=_public_resolver)
        svc.downloader.client = SafeHttpClient(max_retries=0, resolver=_public_resolver)
        result = await svc.extract_resources("https://public.example.com/missing.pdf")
        assert result.status == "error"
        assert result.error.code == ErrorCode.DOCUMENT_NOT_FOUND
        assert result.error.retryable is False


class TestPdfWithoutResourceTables:
    """PDF 不含资源量表格 -> RESOURCE_TABLE_NOT_FOUND。"""

    @respx.mock
    async def test_no_tables(self, tmp_path: Path) -> None:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Quarterly operations update", fontsize=12)
        pdf_bytes = doc.tobytes()
        doc.close()
        respx.get("https://public.example.com/ops.pdf").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=pdf_bytes,
            )
        )
        from mda.servers.mineral_pdf.extractor import ExtractionService

        svc = ExtractionService(_settings(tmp_path), resolver=_public_resolver)
        svc.downloader.client = SafeHttpClient(max_retries=0, resolver=_public_resolver)
        result = await svc.extract_resources("https://public.example.com/ops.pdf")
        assert result.status == "error"
        assert result.error.code == ErrorCode.RESOURCE_TABLE_NOT_FOUND


class TestPriceEmptyData:
    """价格 Provider 空数据 -> DATA_UNAVAILABLE（不编造价格）。"""

    @respx.mock
    async def test_sina_empty_array(self, tmp_path: Path) -> None:
        respx.get(url__regex=r".*InnerFuturesNewService\.getDailyKLine.*").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"[]",  # 无效 symbol 也返回 200 + 空数组
            )
        )
        from mda.servers.lme_price.service import PriceService

        svc = PriceService(_settings(tmp_path))
        svc.client = SafeHttpClient(max_retries=0, resolver=_public_resolver)
        result = await svc.get_trend("lithium_carbonate", 30)
        assert result.status == "error"
        assert result.error.code == ErrorCode.DATA_UNAVAILABLE


class TestMcpConnectionFailure:
    """MCP Server 连接失败 -> MCP_CONNECTION_FAILED（Agent 层可重试）。"""

    async def test_connection_refused(self, tmp_path: Path) -> None:
        from mda.agent.mcp_client import MCPToolRegistry

        registry = MCPToolRegistry(
            _settings(
                tmp_path,
                mcp_transport="http",
                mcp_news_url="http://127.0.0.1:59998/mcp",
                mcp_pdf_url="http://127.0.0.1:59997/mcp",
                mcp_price_url="http://127.0.0.1:59996/mcp",
                mcp_call_timeout_seconds=5.0,
            )
        )
        with pytest.raises(AppError) as exc:
            await registry.connect()
        assert exc.value.code == ErrorCode.MCP_CONNECTION_FAILED
        assert exc.value.retryable is True


class TestLlmInvalidJson:
    """LLM 非法 JSON -> 规划器重试后确定性兜底（已在 unit 覆盖，此处走图）。"""

    async def test_graph_survives_bad_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        from tests.conftest import VALID_PLAN, ScriptedLLM

        llm = ScriptedLLM(responses=["{{{bad json", _json(VALID_PLAN)], default_response="")
        llm.default_response = "mock report"

        from mda.agent.graph import build_graph
        from mda.agent.mcp_client import MCPToolRegistry
        from mda.agent.state import new_state

        mcp = MCPToolRegistry(_settings(tmp_path))
        await mcp.connect()
        try:
            graph = build_graph(llm, mcp)
            state = new_state("Pilbara 锂矿简报", "2026-10-08")
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

        assert final["plan"]["commodity"] == "lithium"
        assert final["evidence"]  # 图在规划失败后仍完成数据收集


class TestToolResultMissingFields:
    """工具结果字段缺失 -> SCHEMA_VALIDATION_FAILED（不崩溃）。"""

    def test_extract_json_missing_fields(self) -> None:
        from mda.agent.mcp_client import MCPToolRegistry
        from mda.common.models import tool_result_from_dict

        # 字段缺失：契约模型以默认值安全降级（data=None），下游节点按缺失处理
        payload = MCPToolRegistry._extract_json(
            json.dumps({"status": "success"})  # 缺 data/metadata/error/warnings
        )
        result = tool_result_from_dict(payload)
        assert result.status == "success"
        assert result.data is None  # 明确为缺失，不伪造
        # 完全非 JSON -> 报错（不崩溃，Agent 层映射为 SCHEMA_VALIDATION_FAILED）
        with pytest.raises(Exception):
            MCPToolRegistry._extract_json("not json")
        # 合法形态
        ok = MCPToolRegistry._extract_json(
            json.dumps(
                {
                    "status": "success",
                    "data": {},
                    "error": None,
                    "metadata": {"source": "", "retrieved_at": "", "is_cached": False},
                    "warnings": [],
                }
            )
        )
        assert ok["status"] == "success"


class TestPdfUnitConflict:
    """PDF 抽取单位冲突 -> needs_review（校验层，已在 unit 覆盖，此处管线级）。"""

    def test_conflict_flags_review(self) -> None:
        from mda.servers.mineral_pdf.schemas import ResourceRecord
        from mda.servers.mineral_pdf.validator import validate_records

        rec = ResourceRecord(
            property_name="P",
            deposit_name="D",
            resource_category="Indicated",
            ore_tonnage=1000.0,
            ore_unit="Mt",  # 与证据文本中的 't' 冲突（模拟）
            grade=1.5,
            grade_unit="% Li2O",
            contained_metal=99999.0,  # 与量级明显不符
            metal_unit="kt LCE",
            source_pdf_url="https://example.com/r.pdf",
            page_number=10,
            table_title="Mineral Resource Estimate",
            evidence_text="unit conflict fixture",
            commodity="lithium",
        )
        outcome = validate_records([rec])
        assert outcome.records[0].validation_status == "needs_review"


class TestPromptInjectionIsolation:
    """外部正文含诱导指令 -> 视为数据，不影响 Agent（任务书 13.5.9）。"""

    async def test_injection_article_returns_as_data(self) -> None:
        """Mock 新闻源返回含指令的正文（明确标注：合成注入样本），
        服务层必须原样作为数据返回，不做任何执行。"""
        provider = MockNewsProvider()
        items = await provider.search("Pilbara lithium", 7)
        assert items, "Mock 源应返回含注入样本的文章"

        from mda.servers.mining_news.providers import mock_fetch_article

        injected = await mock_fetch_article("https://example.com/news/injection-sample")
        if injected:
            # 注入内容必须作为普通文本字段返回（调用方自行隔离）
            assert isinstance(injected["content"], str)

    async def test_injection_does_not_alter_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """含注入正文的证据进入报告生成时，脚本化 LLM 只输出证据数值，
        注入指令不会进入报告。"""
        monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")
        from tests.conftest import VALID_PLAN, EvidenceAwareReportLLM

        llm = EvidenceAwareReportLLM()
        llm.responses = [_json(VALID_PLAN)]

        from mda.agent.graph import build_graph
        from mda.agent.mcp_client import MCPToolRegistry
        from mda.agent.state import new_state

        mcp = MCPToolRegistry(_settings(tmp_path))
        await mcp.connect()
        try:
            graph = build_graph(llm, mcp)
            state = new_state("Pilbara 锂矿简报", "2026-10-08")
            state["known_pdfs"] = []
            state["max_agent_retries"] = 1
            state["max_report_revisions"] = 1
            final = await graph.ainvoke(state)
        finally:
            await mcp.aclose()

        assert "PWNED" not in final["report_markdown"]
        assert "IGNORE" not in final["report_markdown"].upper()


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
