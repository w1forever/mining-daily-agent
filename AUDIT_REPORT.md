# AUDIT_REPORT.md — 严格代码审查与功能验证报告

审计时间：2026-10-09
审计方式：以真实代码、实际 MCP 协议交互、运行输出与自动化测试结果为准；
不依据 README 描述认定实现。审计中发现的问题已全部修复并复测。

## 0. 结论

**PASS**

3 个独立 MCP Server、5 个工具、真实 MCP 协议（stdio + Streamable HTTP）、
LangGraph 编排、真实业务数据、真实 LLM 端到端，均经实际执行验证通过。
审计发现的 8 个缺陷已全部修复并由回归测试锁定；剩余限制如实披露
（见 §6），无未执行的验证被标记为 PASS。

---

## 1. 问题清单（按严重程度排序）

### 中（已修复）

| # | 严重度 | 问题 | 位置 | 根因 | 修复 | 回归验证 |
|---|---|---|---|---|---|---|
| M1 | 中 | Google/Baidu News Provider 构造 RawItem 未设置相关性分数，启用后所有结果被服务层 `score<=0` 过滤丢弃 | `mda/servers/mining_news/providers.py:216,259` | 服务层新增 score 过滤时未同步这两个 Provider | 两个 Provider 均按标题/摘要计算 `relevance_score` | 新增 respx 回归测试 `test_google_provider_sets_score` ✅ |
| M2 | 中 | 容器内完整日报超时：TASK_TIMEOUT_SECONDS=300 在慢网络下不足 | `mda/common/settings.py`、`docker-compose.yml` | Novador PDF 下载实测 85.4s（网络慢）+ 双 PDF 解析 + 多轮 LLM > 300s | 预算提升至 600s（.env.example/compose/RUN.md 同步） | 容器完整冒烟 460s SMOKE PASS ✅ |
| M3 | 中 | 报告校验器不支持矿业单位前缀换算：LLM 把 25,081,000 t 转述为 "25.081 Mt" 被误报为"数值未见于证据" | `mda/agent/verify.py` | 数值匹配仅支持 0.5% 相对容差与四舍五入 | 增加 10^±3/±6 倍换算匹配（t/Mt/kt 惯例） | 单元测试全过 + 本地 CLI 校验零失败 ✅ |

### 低（已修复）

| # | 严重度 | 问题 | 位置 | 根因 | 修复 | 回归验证 |
|---|---|---|---|---|---|---|
| L1 | 低 | 空 query 无短路：图继续执行全流程，且 parse 错误被 merge 覆盖；API 返回 partial 空报告而非 400 | `mda/agent/graph.py`、`mda/agent/nodes/validate.py`、`mda/api/main.py` | parse 错误写入共享 errors 通道被 merge 覆盖；无条件边 | 独立 `errors_parse` 通道 + `route_after_parse` 短路 END + runner 合并 + API 400 | 新增 e2e 测试（断言零工具调用）+ API 合同测试 + 容器内 HTTP 400 实测 ✅ |
| L2 | 低 | validate 节点覆盖 parse 节点写入的 warnings（如"问题超长已截断"丢失） | `mda/agent/nodes/validate.py:240` | 通道整体覆盖而非合并 | 与既有 warnings 合并去重 | 全量测试通过 ✅ |
| L3 | 低 | 新闻检索缓存 TTL 24h，对"今日简报"场景可能全天返回同一批旧闻 | `mda/servers/mining_news/service.py` | 与正文缓存共用 TTL | 检索独立 30min TTL（is_cached 标记保留） | 全量测试通过 ✅ |
| L4 | 低 | 英文月份名译作"N月"被误报（证据 "September" → 报告 "9月"） | `mda/agent/verify.py` | 证据侧只做数字字面量扫描 | 月份名 → 月份数字映射加入证据数值集 | 单元测试 + CLI 实测 ✅ |

### 已核实的既往修复（本轮复测锁定，未回退）

- **SSRF 重定向绕过**（上轮被测试抓出并修复）：重定向逐跳 `validate_url_async`（`http_client.py`），本轮 `test_redirect_revalidated` 复测通过。
- **Mock 数据污染生产缓存**：Mock 模式禁用缓存读写，本轮复测确认。
- **stdio 日志污染 JSON-RPC 通道**：日志走 stderr，本轮协议测试复测确认。

---

## 2. 审计核验表（逐项实测）

### 2.1 MCP 协议真实性 ✅

