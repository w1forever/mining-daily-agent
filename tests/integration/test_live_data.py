"""真实外部数据源集成测试（任务书 13.3）。

必须显式设置 RUN_LIVE_TESTS=1 才执行；失败原因如实报告，
绝不将 Mock 结果冒充真实通过。外部不可达/无授权时标记 blocked 并跳过。

当前环境事实（2026-10-08 实测，见 DATA_SOURCES.md）：
- mining.com / australianmining.com.au RSS：可用（需浏览器 UA）
- FRED 基本金属月度序列：可用
- 新浪财经 GFEX 碳酸锂期货日线：可用
- Google News / 百度 RSS / LME 官网：本网络阻断
- 黄金/白银：无免费源（FRED 已下架）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mda.common.settings import Settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="真实外部数据测试需 RUN_LIVE_TESTS=1（默认跳过，避免依赖外部网络）",
    ),
]

SIGMA_PDF_URL = (
    "https://sigmalithiumresources.com/wp-content/uploads/2023/05/"
    "2023-01-SGML-Updated-Technical-Report-1.pdf"
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        log_level="WARNING",
        llm_provider="mock",
        news_cache_dir=str(tmp_path / "cache" / "news"),
        price_cache_dir=str(tmp_path / "cache" / "price"),
        pdf_cache_dir=str(tmp_path / "cache" / "pdf"),
        extract_cache_dir=str(tmp_path / "cache" / "extract"),
    )


class TestLiveNews:
    async def test_real_news_search(self, tmp_path: Path) -> None:
        from mda.servers.mining_news.service import NewsService

        svc = NewsService(_settings(tmp_path), mock=False)
        result = await svc.search("lithium", 7)
        if result.status == "error":
            pytest.skip(f"新闻源不可达（如实报告）: {result.error.message}")
        assert result.status in ("success", "partial")
        articles = result.data.articles
        assert articles, "应有真实新闻结果"
        for art in articles:
            assert art.url.startswith("http")
            assert art.source in ("mining.com", "australianmining.com.au")

    async def test_real_fetch_article(self, tmp_path: Path) -> None:
        from mda.servers.mining_news.service import NewsService

        svc = NewsService(_settings(tmp_path), mock=False)
        url = "https://www.mining.com/uk-scientists-turn-to-biology-to-unlock-cleaner-lithium/"
        result = await svc.fetch_article(url)
        if result.status == "error":
            pytest.skip(f"正文抓取不可达（如实报告）: {result.error.message}")
        assert result.status in ("success", "partial")
        assert result.data.content, "应提取到真实正文"


class TestLivePdf:
    async def test_real_ni43101_extraction(self, tmp_path: Path) -> None:
        from mda.servers.mineral_pdf.extractor import ExtractionService

        svc = ExtractionService(_settings(tmp_path))
        result = await svc.extract_resources(SIGMA_PDF_URL)
        if result.status == "error":
            pytest.skip(f"PDF 源不可达（如实报告）: {result.error.message}")
        assert result.status in ("success", "partial")
        data = result.data
        assert data.report_standard == "NI 43-101"
        assert data.records, "应抽取到真实资源量记录"
        for rec in data.records:
            assert rec.ore_tonnage is not None or rec.contained_metal is not None
            assert rec.page_number > 0


class TestLivePrices:
    async def test_real_lithium_trend(self, tmp_path: Path) -> None:
        from mda.servers.lme_price.service import PriceService

        svc = PriceService(_settings(tmp_path))
        result = await svc.get_trend("lithium_carbonate", 30)
        if result.status == "error":
            pytest.skip(f"锂价源不可达（如实报告）: {result.error.message}")
        assert result.status in ("success", "partial")
        trend = result.data
        assert trend.unit == "CNY/tonne"
        assert trend.data_points
        # 数据必须真实：最后一个数据点日期不应晚于今天
        from datetime import date

        assert trend.data_points[-1].date <= date.today().isoformat()

    async def test_real_copper_trend(self, tmp_path: Path) -> None:
        from mda.servers.lme_price.service import PriceService

        svc = PriceService(_settings(tmp_path))
        result = await svc.get_trend("copper", 90)
        if result.status == "error":
            pytest.skip(f"FRED 不可达（如实报告）: {result.error.message}")
        assert result.status in ("success", "partial")
        assert result.data.unit == "USD/metric tonne"


class TestLiveJointCall:
    """三个 MCP Server 联合真实调用（经 MCP 协议）。"""

    async def test_joint_three_servers(self, tmp_path: Path) -> None:
        from mda.agent.mcp_client import MCPToolRegistry

        settings = _settings(tmp_path)
        settings = settings.model_copy(
            update={
                "mcp_transport": "stdio",
                "mcp_call_timeout_seconds": 120.0,
            }
        )
        mcp = MCPToolRegistry(settings)
        try:
            await mcp.connect()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"MCP 连接失败（如实报告）: {exc}")

        try:
            news = await mcp.call("search", {"query": "lithium", "days": 7})
            pdf = await mcp.call("extract_resources", {"pdf_url": SIGMA_PDF_URL})
            price = await mcp.call("get_trend", {"commodity": "lithium", "days": 30})
        finally:
            await mcp.aclose()

        assert news.status in ("success", "partial"), news.error
        assert pdf.status in ("success", "partial"), pdf.error
        assert price.status in ("success", "partial"), price.error
