"""统一配置管理（pydantic-settings）。

- 环境变量优先，其次项目根目录 .env 文件。
- 密钥类字段只存引用，日志层负责脱敏。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 日志 ----
    log_level: str = "INFO"

    # ---- LLM（默认 OpenAI 兼容协议；DashScope/DeepSeek/Moonshot 等均可）----
    llm_provider: str = "openai_compatible"  # openai_compatible | anthropic | mock
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
    llm_api_key: str = ""
    dashscope_api_key: str = ""
    vlm_model: str = "qwen-vl-plus"
    vlm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vlm_api_key: str = ""
    llm_timeout_seconds: float = 120.0
    llm_temperature: float = 0.0

    # ---- Agent 任务预算 ----
    task_timeout_seconds: float = 600.0
    max_retries_tool: int = 2
    max_retries_agent: int = 1
    max_report_revisions: int = 1

    # ---- MCP 连接 ----
    mcp_transport: str = "http"  # http | stdio
    mcp_news_url: str = "http://mining-news-mcp:8001/mcp"
    mcp_pdf_url: str = "http://mineral-pdf-mcp:8002/mcp"
    mcp_price_url: str = "http://lme-price-mcp:8003/mcp"
    mcp_call_timeout_seconds: float = 60.0

    # ---- HTTP 通用限制 ----
    http_connect_timeout_seconds: float = 10.0
    http_read_timeout_seconds: float = 30.0
    max_article_bytes: int = 2 * 1024 * 1024  # 2 MiB 正文抓取上限
    max_download_bytes: int = 50 * 1024 * 1024  # 50 MiB PDF 下载上限

    # ---- PDF ----
    pdf_max_pages: int = 800
    pdf_max_parse_seconds: float = 120.0
    pdf_use_vlm: bool = True
    pdf_cache_dir: str = "data/cache/pdf"
    extract_cache_dir: str = "data/cache/extract"

    # ---- 新闻 ----
    news_cache_dir: str = "data/cache/news"
    news_fetch_timeout_seconds: float = 20.0
    news_max_items: int = 30
    news_feeds_enabled: str = "mining.com,australianmining"  # 逗号分隔
    news_provider_google: bool = False  # 本网络环境被阻断，默认关闭
    news_provider_baidu: bool = False  # 本网络环境被阻断，默认关闭

    # ---- 价格 ----
    price_cache_dir: str = "data/cache/price"
    price_cache_ttl_hours: float = 6.0
    fred_enabled: bool = True
    sina_gfex_enabled: bool = True

    # ---- API ----
    api_host: str = "0.0.0.0"  # noqa: S104 - 容器内有意监听所有接口
    api_port: int = 8000

    # ---- 报告输出 ----
    reports_dir: str = "reports"

    @property
    def effective_llm_key(self) -> str:
        return self.llm_api_key or self.dashscope_api_key

    @property
    def effective_vlm_key(self) -> str:
        return self.vlm_api_key or self.effective_llm_key

    @property
    def llm_configured(self) -> bool:
        return bool(self.effective_llm_key)

    def resolved_dir(self, rel: str) -> Path:
        p = Path(rel)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
