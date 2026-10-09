"""MCP 协议验证脚本（MCP Inspector 等效，任务书 2.2/12）。

用官方 MCP SDK 客户端对三个 Server 执行：初始化握手 → 工具发现 →
真实工具调用（Mock 或真实数据）。支持 stdio 与 streamable-http 两种传输。

用法:
    python scripts/verify_mcp.py                       # stdio，Mock 数据源（默认，不依赖网络）
    python scripts/verify_mcp.py --real                # stdio，真实数据源（需网络）
    python scripts/verify_mcp.py --transport http \
        --news http://mining-news-mcp:8001/mcp \
        --pdf  http://mineral-pdf-mcp:8002/mcp \
        --price http://lme-price-mcp:8003/mcp          # Docker 多容器形态
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

SERVERS = {
    "mining-news": "mda.servers.mining_news.server",
    "mineral-pdf": "mda.servers.mineral_pdf.server",
    "lme-price": "mda.servers.lme_price.server",
}
EXPECTED = {
    "mining-news": {"search", "fetch_article"},
    "mineral-pdf": {"extract_resources"},
    "lme-price": {"get_price", "get_trend"},
}
# 每个 Server 的一次真实工具调用
LIVE_CALLS = {
    "mining-news": ("search", {"query": "lithium", "days": 7}),
    "mineral-pdf": (
        "extract_resources",
        {
            "pdf_url": (
                "https://sigmalithiumresources.com/wp-content/uploads/2023/05/"
                "2023-01-SGML-Updated-Technical-Report-1.pdf"
            )
        },
    ),
    "lme-price": ("get_trend", {"commodity": "lithium", "days": 30}),
}


async def check_stdio(server: str, module: str, real: bool) -> bool:
    env = {**os.environ}
    if not real:
        env["MDA_MOCK_PROVIDERS"] = "1"  # 默认 Mock（确定性，不依赖网络）
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", module, "--transport", "stdio"],
        env=env,
    )
    ok = True
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"[{server}] 初始化 OK: {init.serverInfo.name}")
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                missing = EXPECTED[server] - names
                if missing:
                    print(f"[{server}] 工具缺失: {missing}")
                    ok = False
                else:
                    print(f"[{server}] 工具发现 OK: {sorted(names)}")
                tool, args = LIVE_CALLS[server]
                result = await session.call_tool(tool, arguments=args)
                payload = json.loads(result.content[0].text)
                status = payload.get("status")
                print(f"[{server}] 调用 {tool} -> status={status}")
                if status not in ("success", "partial"):
                    print(f"    error={payload.get('error')}")
                    ok = False
                else:
                    data = payload.get("data") or {}
                    keys = list(data.keys())[:6]
                    print(f"    data 字段: {keys}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{server}] 失败: {exc}")
        ok = False
    return ok


async def check_http(server: str, url: str) -> bool:
    from mcp.client.streamable_http import streamablehttp_client

    ok = True
    try:
        async with streamablehttp_client(url, timeout=30) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"[{server}] HTTP 初始化 OK: {init.serverInfo.name}")
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                missing = EXPECTED[server] - names
                if missing:
                    print(f"[{server}] 工具缺失: {missing}")
                    ok = False
                else:
                    print(f"[{server}] 工具发现 OK: {sorted(names)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{server}] HTTP 失败: {exc}")
        ok = False
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=("stdio", "http"))
    parser.add_argument("--real", action="store_true", help="使用真实数据源（默认 Mock）")
    parser.add_argument("--news", default="http://mining-news-mcp:8001/mcp")
    parser.add_argument("--pdf", default="http://mineral-pdf-mcp:8002/mcp")
    parser.add_argument("--price", default="http://lme-price-mcp:8003/mcp")
    args = parser.parse_args()

    results: dict[str, bool] = {}
    if args.transport == "stdio":
        for server, module in SERVERS.items():
            results[server] = await check_stdio(server, module, args.real)
    else:
        urls = {
            "mining-news": args.news,
            "mineral-pdf": args.pdf,
            "lme-price": args.price,
        }
        for server, url in urls.items():
            results[server] = await check_http(server, url)

    print("\n=== 结果 ===")
    for server, ok in results.items():
        print(f"{server}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
