"""mineral-pdf-mcp Server 入口。

工具：
- extract_resources(pdf_url)：NI 43-101 技术报告资源量抽取（任务书 5.1）

启动：
- stdio:            python -m mda.servers.mineral_pdf.server
- streamable-http:  python -m mda.servers.mineral_pdf.server --transport streamable-http --port 8002
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mda.common.logging import get_logger
from mda.common.settings import get_settings
from mda.servers.mineral_pdf.extractor import ExtractionService
from mda.servers.server_common import (
    build_arg_parser,
    init_server_logging,
    make_tool_json,
    mock_enabled,
    run_mcp,
)

log = get_logger(__name__)

EXTRACT_DESCRIPTION = (
    "解析 NI 43-101 技术报告 PDF（输入为 http/https 的 PDF URL），抽取 "
    "Mineral Resource Estimate 资源量表格：Measured/Indicated/Inferred 资源量"
    "与 Proven/Probable 储量（绝不混用）。每条记录包含矿石量、品位、所含金属量"
    "（带单位）、页码证据与表格原文。找不到表格、关键字段缺失、单位不明确或"
    "数据矛盾时返回 partial/error 与 validation_warnings、needs_review 标记，"
    "绝不猜测资源量。"
)


def build_server() -> FastMCP:
    mcp = FastMCP("mineral-pdf")
    service = ExtractionService(get_settings(), mock=mock_enabled())

    @mcp.tool(name="extract_resources", description=EXTRACT_DESCRIPTION)
    async def extract_resources(pdf_url: str) -> str:
        result = await service.extract_resources(pdf_url)
        log.info(
            "tool_called",
            tool_name="extract_resources",
            server_name="mineral-pdf",
            status=result.status,
            error_code=result.error.code.value if result.error else None,
            source=result.metadata.source,
        )
        return make_tool_json(result)

    return mcp


def main() -> None:
    init_server_logging()
    args = build_arg_parser("mineral-pdf", default_port=8002).parse_args()
    run_mcp(build_server(), args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
