"""mining-news-mcp 数据 Schema（任务书 4.2/4.3）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NewsArticle(BaseModel):
    """一条新闻检索结果。文章必须有可追溯的真实 URL。"""

    article_id: str  # 稳定 ID（feed guid 或 URL 规范化哈希）
    title: str
    url: str
    source: str  # 数据源/站点名，如 mining.com
    published_at: str  # ISO 8601（带时区）
    summary: str = ""
    retrieved_at: str = ""
    relevance_score: float = 0.0


class NewsSearchData(BaseModel):
    query: str
    days: int
    articles: list[NewsArticle] = Field(default_factory=list)
    total_found: int = 0
    sources_queried: list[str] = Field(default_factory=list)


class ArticleContent(BaseModel):
    """正文抓取结果。content 为空时表示正文提取失败（不臆造细节）。"""

    url: str  # 原始来源 URL（可用于引用）
    final_url: str = ""  # 重定向后的真实 URL
    title: str = ""
    author: str = ""
    published_at: str = ""
    content: str = ""
    content_length: int = 0
    extraction_method: str = "trafilatura"
    summary: str = ""
