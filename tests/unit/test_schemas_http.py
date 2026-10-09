"""统一契约与安全 HTTP 客户端单元测试（任务书 3.1/10.1/13.1）。"""

from __future__ import annotations

import httpx
import pytest
import respx
from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.models import ToolResult, tool_result_from_dict


class TestToolResultContract:
    def test_success_shape(self) -> None:
        r = ToolResult.ok({"a": 1}, source="s")
        assert r.status == "success"
        assert r.data == {"a": 1}
        assert r.error is None
        assert r.metadata.source == "s"
        assert r.metadata.retrieved_at  # ISO8601

    def test_partial_with_warnings(self) -> None:
        r = ToolResult.ok({"a": 1}, warnings=["w1"])
        assert r.status == "partial"
        assert r.warnings == ["w1"]

    def test_error_shape(self) -> None:
        r = ToolResult.fail(ErrorCode.UPSTREAM_TIMEOUT, "超时")
        assert r.status == "error"
        assert r.data is None
        assert r.error is not None
        assert r.error.code == ErrorCode.UPSTREAM_TIMEOUT
        assert r.error.retryable is True  # 超时默认可重试

    def test_error_non_retryable_by_default(self) -> None:
        r = ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "参数错误")
        assert r.error is not None
        assert r.error.retryable is False

    def test_no_stacktrace_leaked(self) -> None:
        try:
            raise RuntimeError("secret internal detail")
        except RuntimeError:
            r = ToolResult.fail(ErrorCode.UPSTREAM_ERROR, "通用上游错误")
        dumped = r.to_mcp_text()
        assert "secret internal detail" not in dumped
        assert "Traceback" not in dumped

    def test_roundtrip(self) -> None:
        r = ToolResult.ok({"x": [1, 2]})
        r2 = tool_result_from_dict(r.model_dump())
        assert r2 == r

    def test_unknown_error_code_rejected(self) -> None:
        with pytest.raises(ValueError):
            ToolResult.fail("NOT_A_CODE", "x")  # type: ignore[arg-type]


class TestSafeHttpClient:
    """respx 模拟 HTTP（明确标注：隔离外部网络）。"""

    @staticmethod
    async def _public_resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    def _client(self, **kw) -> SafeHttpClient:
        return SafeHttpClient(
            connect_timeout=2.0,
            read_timeout=3.0,
            base_delay=0.0,
            resolver=self._public_resolver,  # 注入解析器，避免真实 DNS
            **kw,
        )

    @respx.mock
    async def test_success(self) -> None:
        respx.get("https://public.example.com/a").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=b"ok")
        )
        client = self._client()
        outcome = await client.fetch("https://public.example.com/a", kind_hint="html")
        assert outcome.body == b"ok"

    @respx.mock
    async def test_retry_then_success(self) -> None:
        route = respx.get("https://public.example.com/flaky")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"recovered"),
        ]
        client = self._client()
        outcome = await client.fetch("https://public.example.com/flaky")
        assert outcome.body == b"recovered"
        assert route.call_count == 2

    @respx.mock
    async def test_429_no_retry_beyond_limit(self) -> None:
        route = respx.get("https://public.example.com/rl")
        route.side_effect = [httpx.Response(429), httpx.Response(429)]
        client = self._client(max_retries=1)
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/rl")
        assert exc.value.code == ErrorCode.UPSTREAM_RATE_LIMITED
        assert route.call_count == 2

    @respx.mock
    async def test_404_document_not_found_no_retry(self) -> None:
        route = respx.get("https://public.example.com/missing")
        route.side_effect = [httpx.Response(404)]
        client = self._client(max_retries=2)
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/missing")
        assert exc.value.code == ErrorCode.DOCUMENT_NOT_FOUND
        assert exc.value.retryable is False
        assert route.call_count == 1  # 4xx 不重试

    @respx.mock
    async def test_timeout_retryable(self) -> None:
        def _timeout(request):  # type: ignore[no-untyped-def]
            raise httpx.ConnectTimeout("timeout")

        respx.get("https://public.example.com/slow").mock(side_effect=_timeout)
        client = self._client(max_retries=1)
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/slow")
        assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT
        assert exc.value.retryable is True

    @respx.mock
    async def test_size_cap(self) -> None:
        respx.get("https://public.example.com/big").mock(
            return_value=httpx.Response(200, content=b"x" * 5000)
        )
        client = self._client(max_bytes=1024)
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/big")
        assert exc.value.code == ErrorCode.PAYLOAD_TOO_LARGE

    @respx.mock
    async def test_redirect_revalidated(self) -> None:
        """重定向目标必须重新校验：指向内网的重定向被拒绝。"""
        respx.get("https://public.example.com/r").mock(
            return_value=httpx.Response(302, headers={"location": "http://10.0.0.1/x"})
        )

        async def resolver(host: str) -> list[str]:
            return ["93.184.216.34"] if "public" in host else ["10.0.0.1"]

        client = SafeHttpClient(
            connect_timeout=2.0,
            read_timeout=3.0,
            base_delay=0.0,
            resolver=resolver,
        )
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/r")
        assert exc.value.code == ErrorCode.INVALID_URL

    @respx.mock
    async def test_html_content_type_enforced(self) -> None:
        respx.get("https://public.example.com/bin").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/zip"}, content=b"data"
            )
        )
        client = self._client()
        with pytest.raises(AppError) as exc:
            await client.fetch("https://public.example.com/bin", kind_hint="html")
        assert exc.value.code == ErrorCode.CONTENT_TYPE_UNEXPECTED

    @respx.mock
    async def test_json_kind_lenient(self) -> None:
        """数据 API 端点的 Content-Type 容差（新浪等源会波动）。"""
        respx.get("https://public.example.com/api").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/javascript"}, content=b"[]"
            )
        )
        client = self._client()
        outcome = await client.fetch("https://public.example.com/api", kind_hint="json")
        assert outcome.body == b"[]"
