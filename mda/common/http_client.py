"""安全 HTTP 客户端。

所有对外抓取（RSS、HTML 正文、PDF 下载）都必须经由 SafeHttpClient：
- 请求前 URL + DNS IP 校验（SSRF 防护）
- 重定向逐跳重新校验（含 DNS 复检）
- 流式读取并限制最大响应体积
- 超时控制（连接超时 + 读取超时）
- 瞬时故障（超时/429/5xx）指数退避重试，最多 2 次；4xx 不重试
- 默认浏览器 User-Agent（部分矿业主流源对非浏览器 UA 返回 403）
"""

from __future__ import annotations

import asyncio
import time

import httpx

from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger
from mda.common.security import (
    DEFAULT_BROWSER_UA,
    FetchOutcome,
    Resolver,
    validate_url_async,
)

log = get_logger(__name__)


class SafeHttpClient:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_BROWSER_UA,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_bytes: int = 2 * 1024 * 1024,
        max_retries: int = 2,
        base_delay: float = 0.5,
        resolver: Resolver | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.resolver = resolver  # 可注入的 DNS 解析器（测试用）

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
            follow_redirects=False,  # 重定向逐跳手动处理，保证每跳都做安全校验
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            },
            trust_env=False,  # 不读取代理环境变量，避免泄露内网代理
        )

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        allow_status: set[int] | None = None,
        kind_hint: str | None = None,  # "html" | "pdf" | "xml" | None
    ) -> FetchOutcome:
        """安全抓取一个 URL（跟随重定向并逐跳校验）。

        - 429/5xx/超时：指数退避重试（最多 max_retries 次）
        - 其他 4xx：不重试，抛 AppError
        """
        cap = max_bytes or self.max_bytes
        allowed = allow_status or {200}
        start = time.perf_counter()
        current = await validate_url_async(url, resolver=self.resolver)

        attempts = 0
        while True:
            attempts += 1
            error: AppError | None = None
            try:
                outcome = await self._fetch_single(
                    current, max_bytes=cap, allowed=allowed, kind_hint=kind_hint
                )
                log.info(
                    "http_fetch_ok",
                    url=url,
                    final_url=outcome.url,
                    status=outcome.status,
                    bytes=len(outcome.body),
                    attempts=attempts,
                    elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
                )
                return outcome
            except AppError as exc:
                error = exc
            except httpx.TimeoutException:
                error = AppError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    f"上游数据源请求超时: {url}",
                    retryable=True,
                    details={"attempt": attempts},
                )
            except httpx.HTTPError as exc:
                error = AppError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"上游连接失败: {exc}",
                    retryable=True,
                    details={"attempt": attempts},
                )
            if attempts > self.max_retries or error is None or not error.retryable:
                if error is not None:
                    log.warning(
                        "http_fetch_failed",
                        url=url,
                        error_code=error.code.value,
                        attempts=attempts,
                        elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
                    )
                    raise error
                raise AppError(ErrorCode.UPSTREAM_ERROR, "未知抓取失败", retryable=True)
            delay = self.base_delay * (2 ** (attempts - 1))
            log.info(
                "http_retry", url=url, attempts=attempts, delay=delay, error_code=error.code.value
            )
            await asyncio.sleep(delay)

    async def _fetch_single(
        self,
        url: str,
        *,
        max_bytes: int,
        allowed: set[int],
        kind_hint: str | None,
        _redirects_left: int = 4,
    ) -> FetchOutcome:
        async with self._client() as client:
            response = await client.send(client.build_request("GET", url), stream=True)
            status = response.status_code

            # 重定向：目标重新做完整安全校验
            if status in (301, 302, 303, 307, 308) and _redirects_left > 0:
                location = response.headers.get("location", "")
                await response.aclose()
                if not location:
                    raise AppError(
                        ErrorCode.UPSTREAM_ERROR,
                        "重定向响应缺少 Location 头",
                        retryable=False,
                    )
                from urllib.parse import urljoin

                next_url = urljoin(url, location)
                log.info("http_redirect", url=url, next=next_url)
                # 重定向目标逐跳重新校验（含 DNS IP 检查，任务书 10.2）
                next_url = await validate_url_async(next_url, resolver=self.resolver)
                return await self._fetch_single(
                    next_url,
                    max_bytes=max_bytes,
                    allowed=allowed,
                    kind_hint=kind_hint,
                    _redirects_left=_redirects_left - 1,
                )
            if _redirects_left == 0 and status in (301, 302, 303, 307, 308):
                raise AppError(ErrorCode.UPSTREAM_ERROR, "重定向次数过多", retryable=False)

            if status == 429:
                raise AppError(
                    ErrorCode.UPSTREAM_RATE_LIMITED,
                    "上游数据源频控（HTTP 429）",
                    retryable=True,
                    details={"url": url},
                )
            if status not in allowed and status >= 500:
                raise AppError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"上游返回 {status}",
                    retryable=True,
                    details={"url": url, "status": status},
                )
            if status == 404:
                raise AppError(
                    ErrorCode.DOCUMENT_NOT_FOUND,
                    f"文档不存在（HTTP 404）: {url}",
                    retryable=False,
                )
            if status not in allowed:
                raise AppError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"上游返回异常状态码 {status}",
                    retryable=False,
                    details={"url": url, "status": status},
                )

            content_type = response.headers.get("content-type", "")
            if kind_hint:
                self._check_content_type(content_type, kind_hint)

            # 流式读取 + 体积上限
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    await response.aclose()
                    raise AppError(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"响应体积超过上限 {max_bytes} 字节",
                        retryable=False,
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            return FetchOutcome(
                url=url,
                status=status,
                content_type=content_type,
                body=body,
                final_host_ips=[],
            )

    @staticmethod
    def _check_content_type(content_type: str, kind_hint: str) -> None:
        if kind_hint == "pdf":
            if "application/pdf" not in (content_type or "").lower() and (
                content_type and "octet-stream" not in content_type.lower()
            ):
                raise AppError(
                    ErrorCode.CONTENT_TYPE_UNEXPECTED,
                    f"Content-Type 非 PDF: {content_type}",
                    retryable=False,
                )
        elif kind_hint == "html":
            ct = (content_type or "").lower()
            if ct and "text/html" not in ct and "xml" not in ct:
                raise AppError(
                    ErrorCode.CONTENT_TYPE_UNEXPECTED,
                    f"Content-Type 非 HTML: {content_type}",
                    retryable=False,
                )
        elif kind_hint == "json":
            # 行情/数据 API 端点：允许 json/javascript/plain/csv/octet-stream
            # （新浪等源对同一端点会返回不同的 Content-Type）
            ct = (content_type or "").lower()
            allowed_markers = (
                "json",
                "javascript",
                "plain",
                "csv",
                "octet-stream",
                "xml",
            )
            if ct and not any(m in ct for m in allowed_markers):
                raise AppError(
                    ErrorCode.CONTENT_TYPE_UNEXPECTED,
                    f"Content-Type 非数据格式: {content_type}",
                    retryable=False,
                )

    async def fetch_text(self, url: str, *, max_bytes: int | None = None) -> str:
        outcome = await self.fetch(url, max_bytes=max_bytes, kind_hint="html")
        return outcome.body.decode("utf-8", errors="replace")

    async def fetch_bytes(
        self, url: str, *, max_bytes: int | None = None, kind_hint: str | None = None
    ) -> bytes:
        outcome = await self.fetch(url, max_bytes=max_bytes, kind_hint=kind_hint)
        return outcome.body
