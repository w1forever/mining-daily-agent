"""生成 mcp-config.json（Claude Desktop / Cursor 客户端配置）。

用法:
    python scripts/generate_mcp_config.py [--python <解释器路径>]

默认使用当前虚拟环境的 python.exe；生成的配置为 stdio 启动模式。
说明：
- Claude Desktop 配置位置（Windows）: %APPDATA%\\Claude\\claude_desktop_config.json
- Cursor 配置位置: <项目>/.cursor/mcp.json
- 两个客户端的配置位置与部分字段不完全相同（Cursor 用 mcpServers 嵌套），
  请按客户端文档放置；本脚本输出的 core 结构两者兼容。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    config = {
        "mcpServers": {
            "mining-news": {
                "command": str(args.python),
                "args": [
                    "-m",
                    "mda.servers.mining_news.server",
                    "--transport",
                    "stdio",
                ],
                "cwd": str(PROJECT_ROOT),
                "env": {"MDA_MOCK_PROVIDERS": "0"},
            },
            "mineral-pdf": {
                "command": str(args.python),
                "args": [
                    "-m",
                    "mda.servers.mineral_pdf.server",
                    "--transport",
                    "stdio",
                ],
                "cwd": str(PROJECT_ROOT),
                "env": {"MDA_MOCK_PROVIDERS": "0"},
            },
            "lme-price": {
                "command": str(args.python),
                "args": [
                    "-m",
                    "mda.servers.lme_price.server",
                    "--transport",
                    "stdio",
                ],
                "cwd": str(PROJECT_ROOT),
                "env": {"MDA_MOCK_PROVIDERS": "0"},
            },
        }
    }
    out = PROJECT_ROOT / "mcp-config.json"
    out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[已生成] {out}")
    print(f"[Python] {args.python}")
    print("说明：仓库提交版 mcp-config.json 为通用版本（command=python，依赖 pip install -e .）；")
    print("本脚本生成含本机绝对路径的配置，适合直接接入本地 Claude Desktop / Cursor：")
    print("放置位置：Claude Desktop -> %APPDATA%\\Claude\\claude_desktop_config.json")
    print("          Cursor        -> <项目>/.cursor/mcp.json")


if __name__ == "__main__":
    main()
