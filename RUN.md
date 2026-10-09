# RUN.md — 一键启动与验收指南

目标：**5 分钟内**依据本文档启动并执行示例请求。
首次构建镜像 / 下载依赖不计入启动耗时（如实说明）。

## 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | Desktop 或 Engine + Compose v2 | 方式 B 需要 |
| Python | 3.10+（本地开发 3.12 验证；Docker 内 3.11） | 方式 A 需要 |
| LLM 密钥 | 任意 OpenAI 兼容端点（默认 DashScope/qwen） | `/report` 需要；`/health*` 不需要 |

## 方式 A：本地运行（stdio MCP，最快）

```powershell
cd mining-daily-agent
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -e .[dev]
Copy-Item .env.example .env        # 填入 LLM_API_KEY（或直接设置环境变量）
```

执行示例请求（CLI 独立于 API）：

```powershell
python -m mda.agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报" --date 2026-10-08 --out reports/demo.md
```

## 方式 B：Docker 一键启动（streamable-http MCP）

```powershell
cd mining-daily-agent
docker compose up -d --build          # 一条命令启动全部 4 个容器
docker compose ps                     # 等待 4 个服务 healthy
```

**容器启动后的自动冒烟测试**（先健康检查，再完整日报）：

```powershell
# 冒烟 1：健康与依赖（含三个 MCP Server 连接性 + 工具清单）
docker compose exec agent-api python scripts/smoke_test.py --base http://agent-api:8000
# 冒烟 2：完整日报（真实 LLM + 真实数据，约 1-3 分钟）
docker compose exec agent-api python scripts/smoke_test.py --base http://agent-api:8000 --full
```

或从宿主机调用：

```powershell
curl http://localhost:8000/health
curl http://localhost:8000/health/dependencies
curl -X POST http://localhost:8000/report -H "Content-Type: application/json" -d '{"query":"给我生成一份关于 Pilbara 锂矿的今日简报","report_date":"2026-10-08"}'
```

> 注意：`/report` 需要 LLM_API_KEY。将 `.env` 中的 `LLM_API_KEY` 写入后
> `docker compose up -d` 自动注入（env_file 机制见 compose 文件）；
> **密钥绝不写入镜像**（.dockerignore 排除 .env）。

## 方式 C：Claude Desktop / Cursor 接入（stdio）

```powershell
python scripts/generate_mcp_config.py        # 生成本机绝对路径版 mcp-config.json
```

- 仓库内 `mcp-config.json` 为通用版本（`command: python`，依赖 `pip install -e .`）；
  本机使用时建议先运行上面脚本生成绝对路径版本（避免客户端 PATH 差异）。
- Claude Desktop（Windows）：把 `mcp-config.json` 内容合并进
  `%APPDATA%\Claude\claude_desktop_config.json`
- Cursor：放置为 `<项目>/.cursor/mcp.json`
- 两个客户端配置位置与字段不完全相同，按各自文档放置（本配置的
  mcpServers 结构两者兼容）。

**验证工具发现与执行**（MCP Inspector 等效）：

```powershell
python scripts/verify_mcp.py                # Mock 数据源（不依赖网络）
python scripts/verify_mcp.py --real         # 真实数据源
python scripts/verify_mcp.py --transport http  # Docker 形态（服务名 URL）
```

## API 说明

| 端点 | 说明 |
|---|---|
| `GET /health` | API 健康 |
| `GET /health/dependencies` | 三个 MCP Server 连接性 + 工具清单 + LLM 配置（区分"配置存在"与"真实可用"） |
| `POST /report` | `{"query": "...", "report_date": "YYYY-MM-DD"}` → request_id/status/report_markdown/sources/missing_data/warnings/elapsed_ms；整体时限默认 600s（慢网络下两个 PDF 下载+解析+多轮 LLM 的实测预算） |

## 常用验证命令

```powershell
pytest -q                                        # 全量测试（默认 Mock 隔离）
RUN_LIVE_TESTS=1 pytest tests/integration        # 真实数据集成（需网络）
RUN_LLM_TESTS=1 pytest tests/e2e/test_real_llm.py  # 真实 LLM（需密钥）
ruff check . && ruff format --check . && mypy mda  # 质量门禁
docker compose down                              # 停止
```

## 已知前置与限制（如实说明）

1. 首次 `docker compose up --build` 需拉取基础镜像并安装依赖（数分钟），不计入启动耗时。
2. `/report` 依赖有效 LLM 密钥；无密钥时返回 `MODEL_CONFIG_MISSING`（503）。
3. 本开发网络下 Google News / 百度 RSS / LME 官网被阻断（见 DATA_SOURCES.md），
   相关 Provider 默认关闭或返回明确的 `UPSTREAM_UNAVAILABLE`。
4. FRED 月度数据滞后约 3 个月；趋势窗口会显式扩展并警告，日期如实标注。
