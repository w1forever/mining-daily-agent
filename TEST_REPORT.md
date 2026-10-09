# TEST_REPORT.md — 测试报告

生成时间：2026-10-09（最后一次全量回归）

## 1. 测试总数与结果

| 测试类别 | 数量 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---|---|---|---|
| 单元测试（tests/unit） | 122 | 122 | 0 | 0 | Mock/Fixture 隔离（明确标注） |
| MCP 协议合同测试（tests/contract） | 22 | 22 | 0 | 0 | 真实 stdio/streamable-http 会话 + API 合同（含空 query 400 回归） |
| Agent e2e + 故障注入（tests/e2e，默认部分） | 17 | 14 | 0 | 3（真实 LLM 门控） | 真实 MCP 链路 + 脚本化 LLM（明确标注）+ 空 query 短路回归 |
| **默认全量（pytest tests/unit tests/contract tests/e2e）** | **161** | **158** | **0** | **3（真实 LLM 门控）** | 审计轮新增 5 项回归（Provider 分数/空 query 短路/API 合同）后实测 |
| 真实数据集成（tests/integration，RUN_LIVE_TESTS=1） | 6 | 6 | 0 | 0 | 真实新闻/PDF/价格，全部实测通过 |
| 真实 LLM e2e（RUN_LLM_TESTS=1） | 3 | 3 | 0 | 0 | qwen-plus 真实结构化输出 + 注入防御 |
| 质量门禁 | — | — | — | — | `ruff check` ✅ / `ruff format --check` ✅ / `mypy mda` ✅（0 问题） |
| Docker 容器冒烟（compose exec） | 2 | 2 | 0 | 0 | 依赖检查 ✅ + 完整日报（166s，34 来源）✅ |
| MCP 协议验证脚本（verify_mcp.py） | 3×2 | 6 | 0 | 0 | Mock 与真实数据两种模式，三 Server 全 PASS |

## 2. 真实数据验证明细（非 Mock）

- 新闻：mining.com 真实检索（当日文章）与正文抓取（trafilatura）✅
- PDF：Sigma Lithium Grota do Cirilo NI 43-101（568 页）✅
  - 19 条资源量记录（5 矿床），与原文逐值核对一致（如 Xuxa Measured
    10,193,000 t @ 1.59% Li₂O = 400.8 kt LCE，页 36）
  - Li₂O→LCE 量级校验（×2.473 系数）全部通过；合计行防重复计算通过；
    真实触发「文中 3 个生效日期」混用警告 ✅
- 价格：GFEX 碳酸锂期货日线（实时到当日，CNY/吨）✅；FRED 铜月度均价 ✅
- 三 Server 联合 MCP 调用 ✅

## 3. 故障注入覆盖（任务书 13.5）

| 故障 | 验证结果 |
|---|---|
| 1. 新闻源超时 | ✅ UPSTREAM_TIMEOUT + retryable（respx 注入） |
| 2. PDF 404 | ✅ DOCUMENT_NOT_FOUND + 不重试 |
| 3. PDF 无资源量表 | ✅ RESOURCE_TABLE_NOT_FOUND |
| 4. 价格 Provider 空数据 | ✅ DATA_UNAVAILABLE（含新浪 200+空数组陷阱） |
| 5. MCP Server 连接失败 | ✅ MCP_CONNECTION_FAILED + retryable（真实断连端口） |
| 6. LLM 非法 JSON | ✅ 规划器重试后确定性兜底（图级验证） |
| 7. 工具结果字段缺失 | ✅ 契约默认值安全降级（data=None → 按缺失处理） |
| 8. PDF 单位冲突 | ✅ needs_review + 警告（换算不一致） |
| 9. 正文提示注入 | ✅ 真实 LLM 显式识别并拒绝注入指令（报告中披露"不予采信"），注入的虚假资源量未进入资源量章节 |

