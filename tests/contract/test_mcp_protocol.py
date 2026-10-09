"""MCP 协议合同测试（任务书 13.2）。

通过官方 MCP SDK 客户端与真实 stdio 子进程建立 MCP 会话，验证：
初始化握手 / 工具发现 / 工具 Schema / 正常调用 / 错误调用 / 超时处理。

Server 子进程使用 MDA_MOCK_PROVIDERS=1（明确标注：确定性 Mock Provider），
隔离外部网络；协议链路（stdio JSON-RPC）是真实建立的，不是函数直调。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SERVER_MODULES = {
    "mining-news": "mda.servers.mining_news.server",
    "mineral-pdf": "mda.servers.mineral_pdf.server",
    "lme-price": "mda.servers.lme_price.server",
}

EXPECTED_TOOLS = {
    "mining-news": {"search", "fetch_article"},
    "mineral-pdf": {"extract_resources"},
    "lme-price": {"get_price", "get_trend"},
}

REQUIRED_ARGS = {
    "search": {"query", "days"},
    "fetch_article": {"url"},
    "extract_resources": {"pdf_url"},
    "get_price": {"commodity", "date"},
    "get_trend": {"commodity", "days"},
}


def _server_params(name: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULES[name], "--transport", "stdio"],
        env={**os.environ, "MDA_MOCK_PROVIDERS": "1"},
        cwd=str(PROJECT_ROOT),
    )


class TestInitializeAndListTools:
    """初始化握手 + 工具发现（三个 Server）。"""

    @pytest.mark.parametrize("server", sorted(SERVER_MODULES))
    async def test_initialize_and_tools(self, server: str) -> None:
        params = _server_params(server)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == server
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == EXPECTED_TOOLS[server], f"工具清单不符: {names}"

    @pytest.mark.parametrize("server", sorted(SERVER_MODULES))
    async def test_tool_schemas(self, server: str) -> None:
        params = _server_params(server)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                for tool in tools.tools:
                    schema = tool.inputSchema
                    assert schema.get("type") == "object"
                    properties = schema.get("properties", {})
                    required = set(schema.get("required", []))
                    for arg in REQUIRED_ARGS[tool.name]:
                        assert arg in properties, (
                            f"{tool.name} 缺少参数 {arg}（required={required}）"
                        )
                    for arg in required:
                        assert arg in properties


class TestToolCalls:
    """正常/错误工具调用（Mock 数据源，明确标注）。"""

    async def _call(self, server: str, tool: str, args: dict):
        params = _server_params(server)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, arguments=args)

    async def test_search_success(self) -> None:
        result = await self._call("mining-news", "search", {"query": "Pilbara lithium", "days": 7})
        assert result.isError is False
        payload = json.loads(result.content[0].text)
        assert payload["status"] in ("success", "partial")
        articles = payload["data"]["articles"]
        assert isinstance(articles, list)
        for art in articles:
            assert art["url"].startswith("http")
            assert art["title"]

    async def test_search_invalid_days(self) -> None:
        result = await self._call("mining-news", "search", {"query": "lithium", "days": 999})
        assert result.isError is False  # 业务错误经统一契约返回，而非协议错误
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "INVALID_ARGUMENT"

    async def test_fetch_article_mock(self) -> None:
        result = await self._call(
            "mining-news",
            "fetch_article",
            {"url": "https://example.com/news/pilbara-expansion"},
        )
        payload = json.loads(result.content[0].text)
        assert payload["status"] in ("success", "partial")
        assert payload["data"]["content"]

    async def test_fetch_article_ssrf_blocked(self) -> None:
        """内网 URL 必须在 Server 侧被拒（SSRF 防护）。"""
        result = await self._call("mining-news", "fetch_article", {"url": "http://127.0.0.1/admin"})
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "INVALID_URL"

    async def test_extract_resources_mock(self) -> None:
        result = await self._call(
            "mineral-pdf",
            "extract_resources",
            {"pdf_url": "https://example.com/fixture.pdf"},
        )
        payload = json.loads(result.content[0].text)
        assert payload["status"] in ("success", "partial")
        records = payload["data"]["records"]
        assert len(records) == 4  # 合成 PDF：Measured/Indicated/合计/Inferred
        cats = {r["resource_category"] for r in records}
        assert "Measured" in cats and "Inferred" in cats

    async def test_get_trend_mock(self) -> None:
        result = await self._call("lme-price", "get_trend", {"commodity": "lithium", "days": 30})
        payload = json.loads(result.content[0].text)
        assert payload["status"] in ("success", "partial")
        trend = payload["data"]
        assert trend["commodity"] == "lithium_carbonate"
        assert trend["unit"] == "CNY/tonne"
        assert len(trend["data_points"]) >= 2
        assert trend["change_percent"] is not None

    async def test_get_price_mock(self) -> None:
        from datetime import date, timedelta

        target = (date.today() - timedelta(days=3)).isoformat()
        result = await self._call("lme-price", "get_price", {"commodity": "copper", "date": target})
        payload = json.loads(result.content[0].text)
        assert payload["status"] in ("success", "partial")
        snap = payload["data"]
        assert snap["price"] is not None
        assert snap["currency"] == "USD"
        assert snap["unit"] == "USD/metric tonne"

    async def test_unsupported_commodity(self) -> None:
        result = await self._call("lme-price", "get_trend", {"commodity": "gold", "days": 30})
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "error"
        assert payload["error"]["code"] in (
            "DATA_UNAVAILABLE",
            "UNSUPPORTED_COMMODITY",
        )

    async def test_unknown_tool_rejected(self) -> None:
        """调用未注册工具 -> MCP 层返回 isError=True（协议级错误响应）。"""
        result = await self._call("mining-news", "not_a_tool", {})
        assert result.isError is True


class TestServerUnavailable:
    async def test_timeout_handled(self) -> None:
        """MCP Server 不可用（不存在的模块）-> 会话建立失败，客户端必须处理。"""
        bad = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mda.servers.does_not_exist.server", "--transport", "stdio"],
            env={**os.environ},
            cwd=str(PROJECT_ROOT),
        )
        with pytest.raises(Exception):
            async with stdio_client(bad) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    await session.list_tools()
