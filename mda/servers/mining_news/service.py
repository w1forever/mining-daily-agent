"""mining-news-mcp 服务层：检索编排 + 正文抓取（任务书 4.2/4.3）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from mda.common.cache import JsonFileCache
from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.logging import get_logger
from mda.common.models import ToolResult, iso_now
from mda.common.security import validate_url_async
from mda.common.settings import Settings
from mda.servers.mining_news.providers import (
    BaiduNewsRssProvider,
    GoogleNewsRssProvider,
    MockNewsProvider,
    NewsProvider,
    RawItem,
    RssFeedProvider,
    mock_fetch_article,
    normalize_article_url,
    stable_article_id,
    strip_html,
)
from mda.servers.mining_news.schemas import ArticleContent, NewsArticle, NewsSearchData

log = get_logger(__name__)

_FEED_REGISTRY: dict[str, tuple[str, str]] = {
    "mining.com": ("mining.com", "https://www.mining.com/feed/"),
    "australianmining": (
        "australianmining.com.au",
        "https://www.australianmining.com.au/feed/",
    ),
}

MAX_QUERY_LEN = 200
MIN_DAYS, MAX_DAYS = 1, 30
ARTICLE_CACHE_TTL = timedelta(hours=24)
SEARCH_CACHE_TTL = timedelta(minutes=30)
MAX_ARTICLE_CONTENT = 20_000  # 返回给 Agent 的正文字符上限


def validate_search_args(query: str, days: int) -> None:
    if not query or not query.strip():
        raise AppError(ErrorCode.INVALID_ARGUMENT, "query 不能为空", retryable=False)
    if len(query) > MAX_QUERY_LEN:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"query 超过 {MAX_QUERY_LEN} 字符上限",
            retryable=False,
        )
    if not isinstance(days, int) or not (MIN_DAYS <= days <= MAX_DAYS):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"days 必须是 {MIN_DAYS}~{MAX_DAYS} 的整数",
            retryable=False,
        )


class NewsService:
    def __init__(self, settings: Settings | None = None, *, mock: bool = False) -> None:
        self.settings = settings or Settings()
        self.client = SafeHttpClient(
            connect_timeout=self.settings.http_connect_timeout_seconds,
            read_timeout=self.settings.news_fetch_timeout_seconds,
            max_bytes=self.settings.max_article_bytes,
        )
        self.cache = JsonFileCache(
            self.settings.resolved_dir(self.settings.news_cache_dir),
            ttl=ARTICLE_CACHE_TTL,
        )
        self.search_cache = JsonFileCache(
            self.settings.resolved_dir(self.settings.news_cache_dir),
            ttl=SEARCH_CACHE_TTL,
        )
        self.mock = mock
        self.providers: list[NewsProvider] = self._build_providers()

    def _build_providers(self) -> list[NewsProvider]:
        if self.mock:
            return [MockNewsProvider()]
        providers: list[NewsProvider] = []
        enabled = {
            name.strip() for name in self.settings.news_feeds_enabled.split(",") if name.strip()
        }
        for key, (source_name, feed_url) in _FEED_REGISTRY.items():
            if key in enabled:
                providers.append(RssFeedProvider(source_name, feed_url, self.client))
        if self.settings.news_provider_google:
            providers.append(GoogleNewsRssProvider(self.client))
        if self.settings.news_provider_baidu:
            providers.append(BaiduNewsRssProvider(self.client))
        return providers

    async def search(self, query: str, days: int) -> ToolResult:
        try:
            validate_search_args(query, days)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False)

        # Mock 模式绝不读写缓存：防止合成数据污染真实数据缓存
        cache_key = f"search|{query.strip().lower()}|{days}"
        if not self.mock:
            cached = self.search_cache.get(cache_key)
            if cached is not None and "articles" in cached:
                data = NewsSearchData.model_validate(cached["articles"])
                return ToolResult.ok(data, source=",".join(data.sources_queried), is_cached=True)

        if not self.providers:
            return ToolResult.fail(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "没有启用任何新闻数据源",
                retryable=False,
            )

        # 并发请求所有 Provider；单个失败不拖垮整体，计入 errors 供日志
        results = await asyncio.gather(
            *(self._safe_search(p, query, days) for p in self.providers),
            return_exceptions=True,
        )
        raw_items: list[RawItem] = []
        provider_errors: list[str] = []
        for provider, result in zip(self.providers, results, strict=True):
            if isinstance(result, BaseException):
                provider_errors.append(f"{provider.name}: {result}")
                log.warning(
                    "news_provider_failed",
                    source=provider.name,
                    error_code=getattr(result, "code", None) or "error",
                )
            else:
                raw_items.extend(result)

        articles = self._merge_and_rank(raw_items, query, days)
        sources_queried = [p.name for p in self.providers]
        data = NewsSearchData(
            query=query.strip(),
            days=days,
            articles=articles,
            total_found=len(articles),
            sources_queried=sources_queried,
        )
        warnings: list[str] = []
        if provider_errors:
            warnings.append(
                f"部分新闻源不可用（{len(provider_errors)}/{len(self.providers)}）: "
                + "; ".join(provider_errors)
            )
        if not articles:
            warnings.append("没有找到匹配的新闻（数据缺失）")
        # 空结果也缓存（短期），避免反复打源；Mock 模式不写缓存
        if not self.mock:
            self.search_cache.set(cache_key, {"articles": data.model_dump()})
        return ToolResult.ok(
            data,
            source=",".join(sources_queried),
            warnings=warnings or None,
        )

    async def _safe_search(self, provider: NewsProvider, query: str, days: int) -> list[RawItem]:
        try:
            return await asyncio.wait_for(
                provider.search(query, days),
                timeout=self.settings.news_fetch_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise AppError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"{provider.name} 检索超时",
                retryable=True,
            ) from None
        except AppError as exc:
            raise exc

    def _merge_and_rank(self, raw_items: list[RawItem], query: str, days: int) -> list[NewsArticle]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        merged: list[NewsArticle] = []
        for raw in raw_items:
            item = raw  # RawItem
            norm_url = normalize_article_url(item.url)
            item_id = stable_article_id(item.guid, item.url)
            if norm_url in seen_urls or item_id in seen_ids:
                continue  # 相同文章不重复返回
            published = item.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff:
                continue
            if item.score <= 0:
                continue  # 防御：零相关项不进入结果（Provider 层已过滤）
            seen_urls.add(norm_url)
            seen_ids.add(item_id)
            merged.append(
                NewsArticle(
                    article_id=item_id,
                    title=item.title,
                    url=item.url,
                    source=item.source,
                    published_at=published.astimezone(timezone.utc).isoformat(),
                    summary=item.summary[:400],
                    retrieved_at=iso_now(),
                    relevance_score=item.score,
                )
            )
        # 相关性排序（分数保存在 RawItem 之外，由 provider 层完成打分并附着）
        merged.sort(key=lambda a: a.relevance_score, reverse=True)
        return merged[: self.settings.news_max_items]

    async def fetch_article(self, url: str) -> ToolResult:
        # 1. URL 安全校验（SSRF）
        try:
            normalized = await validate_url_async(url)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False)

        cache_key = f"article|{normalize_article_url(normalized)}"
        if not self.mock:
            cached = self.cache.get(cache_key)
            if cached is not None and "content" in cached:
                return ToolResult.ok(
                    ArticleContent.model_validate(cached["content"]),
                    source=normalized,
                    is_cached=True,
                )

        if self.mock:
            mock_data = await mock_fetch_article(normalized)
            if mock_data is None:
                return ToolResult.fail(
                    ErrorCode.DOCUMENT_NOT_FOUND,
                    f"文档不存在: {normalized}",
                    retryable=False,
                )
            content = ArticleContent(url=normalized, final_url=normalized, **mock_data)
            content.content_length = len(content.content)
            return ToolResult.ok(content, source=normalized)

        # 2. 抓取 + 重定向复检由 SafeHttpClient 完成
        try:
            outcome = await self.client.fetch(
                normalized,
                max_bytes=self.settings.max_article_bytes,
                kind_hint="html",
            )
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=exc.retryable)

        html = outcome.body.decode("utf-8", errors="replace")
        warnings: list[str] = []

        # 3. trafilatura 正文提取
        extracted = self._extract(html, normalized)
        if not extracted["content"]:
            warnings.append("正文提取失败：页面可能为 JS 渲染或付费墙，不臆造细节")
        content = ArticleContent(
            url=normalized,
            final_url=outcome.url,
            title=extracted["title"],
            author=extracted["author"],
            published_at=extracted["published_at"],
            content=extracted["content"][:MAX_ARTICLE_CONTENT],
            content_length=len(extracted["content"]),
            extraction_method=extracted["method"],
            summary=extracted["content"][:400],
        )
        self.cache.set(cache_key, {"content": content.model_dump()})
        return ToolResult.ok(content, source=normalized, warnings=warnings or None)

    @staticmethod
    def _extract(html: str, url: str) -> dict:
        import trafilatura

        try:
            doc = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            )
            meta = trafilatura.extract_metadata(html)
            title = meta.title or ""
            author = meta.author or ""
            date = ""
            meta_date = getattr(meta, "date", None)
            if meta_date:
                try:
                    # trafilatura 类型桩为 str；实际可能是 datetime
                    date = meta_date.isoformat()  # type: ignore[union-attr]
                except (AttributeError, ValueError):
                    date = ""
            if doc and doc.strip():
                return {
                    "title": title,
                    "author": author,
                    "published_at": date,
                    "content": doc.strip(),
                    "method": "trafilatura",
                }
            return {
                "title": title or "",
                "author": author,
                "published_at": date,
                "content": "",
                "method": "trafilatura",
            }
        except Exception as exc:  # noqa: BLE001 - 提取失败必须降级而非崩溃
            log.warning("trafilatura_extract_failed", url=url, reason=str(exc))
            fallback = strip_html(html)
            return {
                "title": "",
                "author": "",
                "published_at": "",
                "content": fallback[:MAX_ARTICLE_CONTENT],
                "method": "html_strip_fallback",
            }
