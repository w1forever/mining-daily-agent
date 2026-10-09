"""mining-news-mcp Server 入口。

工具：
- search(query, days)：矿业新闻检索（任务书 4.2）
- fetch_article(url)：新闻正文抓取（任务书 4.3）

启动：
- stdio:          python -m mda.servers.mining_news.server
- streamable-http: python -m mda.servers.mining_news.server --transport streamable-http --port 8001
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mda.common.logging import get_logger
from mda.common.settings import get_settings
from mda.servers.mining_news.service import NewsService
from mda.servers.server_common import (
    build_arg_parser,
    init_server_logging,
    make_tool_json,
    mock_enabled,
    run_mcp,
)

log = get_logger(__name__)

SEARCH_DESCRIPTION = (
    "搜索最近 N 天的矿业新闻。输入查询关键词（支持中英文，如 'Pilbara lithium' 或 '锂矿'）"
    "和回溯天数 days（1~30）。返回文章列表（标题、URL、来源、发布时间、摘要），"
    "无结果时返回空列表并携带明确的无结果状态，绝不编造新闻。"
)
FETCH_DESCRIPTION = (
    "抓取指定新闻文章的正文。输入必须为 http/https URL。返回清洗后的正文、标题、作者、"
    "发布时间与来源 URL（可用于引用）。正文提取失败时返回明确提示，不基于标题臆造细节。"
)


def build_server() -> FastMCP:
    mcp = FastMCP("mining-news")
    service = NewsService(get_settings(), mock=mock_enabled())

    @mcp.tool(name="search", description=SEARCH_DESCRIPTION)
    async def search(query: str, days: int = 7) -> str:
        result = await service.search(query, days)
        log.info(
            "tool_called",
            tool_name="search",
            server_name="mining-news",
            status=result.status,
            error_code=result.error.code.value if result.error else None,
            source=result.metadata.source,
        )
        return make_tool_json(result)

    @mcp.tool(name="fetch_article", description=FETCH_DESCRIPTION)
    async def fetch_article(url: str) -> str:
        result = await service.fetch_article(url)
        log.info(
            "tool_called",
            tool_name="fetch_article",
            server_name="mining-news",
            status=result.status,
            error_code=result.error.code.value if result.error else None,
            source=result.metadata.source,
        )
        return make_tool_json(result)

    return mcp


def main() -> None:
    init_server_logging()
    args = build_arg_parser("mining-news", default_port=8001).parse_args()
    run_mcp(build_server(), args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
