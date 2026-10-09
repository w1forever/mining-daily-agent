# DATA_SOURCES.md — 数据源可行性表

所有结论来自 2026-10-08 的真实 HTTP 实测（研究代理 + 人工复核）。未验证的
数据源绝不标注为可用。

## 一、新闻源

| 源 | 端点 | 状态 | 说明 |
|---|---|---|---|
| mining.com | `https://www.mining.com/feed/` | ✅ 可用 | RSS 200，~36 条/日；**非浏览器 UA 返回 403**，已统一使用浏览器 UA |
| Australian Mining | `https://www.australianmining.com.au/feed/` | ✅ 可用 | RSS 200，量小（~5 条），辅助源 |
| Google News RSS | `https://news.google.com/rss/search?q=...` | ⛔ 本网络阻断 | TCP 无法建立（CN egress）；Provider 已实现，默认关闭，可达网络下可启用 |
| 百度新闻 RSS | `https://www.baidu.com/search/rss.php?...` | ⛔ 本网络阻断 | 302 → forbiddenip；Provider 已实现，默认关闭 |
| Kitco RSS | `https://www.kitco.com/rss/news.xml` | ❌ 已死 | 404（多路径均 404） |
| Resource World | `https://resourceworld.com/feed/` | ❌ Cloudflare 挑战 | 403 挑战页，UA 无效，需 JS 客户端 |

许可说明：仅访问公开 RSS/页面，不绕过任何付费墙或登录墙；抓取频率受
`asyncio` 并发上限与缓存（24h TTL）约束。

## 二、价格源

| 源 | 端点 | 状态 | 口径 |
|---|---|---|---|
| FRED（St. Louis Fed） | `fredgraph.csv?id=PCOPPUSDM` 等 6 个序列 | ✅ 可用 | 铜/铝/镍/锌/锡/铅 全球月度均价，USD/metric tonne；**WAF 对「数据中心 IP+浏览器 UA」黑洞连接，必须用 curl 类工具 UA**（实测）；数据滞后约 3 个月，已做显式窗口扩展并警告 |
| 新浪财经公开行情 | `stock2.finance.sina.com.cn/futures/api/json.php/...getDailyKLine?symbol=lc0` | ✅ 可用 | GFEX 碳酸锂期货主力连续日线，CNY/tonne，实时到当日；**无效 symbol 返回 200+空数组，必须判空**；Content-Type 会在 json/javascript/plain 间波动，已容差 |
| LME 官网 | `lme.com/en/metals/ev/lithium-hydroxide-cif-fastmarkets` | ⛔ Cloudflare 阻断 | 403 挑战页（浏览器头也无效）；无授权实时行情，不冒充 |
| FRED 黄金/白银 | `PGOLDUSDM` / `PSILVERUSDM` | ❌ 不存在 | 2022-01 IBA/LBMA 数据已从 FRED 移除；当前无免费源 → `DATA_UNAVAILABLE` |
| 世界银行 Pink Sheet | `thedocs.worldbank.org/.../CMO-Historical-Data-Monthly.xlsx` | ✅ 可用（未接入） | 任务书原 URL 已死（404），新 URL 200；月度 xlsx，当前未接入管线 |
| 锂精矿（spodumene）现货指数 | Fastmarkets 等 | ❌ 付费订阅 | 无免费源 → 明确 `DATA_UNAVAILABLE`，日报用碳酸锂期货口径并显式标注差异 |
| 氢氧化锂 | LME CIF / 商业订阅 | ❌ 无免费源 | `DATA_UNAVAILABLE` |

## 三、NI 43-101 PDF（证据注册表 data/known_pdfs.json）

均实测：HTTP 200 + `%PDF` 魔数 + 封面核对（SHA256 记录于 fixtures/README.md）。

| 项目 | URL | 生效日 | 大小 | 状态 |
|---|---|---|---|---|
| Sigma Lithium — Grota do Cirilo（锂） | sigmalithiumresources.com/.../2023-01-SGML-Updated-Technical-Report-1.pdf | 2022-10-31 | 17MB | ✅ 启用（主验收报告，19 条资源量记录逐值核对） |
| Probe Gold — Novador（金） | novador.ca/.../probe-43-101-pea-march-26.pdf | 2024-02-13 | 16MB | ✅ 启用 |
| Lithium Americas — Thacker Pass（锂） | investors.lithium-argentina.com/static-files/fe7e604b-... | 2018-08-01 | 5MB | 注册（SEC 6-K 包裹，封面在第 4 页） |
| Patriot — Shaakichiuwaanaan FS（锂） | pmet.ca/.../FS_Technical_Report_43-101-1.pdf | 2025-11 | 34MB | 注册（超默认体积偏好，关闭） |
| Frontier — PAK FS/PFS（锂） | frontierlithium.com/_files/ugd/... | — | 28-35MB | 注册（体积大，关闭） |

**标准披露**：Pilbara 矿企（Pilbara Minerals 等 ASX 上市）采用 JORC 标准，
不发布 NI 43-101 报告；系统绝不把 JORC 数据伪装为 NI 43-101，日报显式披露
目标报告缺失并以注册表报告完成能力验收（标注"非目标矿区"）。

失败样本（如实记录）：Skeena Eskay Creek 404；Osisko 301→非 PDF；
BLM Thacker Pass 403（bot 拦截）。

## 四、LLM / VLM

- LLM：OpenAI 兼容端点（默认 DashScope `qwen-plus`），`LLM_BASE_URL`/`LLM_MODEL`
  可换成任意兼容服务；支持 `mock` Provider（仅测试，明确标注）。
- VLM（PDF 复杂表格兜底）：`qwen-vl-plus`（OpenAI 兼容多模态），只对候选页调用。
- 密钥通过环境变量注入，绝不写入镜像/日志（日志层自动脱敏）。
