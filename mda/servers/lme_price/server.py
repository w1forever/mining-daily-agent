"""lme-price-mcp Server 入口。

工具：
- get_price(commodity, date)：指定商品、指定日期报价（任务书 6.3）
- get_trend(commodity, days)：历史价格走势（任务书 6.4）

启动：
- stdio:            python -m mda.servers.lme_price.server
- streamable-http:  python -m mda.servers.lme_price.server --transport streamable-http --port 8003
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mda.common.logging import get_logger
from mda.common.settings import get_settings
from mda.servers.lme_price.service import PriceService
from mda.servers.server_common import (
    build_arg_parser,
    init_server_logging,
    make_tool_json,
    mock_enabled,
    run_mcp,
)

log = get_logger(__name__)

GET_PRICE_DESCRIPTION = (
    "获取指定矿产品在指定日期（YYYY-MM-DD）的价格。commodity 支持中英文别名："
    "铜/copper、铝/aluminum、镍/nickel、锌/zinc、锡/tin、铅/lead（FRED 全球月度均价，"
    "USD/metric tonne）、碳酸锂/lithium carbonate（GFEX 碳酸锂期货收盘价，CNY/tonne）。"
    "非交易日或数据未发布时返回最近实际报价并显式标记 is_estimated=true，绝不静默冒充。"
    "注意：LME 官方报价与锂产品不是同一报价体系，锂精矿与碳酸锂是不同产品。"
)
GET_TREND_DESCRIPTION = (
    "获取指定矿产品最近 N 天（1~730）的价格走势。返回起止价格、涨跌额、涨跌幅、"
    "完整数据点（同一币种/单位/价格口径）。起点价格为 0 时 change_percent 为 null。"
)


def build_server() -> FastMCP:
    mcp = FastMCP("lme-price")
    service = PriceService(get_settings(), mock=mock_enabled())

    @mcp.tool(name="get_price", description=GET_PRICE_DESCRIPTION)
    async def get_price(commodity: str, date: str) -> str:
        result = await service.get_price(commodity, date)
        log.info(
            "tool_called",
            tool_name="get_price",
            server_name="lme-price",
            status=result.status,
            error_code=result.error.code.value if result.error else None,
            source=result.metadata.source,
        )
        return make_tool_json(result)

    @mcp.tool(name="get_trend", description=GET_TREND_DESCRIPTION)
    async def get_trend(commodity: str, days: int) -> str:
        result = await service.get_trend(commodity, days)
        log.info(
            "tool_called",
            tool_name="get_trend",
            server_name="lme-price",
            status=result.status,
            error_code=result.error.code.value if result.error else None,
            source=result.metadata.source,
        )
        return make_tool_json(result)

    return mcp


def main() -> None:
    init_server_logging()
    args = build_arg_parser("lme-price", default_port=8003).parse_args()
    run_mcp(build_server(), args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
