"""Agent 侧 MCP Client（任务书 2.2/7.4）。

- 使用 langchain-mcp-adapters 的 MultiServerMCPClient（已核对 0.3.2 安装版 API）。
- 支持 http（Docker 多容器）与 stdio（本地）两种传输，由 MCP_TRANSPORT 决定。
- Agent 通过 MCP 协议调用工具，绝不直接 import 业务工具函数。
- 工具调用限时；连接失败映射为 MCP_CONNECTION_FAILED。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger
from mda.common.models import ToolResult, tool_result_from_dict
from mda.common.settings import Settings

log = get_logger(__name__)

SERVER_MODULES = {
    "mining-news": "mda.servers.mining_news.server",
    "mineral-pdf": "mda.servers.mineral_pdf.server",
    "lme-price": "mda.servers.lme_price.server",
}

REQUIRED_TOOLS = {
    "mining-news": {"search", "fetch_article"},
    "mineral-pdf": {"extract_resources"},
    "lme-price": {"get_price", "get_trend"},
}


class MCPToolRegistry:
    """三个 MCP Server 的工具注册表（真协议客户端）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._client: MultiServerMCPClient | None = None
        self._tools: dict[str, BaseTool] = {}
        self._server_tools: dict[str, list[str]] = {}

    def _connections(self) -> dict[str, Any]:
        if self.settings.mcp_transport == "stdio":
            env = {**os.environ}
            connections = {}
            for name, module in SERVER_MODULES.items():
                connections[name] = {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", module, "--transport", "stdio"],
                    "env": env,
                    "cwd": str(os.getcwd()),
                }
            return connections
        return {
            "mining-news": {
                "transport": "streamable_http",
                "url": self.settings.mcp_news_url,
                "timeout": self.settings.mcp_call_timeout_seconds,
                "sse_read_timeout": self.settings.mcp_call_timeout_seconds,
            },
            "mineral-pdf": {
                "transport": "streamable_http",
                "url": self.settings.mcp_pdf_url,
                "timeout": self.settings.mcp_call_timeout_seconds,
                "sse_read_timeout": self.settings.mcp_call_timeout_seconds,
            },
            "lme-price": {
                "transport": "streamable_http",
                "url": self.settings.mcp_price_url,
                "timeout": self.settings.mcp_call_timeout_seconds,
                "sse_read_timeout": self.settings.mcp_call_timeout_seconds,
            },
        }

    async def connect(self) -> None:
        """建立到三个 MCP Server 的会话并发现工具。"""
        if self._client is not None:
            return
        try:
            self._client = MultiServerMCPClient(self._connections())
            tools = await asyncio.wait_for(
                self._client.get_tools(),
                timeout=self.settings.mcp_call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AppError(
                ErrorCode.MCP_CONNECTION_FAILED,
                "MCP 工具发现超时",
                retryable=True,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.MCP_CONNECTION_FAILED,
                f"MCP 连接失败: {exc}",
                retryable=True,
            ) from exc
        for tool in tools:
            self._tools[tool.name] = tool
            for server, expected in REQUIRED_TOOLS.items():
                if tool.name in expected:
                    self._server_tools.setdefault(server, []).append(tool.name)
        log.info(
            "mcp_tools_discovered",
            tools=list(self._tools),
            servers={s: sorted(t) for s, t in self._server_tools.items()},
        )

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                self._client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001, S110 - 关闭时尽力而为
                pass
            self._client = None
            self._tools = {}

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def missing_required_tools(self) -> dict[str, list[str]]:
        missing: dict[str, list[str]] = {}
        for server, expected in REQUIRED_TOOLS.items():
            have = set(self._server_tools.get(server, []))
            diff = sorted(expected - have)
            if diff:
                missing[server] = diff
        return missing

    async def call(self, name: str, arguments: dict, *, timeout: float | None = None) -> ToolResult:
        """通过 MCP 协议调用工具，返回统一契约结果。"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT,
                f"未知工具: {name}",
                retryable=False,
            )
        t = timeout or self.settings.mcp_call_timeout_seconds
        try:
            raw = await asyncio.wait_for(tool.ainvoke(arguments), timeout=t)
        except asyncio.TimeoutError:
            return ToolResult.fail(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"MCP 工具调用超时: {name}",
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 - 会话中断/连接失败
            return ToolResult.fail(
                ErrorCode.MCP_CONNECTION_FAILED,
                f"MCP 工具调用失败: {name}: {exc}",
                retryable=True,
            )
        try:
            payload = self._extract_json(raw)
            return tool_result_from_dict(payload)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                f"工具结果不符合统一契约: {name}: {exc}",
                retryable=False,
            )

    @staticmethod
    def _extract_json(raw: Any) -> dict:
        """兼容 langchain-mcp-adapters 0.3.x 的多种返回形态：
        (content, artifact) 元组 / 字符串 / 文本块列表 / dict。"""
        if isinstance(raw, tuple):
            raw = raw[0]
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, str):
                text = first
            elif isinstance(first, dict) and first.get("type") == "text":
                text = str(first.get("text", ""))
            else:
                text = str(raw)
        elif isinstance(raw, dict):
            if raw.get("type") == "text":
                return json.loads(str(raw.get("text", "")))
            return raw
        else:
            text = str(raw)
        text = text.strip()
        if text.startswith("﻿"):
            text = text[1:]
        return json.loads(text)
