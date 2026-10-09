"""Agent CLI：不启动 API 也能独立验证 Agent（任务书 9）。

用法:
    python -m mda.agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报" \
        [--date YYYY-MM-DD] [--out reports/report.md] [--transport stdio|http]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mda.agent.runner import run_agent
from mda.common.errors import AppError
from mda.common.logging import setup_logging
from mda.common.settings import Settings


async def _run(query: str, report_date: str, out: str | None, transport: str) -> int:
    settings = Settings(mcp_transport=transport)
    try:
        final = await run_agent(query, report_date, settings)
    except AppError as exc:
        print(f"[错误] {exc.code.value}: {exc.message}")
        return 2
    markdown = final.get("report_markdown", "")
    print(markdown if markdown else "[警告] 报告为空")
    print("\n--- 数据缺失 ---")
    for item in final.get("missing_data", []):
        print(f"  - {item}")
    print("--- 校验失败 ---")
    for item in final.get("verify_failures", []):
        print(f"  - {item}")
    if out and markdown:
        # 写盘为阻塞操作，放入线程池避免 ASYNC240
        await asyncio.to_thread(_write_report, out, markdown)
        print(f"\n[已保存] {out}")
    return 0 if markdown else 1


def _write_report(out: str, markdown: str) -> None:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(markdown, encoding="utf-8")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="矿权日报 Agent CLI")
    parser.add_argument("query", help="用户自然语言问题")
    parser.add_argument("--date", default="", help="报告日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--out", default="", help="保存 Markdown 到文件")
    parser.add_argument("--transport", default=None, choices=("stdio", "http"))
    args = parser.parse_args()
    settings = Settings()
    transport = args.transport or settings.mcp_transport
    code = asyncio.run(_run(args.query, args.date, args.out or None, transport))
    sys.exit(code)


if __name__ == "__main__":
    main()
