"""Streamable HTTP 传输合同测试（Docker 部署形态）。

在同一进程内以 streamable-http 传输启动 FastMCP Server（真实 SDK 服务端），
用官方 SDK 的 streamablehttp_client 连接（真实 HTTP 协议链路）。
Server 使用 MDA_MOCK_PROVIDERS=1（明确标注）。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mda.servers.mining_news.server import build_server

MOCK_ENV = {**os.environ, "MDA_MOCK_PROVIDERS": "1"}


@pytest.fixture()
async def http_server(monkeypatch: pytest.MonkeyPatch):
    """在后台任务中启动真实 FastMCP streamable-http Server。"""
    for key in list(os.environ):
        if key == "MDA_MOCK_PROVIDERS":
            continue
    monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")

    mcp = build_server()
    port = 18801

    async def run_server() -> None:
        # 直接使用 anyio 运行 uvicorn（mcp.run 是同步阻塞 API，此处手动装配）
        starlette_app = mcp.streamable_http_app()
        import uvicorn

        config = uvicorn.Config(starlette_app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        await server.serve()

    task = asyncio.create_task(run_server())
    # 等待端口就绪
    import socket
    import time

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            await asyncio.sleep(0.2)
    else:  # pragma: no cover
        task.cancel()
        raise RuntimeError("HTTP Server 启动超时")
    yield f"http://127.0.0.1:{port}/mcp"
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task


class TestStreamableHttp:
    async def test_full_session(self, http_server: str) -> None:
        async with streamablehttp_client(http_server) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "mining-news"
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == {"search", "fetch_article"}
                result = await session.call_tool(
                    "search", arguments={"query": "lithium", "days": 7}
                )
                payload = json.loads(result.content[0].text)
                assert payload["status"] in ("success", "partial")
                assert payload["data"]["articles"]

    async def test_call_error_path(self, http_server: str) -> None:
        async with streamablehttp_client(http_server) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("search", arguments={"query": "x", "days": 999})
                payload = json.loads(result.content[0].text)
                assert payload["status"] == "error"
                assert payload["error"]["code"] == "INVALID_ARGUMENT"


class TestConnectionRefused:
    async def test_client_handles_connection_failure(self) -> None:
        """Server 不可用：客户端必须把异常转换为可处理的错误（Agent 层映射）。"""

        with pytest.raises(Exception):
            async with streamablehttp_client("http://127.0.0.1:59999/mcp", timeout=3.0) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
