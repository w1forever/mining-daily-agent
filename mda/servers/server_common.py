"""三个 MCP Server 的公共入口工具。

- 统一支持 stdio（本地/Claude Desktop）与 streamable-http（Docker 多容器）两种传输。
- MDA_MOCK_PROVIDERS=1 时启用确定性 Mock Provider（仅用于合同/协议测试，
  明确标注，生产默认关闭）。
- 工具统一返回 ToolResult 的 JSON 文本（契约 3.1）。
"""

from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

from mda.common.logging import get_logger, setup_logging
from mda.common.models import ToolResult

log = get_logger(__name__)


def mock_enabled() -> bool:
    return os.environ.get("MDA_MOCK_PROVIDERS", "0").strip() in ("1", "true", "yes")


def make_tool_json(result: ToolResult) -> str:
    """把统一契约结果序列化为 MCP 工具返回值（JSON 文本）。"""
    return result.to_mcp_text()


def build_arg_parser(name: str, default_port: int) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{name} MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="传输方式（stdio 用于本地/Claude Desktop；streamable-http 用于 Docker）",
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - Docker 容器需监听所有接口
    parser.add_argument("--port", type=int, default=default_port)
    return parser


def run_mcp(mcp: FastMCP, transport: str, host: str, port: int) -> None:
    """启动 MCP Server（阻塞运行）。"""
    if transport == "streamable-http":
        mcp.settings.host = host  # noqa: S104 - streamable-http 需在容器内网监听
        mcp.settings.port = port
        # DNS rebinding 防护：默认只允许 localhost Host 头；容器内用
        # 服务名访问，必须显式放行（通过 MDA_ALLOWED_HOSTS 配置）。
        from mcp.server.transport_security import TransportSecuritySettings

        extra_hosts = [
            h.strip() for h in os.environ.get("MDA_ALLOWED_HOSTS", "").split(",") if h.strip()
        ]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "localhost",
                "127.0.0.1",
                "0.0.0.0",  # noqa: S104 - Host 头白名单条目（非绑定地址）
                *extra_hosts,
            ],
        )
        # 默认挂载路径 /mcp 与 mcp-config/docker 约定一致
        log.info("mcp_server_start", transport=transport, host=host, port=port)
        mcp.run(transport="streamable-http")
    else:
        log.info("mcp_server_start", transport=transport)
        mcp.run(transport="stdio")


def init_server_logging() -> None:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
