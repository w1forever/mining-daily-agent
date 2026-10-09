"""新闻服务单元测试：日期过滤/去重/排序/正文清洗/参数校验（任务书 4.4/13.1）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx as httpx_mod
import pytest
import respx as respx_mod
from mda.common.errors import AppError, ErrorCode
from mda.servers.mining_news.providers import (
    RawItem,
    keyword_terms,
    normalize_article_url,
    relevance_score,
    stable_article_id,
    strip_html,
)
from mda.servers.mining_news.service import NewsService, validate_search_args

NOW = datetime.now(timezone.utc)


def _item(
    title: str, url: str, hours_ago: int = 5, summary: str = "", guid: str | None = None
) -> RawItem:
    return RawItem(
        title=title,
        url=url,
        guid=guid,
        published_at=NOW - timedelta(hours=hours_ago),
        summary=summary,
        source="test",
        score=relevance_score(title, summary, keyword_terms("lithium")),
    )


class TestKeywordTerms:
    def test_english_and_chinese(self) -> None:
        terms = keyword_terms("Pilbara 锂矿")
        assert "pilbara" in terms
        assert "锂矿" in terms
        assert "锂" not in terms  # 单字不纳入（>=2）

    def test_empty(self) -> None:
        assert keyword_terms("??") == []


class TestRelevance:
    def test_title_weight(self) -> None:
        assert relevance_score("Lithium price rally", "", ["lithium"]) == 3.0
        assert relevance_score("Copper rally", "lithium mentioned", ["lithium"]) == 1.0
        assert relevance_score("Copper rally", "", ["lithium"]) == 0.0


class TestNormalizeAndDedup:
    def test_utm_stripped(self) -> None:
        a = normalize_article_url("https://x.com/a?utm_source=tw&id=1")
        b = normalize_article_url("https://x.com/a?id=1")
        assert a == b

    def test_trailing_slash_and_case(self) -> None:
        # 主机名大小写与尾斜杠归一；路径大小写保留（URL 路径大小写敏感）
        a = normalize_article_url("https://X.com/news/")
        b = normalize_article_url("https://x.com/news")
        assert a == b

    def test_guid_preferred(self) -> None:
        assert stable_article_id("?p=123", "https://x.com/a").startswith("guid:")
        assert stable_article_id(None, "https://x.com/a").startswith("url:")


class TestStripHtml:
    def test_clean(self) -> None:
        text = strip_html("<p>Hello <b>锂矿</b> &amp; world</p>")
        assert "Hello" in text
        assert "锂矿" in text
        assert "&" in text
        assert "<" not in text


class TestValidateArgs:
    def test_ok(self) -> None:
        validate_search_args("lithium", 7)

    @pytest.mark.parametrize(
        "query,days",
        [("", 7), ("   ", 7), ("a" * 201, 7), ("ok", 0), ("ok", 31), ("ok", "7")],  # type: ignore[list-item]
    )
    def test_invalid(self, query: str, days: int) -> None:
        with pytest.raises(AppError) as exc:
            validate_search_args(query, days)
        assert exc.value.code == ErrorCode.INVALID_ARGUMENT


class TestMergeAndRank:
    """日期过滤/去重/排序（使用合成 RawItem，明确标注为测试数据）。"""

    def _service(self, tmp_path) -> NewsService:
        from mda.common.settings import Settings

        return NewsService(
            Settings(
                news_cache_dir=str(tmp_path / "cache"),
                news_max_items=10,
                news_feeds_enabled="",
            ),
            mock=True,
        )

    def test_date_filter_and_rank_and_dedup(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        items = [
            _item("Lithium A", "https://x.com/a", hours_ago=1, summary="lithium context"),
            _item("Lithium A duplicate", "https://x.com/a?utm_source=x", hours_ago=1),
            _item("Copper only", "https://x.com/c", hours_ago=1),  # 无匹配词 -> 0 分
            _item("Old lithium", "https://x.com/old", hours_ago=8 * 24),  # 超 7 天
        ]
        merged = svc._merge_and_rank(items, "lithium", 7)
        assert len(merged) == 1
        assert merged[0].url == "https://x.com/a"

    def test_empty_result_not_crash(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        merged = svc._merge_and_rank([], "lithium", 7)
        assert merged == []


class TestProviderScores:
    """Google/Baidu Provider 必须计算相关性分数（修复回归：score<=0 会被服务层过滤）。"""

    @respx_mod.mock
    async def test_google_provider_sets_score(self) -> None:
        from mda.common.http_client import SafeHttpClient
        from mda.servers.mining_news.providers import GoogleNewsRssProvider

        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            "<item><title>Lithium price rally continues</title>"
            "<link>https://news.example.com/a</link>"
            "<pubDate>Wed, 08 Oct 2026 12:00:00 +0000</pubDate>"
            "<description>lithium context</description></item></channel></rss>"
        )
        respx_mod.get(url__regex=r".*news\.google\.com.*").mock(
            return_value=httpx_mod.Response(
                200, headers={"content-type": "application/rss+xml"}, content=xml.encode()
            )
        )
        client = SafeHttpClient(
            max_retries=0,
            resolver=lambda h: _f(["93.184.216.34"]),  # 注入解析器（测试双）
        )
        provider = GoogleNewsRssProvider(client)
        items = await provider.search("lithium", 7)
        assert items, "应返回条目"
        assert all(i.score > 0 for i in items), "条目必须携带相关性分数"


async def _f(ips: list[str]) -> list[str]:
    return ips


class TestHtmlExtraction:
    """正文提取（合成 HTML，明确标注）。"""

    SAMPLE_HTML = (
        "<html><head><title>Test Lithium Article</title></head><body>"
        "<nav>menu junk</nav>"
        "<article><h1>Lithium demand grows</h1>"
        "<p>The lithium market expanded in 2026 with new supply.</p>"
        "<p>Second paragraph with more context.</p></article>"
        "<footer>copyright ads</footer></body></html>"
    )

    def test_trafilatura_extract(self) -> None:
        from mda.servers.mining_news.service import NewsService

        out = NewsService._extract(self.SAMPLE_HTML, "https://x.com/a")
        assert "Lithium demand grows" in out["content"]
        # trafilatura 可能优先取 <h1> 而非 <title>，两者均可
        assert out["title"] in ("Test Lithium Article", "Lithium demand grows")
        assert out["method"] == "trafilatura"

    def test_empty_html_fallback(self) -> None:
        from mda.servers.mining_news.service import NewsService

        out = NewsService._extract("<html><body><p>plain</p></body></html>", "https://x.com/b")
        assert "plain" in out["content"]