| 核验项 | 方法 | 结果 |
|---|---|---|
| 3 个独立 MCP Server | 独立进程/独立容器（stdio 子进程 ×3 / 4 容器） | ✅ `mda/servers/{mining_news,mineral_pdf,lme_price}/server.py` |
| 5 个指定工具 | `tools/list` 实测 | ✅ search/fetch_article/extract_resources/get_price/get_trend |
| 真实协议（非函数冒充） | Agent 层无任何 `mda.servers.*` 业务函数导入（仅静态别名表）；调用经 `MultiServerMCPClient` → JSON-RPC | ✅ Grep 审计 + 调用日志（ListToolsRequest/CallToolRequest） |
| tools/list + tools/call | SDK 客户端真实会话 | ✅ 19 个合同测试 + verify_mcp.py Mock/真实双模式 6 项 PASS |
| stdio 配置 | `python -m mda.servers.*.server --transport stdio` | ✅ 本地/Claude Desktop 形态实测 |
| Streamable HTTP 配置 | FastMCP streamable-http + 端口 8001-8003 | ✅ 容器内依赖检查 connected=true |
| LangGraph 经 MCP Client 调用 | 图节点只调 `mcp.call()`（真协议包装） | ✅ |

### 2.2 Agent 编排真实性 ✅

| 核验项 | 结果 |
|---|---|
| LangGraph StateGraph | ✅ `graph.py` StateGraph(AgentState) + 条件边；13 个 e2e 测试实测图执行 |
| Planner 结构化输出 | ✅ Pydantic PlanModel + `with_structured_output`；真实 LLM 测试验证 |
| 工具路由与执行节点 | ✅ 计划驱动三路并行 collect 节点（非 LLM 自由路由，规避幻觉路由） |
| 并行 State 冲突 | ✅ 各节点独占通道（news/errors_news/missing_news…），merge 统一归并 |
| MCP 异常传播 | ✅ 工具错误→统一契约→errors 通道→targeted_retry（可重试）/missing（不可重试）；连接失败→MCP_CONNECTION_FAILED |
| 无限循环防护 | ✅ 补检索 ≤1 次（retry_count 守卫）、报告修订 ≤1 次（revision_count 守卫）、整体 600s 时限、LLM 重试 ≤2 次 |

### 2.3 业务数据真实性 ✅

| 核验项 | 结果 |
|---|---|
| 新闻来源 | ✅ mining.com / australianmining.com.au 真实 RSS（实测 HTTP 200 + UA 要求已在 DATA_SOURCES.md 记录） |
| 正文获取 | ✅ trafilatura 真实抓取（集成测试 + 报告中出现正文细节） |
| NI 43-101 解析 | ✅ Sigma 报告 19 条记录与原文逐值核对；测试断言精确值（10,193,000 t @1.59% = 400.8 kt LCE） |
| Indicated/Inferred 区分 | ✅ 类别映射 + Resources/Reserves 不混用（双向校验）+ 合计行防重复计算 |
| 价格来源合法可追溯 | ✅ FRED（免费公开 CSV）+ 新浪财经 GFEX 公开行情接口（均带 source_url）；LME 被阻断/付费源一律 DATA_UNAVAILABLE |
| 硬编码示例数值 | ✅ Grep 审计：生产路径无硬编码价格/资源量（合成 PDF 数字在 mock 门控内；注册表日期为真实报告元数据） |
| 缺失数据伪装 | ✅ 空结果→DATA_UNAVAILABLE/显式缺失披露；合同测试断言无结果不编造 |

### 2.4 数据安全与准确性 ✅

| 核验项 | 结果 |
|---|---|
| SSRF | ✅ URL 规范化+凭据拒绝+DNS 解析后逐 IP 检查（IPv4/IPv6 内网段全拒）+重定向逐跳复检；30 个安全单测 |
| PDF 大小/时限 | ✅ 50MiB 上限、页数 800 上限、下载/解析超时、%PDF 魔数校验、SHA256 缓存 |
| 单位与金属量计算 | ✅ Li₂O→LCE（×2.473）、黄金（/31.1035）、百分比金属三套换算 ±10% 容差校验；单位冲突→needs_review |
| 日期准确 | ✅ ISO8601+时区；非交易日 is_estimated 显式标记；FRED 滞后窗口显式扩展+警告 |
| 事实来源 | ✅ 每条证据带 evidence_id/URL/页码/原文；确定性校验器（数值/URL/日期/缺失披露）把关 |
| 提示注入 | ✅ 真实 LLM 实测拒绝注入指令（识别并披露"不予采信"），虚假资源量未进入资源量章节 |

---

## 3. 实际测试结果（本轮全量执行）

