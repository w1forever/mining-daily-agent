"""MCP 协议探针：用官方 SDK 客户端对 stdio Server 做 initialize/list_tools/call_tool。

用法: python scripts/mcp_probe.py [--transport stdio|streamable-http] [--tool search] [--args '{"query":"lithium","days":7}']

这是 verify_mcp.py 的基础版，供开发期快速验证。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def probe_stdio(server_module: str, tool: str, tool_args: dict) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", server_module, "--transport", "stdio"],
        # SDK 默认只透传白名单环境变量；显式传全量环境以便
        # MDA_MOCK_PROVIDERS / .env 配置在子进程生效。
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[init] server={init.serverInfo.name} v{init.serverInfo.version}")
            tools = await session.list_tools()
            print(f"[tools/list] {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description[:80]}...")
            if tool:
                result = await session.call_tool(tool, arguments=tool_args)
                print(f"[tools/call] {tool} isError={result.isError}")
                for content in result.content:
                    text = getattr(content, "text", str(content))
                    print(text)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="mda.servers.mining_news.server")
    parser.add_argument("--tool", default="")
    parser.add_argument("--args", default="{}")
    args = parser.parse_args()
    await probe_stdio(args.server, args.tool, json.loads(args.args))


if __name__ == "__main__":
    asyncio.run(main())