## 4. 测试中发现并修复的关键缺陷（如实记录）

1. **stdio 日志污染 JSON-RPC 通道**（logging 写 stdout）→ 日志改 stderr。
2. **重定向未重新校验**（SSRF 绕过隐患，测试抓出）→ 逐跳 validate_url_async。
3. **Mock 数据污染生产缓存**（mock 探针写入 data/cache）→ Mock 模式禁用缓存 + 清库。
4. **真实表格列错位**（表头与数据列索引偏移）→ 改为非空单元格顺序 + 字段序 +
   量级启发式映射；多级表头（Grade 标签在第二行）→ 合并表头行构建字段序。
5. **`ag` 误匹配 Tonnage** → 短键词边界匹配。
6. **合计行被丢弃**（'Measured + Indicated' 空格未归一）→ 规范化修复。
7. **FRED WAF 黑洞连接**（数据中心 IP+浏览器 UA 被挂起）→ FRED 专用 curl 类 UA
   （实测定位）；FRED 月度数据滞后 → 趋势窗口显式扩展并警告。
8. **报告校验器**：中文 `\w` 边界导致 "117,300" 只匹配 "300"（ASCII 词边界修复）、
   证据引用 ID 误报、`43-101` 上下文误报、"a fifth"→20% 转述误报、
   "820万" vs "$8.2 million" 单位量词换算、四舍五入表述（18 vs 18.25%）、
   报告日期行精确匹配、URL slug 内数字放行。
9. **MCP SDK 1.30 的 DNS rebinding 防护**在 Docker 服务名访问时 421 →
   TransportSecuritySettings.allowed_hosts 显式放行（MDA_ALLOWED_HOSTS）。
10. **真实 LLM 商品名不规范**（"lithium carbonate futures (GFEX...)"）→
    规划器提示词约束 + 价格节点别名归一化双保险。

## 5. 外部依赖与阻塞项（如实披露）

| 项 | 状态 |
|---|---|
| LLM 密钥 | 用户提供 DashScope 密钥，实测有效（qwen-plus/qwen-vl-plus 可达） |
| Google News / 百度 RSS | 本网络阻断，Provider 已实现默认关闭（DATA_SOURCES.md 记录） |
| LME 官网 | Cloudflare 阻断，无授权行情不冒充 |
| 黄金/白银价格 | FRED 序列 2022 年下架，无免费源 → DATA_UNAVAILABLE |
| 锂精矿（spodumene）现货 | 商业订阅（Fastmarkets），无免费源 → 明确披露，用碳酸锂期货口径并标注差异 |
| Pilbara 目标矿区 NI 43-101 | 不存在（ASX/JORC 标准）→ 日报显式披露，Sigma 报告仅作能力验收 |

## 6. 已知限制与残余风险

1. **真实 LLM 测试的固有波动**：不同轮次 LLM 输出有差异，出现过两次
   （注入披露措辞变化、LLM 自行推导"距今逾4年"）。已通过「修订一次 +
   人工核查标记」机制兜底；提示词已补充禁止推导规则，报告绝不冒充全绿。
2. PDF 解析对**无网格线/图文混排**表格依赖 VLM 兜底；VLM 未配置时此类
   文档返回 RESOURCE_TABLE_NOT_FOUND（不猜测）。
3. **金矿报告格式的品位单位检测覆盖不全**：Novador（金）PEA 的表格
   结构与本解析器主验证的锂报告不同，部分记录品位单位未能自动识别；
   系统正确降级为 needs_review + 人工复核披露（2026-10-09 实测行为），
   未将单位不明的数据当作事实输出。改进方向：扩充金矿表头样本集。
4. DNS 校验与 httpx 建连间的 TOCTOU 窗口已文档化（对抗主动 rebinding
   需出口代理固定解析）。
5. 新浪行情接口为公开网页接口，无 SLA；异常时降级为 DATA_UNAVAILABLE。