| 测试 | 命令 | 结果 |
|---|---|---|
| Ruff | `ruff check .` / `ruff format --check .` | ✅ 全绿（82 文件） |
| Mypy | `mypy mda` | ✅ 0 问题（50 源文件） |
| 单元测试 | pytest tests/unit | ✅ 全部通过 |
| MCP 协议测试 | pytest tests/contract（真 stdio + streamable-http 会话） | ✅ 22 项通过（含新增 API 合同 3 项） |
| Agent e2e + 故障注入 | pytest tests/e2e（9 类故障） | ✅ 通过（含新增空 query 短路回归） |
| **默认全量** | `pytest` | **✅ 158 passed**（3 skipped = live/llm 门控标记） |
| 真实数据集成 | RUN_LIVE_TESTS=1 pytest tests/integration | ✅ 6 passed（真实新闻/PDF/锂价/FRED/三 Server 联合） |
| 真实 LLM e2e | RUN_LLM_TESTS=1 pytest tests/e2e/test_real_llm.py | ✅ 3 passed（含注入防御） |
| 协议验证脚本 | verify_mcp.py（Mock + 真实双模式） | ✅ 6 项 PASS |
| Docker Compose | `docker compose up -d --build` | ✅ 4 容器 healthy |
| 容器通信 | 容器内依赖检查 + 完整日报（真实 LLM+真实数据） | ✅ SMOKE PASS（460s，3799 字符报告） |
| 容器空 query | POST /report {"query":"   "} | ✅ HTTP 400 INVALID_ARGUMENT |
| 本地 CLI | 完整日报生成 | ✅ 确定性校验零失败 |

## 4. 根因分析摘要

本轮 8 个缺陷全部属于「边界路径与转述容忍」类，无核心链路缺陷：
- M1/M3/L4：正确性校验器与 Provider 之间的"契约缝隙"（过滤规则未全量覆盖 / 合理转述未被识别）；
- M2：预算参数与真实网络状况失配（非逻辑错误，实测数据驱动调整）；
- L1/L2：LangGraph 通道语义的覆盖 vs 合并细节；
- L3：缓存 TTL 与业务场景（"今日简报"）匹配度。

## 5. 未解决问题

无阻塞性未解决问题。以下为如实记录的残余风险（非缺陷）：

1. 真实 LLM 输出固有波动（约 1/5 次运行出现断言级差异，如措辞变化、偶发违反"禁止推导"写出"距今已逾4年"）——系统通过「修订一次 + ⚠️人工核查标记」机制兜底，绝不冒充全绿。
2. 金矿报告格式的品位单位识别覆盖不全（Novador 报告部分记录 needs_review）——已如实披露而非当作事实；改进方向为扩充金矿表头样本。
3. DNS 校验与 httpx 建连间的 TOCTOU 窗口（已文档化）。
4. FRED 数据滞后约 3 个月、新浪接口无 SLA——均已有显式降级路径。

## 6. 外部数据源限制（实测记录）

- Google News / 百度 RSS / LME 官网：本网络阻断（Provider 已实现，默认关闭或明确报错）
- 黄金/白银：FRED 序列 2022 年下架，无免费源 → DATA_UNAVAILABLE
- 锂精矿（spodumene）现货：Fastmarkets 付费订阅 → 用 GFEX 碳酸锂期货口径并显式标注差异
- Pilbara 目标矿区无 NI 43-101 报告（ASX/JORC 标准）→ 日报显式披露 + Sigma 报告能力验收（标注非目标矿区）

## 7. 面试题目交付要求对照

| 要求 | 状态 |
|---|---|
| 3 个独立 MCP Server + 5 个工具 + 真实协议 | ✅ 实测 |
| LangGraph Agent Client 经 MCP 调用 | ✅ 实测 |
| 真实新闻/NI 43-101 抽取/真实价格 | ✅ 实测（逐值核对） |
| Markdown 日报 + 真实引用 + 缺失披露 | ✅ 实测（校验零失败） |
| 错误处理与降级（9 类故障注入） | ✅ 实测 |
| Docker Compose / mcp-config.json / RUN.md | ✅ 实测（4 容器 healthy + 冒烟 PASS） |
| 自动化测试与真实测试结果 | ✅ 158+6+3+6 全绿（本报告 §3） |
| 确定性报告复核（非 LLM 自评） | ✅ 实现并实测拦截幻觉数值 |
| VLM 兜底（P1） | ✅ 实现（qwen-vl-plus 可用），文本路径未触发时保持文本解析 |

**最终结论：PASS。**
