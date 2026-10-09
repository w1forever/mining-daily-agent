"""PDF 安全获取（任务书 5.3 第一阶段）。

- URL 安全校验（SSRF）
- 下载超时 + 文件最大体积限制
- Content-Type 与实际魔数（%PDF）双重校验
- SHA256 去重 + 文件缓存
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mda.common.cache import BlobCache
from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.logging import get_logger
from mda.common.models import iso_now
from mda.common.security import looks_like_pdf, validate_url_async
from mda.common.settings import Settings

log = get_logger(__name__)

PDF_MAGIC = b"%PDF-"


@dataclass
class DownloadedPdf:
    path: Path
    sha256: str
    size: int
    url: str
    is_cached: bool


class PdfDownloader:
    def __init__(
        self,
        settings: Settings,
        client: SafeHttpClient | None = None,
        resolver=None,  # 可注入 DNS 解析器（测试用）
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.client = client or SafeHttpClient(
            connect_timeout=settings.http_connect_timeout_seconds,
            read_timeout=settings.http_read_timeout_seconds,
            max_bytes=settings.max_download_bytes,
            resolver=resolver,
        )
        self.blob = BlobCache(settings.resolved_dir(settings.pdf_cache_dir))

    async def download(self, url: str) -> DownloadedPdf:
        # 1. URL 安全校验
        try:
            normalized = await validate_url_async(url, resolver=self.resolver)
        except AppError as exc:
            raise AppError(exc.code, exc.message, retryable=False) from exc

        # 2. 下载（SafeHttpClient 内部处理重定向复检/超时/限速/体积上限）
        try:
            body = await self.client.fetch_bytes(
                normalized,
                max_bytes=self.settings.max_download_bytes,
                kind_hint="pdf",
            )
        except AppError as exc:
            raise exc

        # 3. 魔数校验（防 Content-Type 伪装）
        if not looks_like_pdf(body):
            raise AppError(
                ErrorCode.PDF_PARSE_FAILED,
                "下载内容不是有效 PDF（魔数校验失败）",
                retryable=False,
                details={"url": normalized, "size": len(body)},
            )

        # 4. SHA256 去重 + 缓存
        sha256 = self.blob.sha256_of(body)
        if not self.blob.has(sha256):
            self.blob.write(sha256, body)
            meta = self.blob.root / f"{sha256}.json"
            import json

            meta.write_text(
                json.dumps(
                    {
                        "url": normalized,
                        "size": len(body),
                        "sha256": sha256,
                        "retrieved_at": iso_now(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            is_cached = False
        else:
            is_cached = True

        log.info(
            "pdf_downloaded",
            url=normalized,
            sha256=sha256[:16],
            bytes=len(body),
            is_cached=is_cached,
        )
        return DownloadedPdf(
            path=self.blob.root / f"{sha256}.bin",
            sha256=sha256,
            size=len(body),
            url=normalized,
            is_cached=is_cached,
        )
