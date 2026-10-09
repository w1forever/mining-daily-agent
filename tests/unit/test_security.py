"""URL 安全校验 / SSRF 防护单元测试（任务书 10.2/13.1）。"""

from __future__ import annotations

import pytest
from mda.common.errors import AppError, ErrorCode
from mda.common.security import (
    content_type_kind,
    ip_is_blocked,
    looks_like_pdf,
    normalize_url,
    validate_url_async,
)

# 测试双：注入式 DNS 解析器（明确标注，避免真实 DNS）
FAKE_RESOLVER = {
    "public.example.com": ["93.184.216.34"],
    "private.example.com": ["10.0.0.5"],
    "loopback.example.com": ["127.0.0.1"],
    "linklocal.example.com": ["169.254.1.1"],
    "ipv6-ula.example.com": ["fd00::1"],
    "multi.example.com": ["93.184.216.34", "192.168.1.10"],  # 混含内网
}


async def _resolver(host: str) -> list[str]:
    return FAKE_RESOLVER.get(host, [])


class TestNormalizeUrl:
    def test_reject_non_http(self) -> None:
        with pytest.raises(AppError) as exc:
            normalize_url("ftp://example.com/file")
        assert exc.value.code == ErrorCode.INVALID_URL

    def test_reject_credentials(self) -> None:
        with pytest.raises(AppError) as exc:
            normalize_url("https://user:pass@example.com/a")
        assert exc.value.code == ErrorCode.INVALID_URL

    def test_reject_empty(self) -> None:
        with pytest.raises(AppError) as exc:
            normalize_url("")
        assert exc.value.code == ErrorCode.INVALID_URL

    def test_reject_too_long(self) -> None:
        with pytest.raises(AppError) as exc:
            normalize_url("https://example.com/" + "a" * 3000)
        assert exc.value.code == ErrorCode.INVALID_URL

    def test_ok_trailing_dot_normalized(self) -> None:
        url = normalize_url("https://example.com./path?q=1")
        assert url == "https://example.com/path?q=1"


class TestSsidIpBlocks:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "169.254.0.1",
            "0.0.0.0",
            "100.64.0.1",
            "198.18.0.1",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
        ],
    )
    def test_blocked(self, ip: str) -> None:
        assert ip_is_blocked(ip) is True

    @pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
    def test_allowed(self, ip: str) -> None:
        assert ip_is_blocked(ip) is False


class TestValidateUrlAsync:
    async def test_public_ok(self) -> None:
        url = await validate_url_async("https://public.example.com/a.pdf", resolver=_resolver)
        assert url == "https://public.example.com/a.pdf"

    @pytest.mark.parametrize(
        "host",
        [
            "private.example.com",
            "loopback.example.com",
            "linklocal.example.com",
            "ipv6-ula.example.com",
        ],
    )
    async def test_blocked_hosts(self, host: str) -> None:
        with pytest.raises(AppError) as exc:
            await validate_url_async(f"https://{host}/x", resolver=_resolver)
        assert exc.value.code == ErrorCode.INVALID_URL

    async def test_mixed_ips_rejected(self) -> None:
        """任一解析 IP 命中内网即拒绝（防止 DNS 轮询绕过）。"""
        with pytest.raises(AppError) as exc:
            await validate_url_async("https://multi.example.com/x", resolver=_resolver)
        assert exc.value.code == ErrorCode.INVALID_URL

    async def test_dns_failure_rejected(self) -> None:
        with pytest.raises(AppError) as exc:
            await validate_url_async(
                "https://nx.example.com/x",
                resolver=lambda h: (_ for _ in ()).throw(OSError("no such host")),
            )
        assert exc.value.code == ErrorCode.INVALID_URL


class TestContentType:
    def test_kinds(self) -> None:
        assert content_type_kind("text/html; charset=utf-8") == "html"
        assert content_type_kind("application/pdf") == "pdf"
        assert content_type_kind("application/rss+xml") == "xml"
        assert content_type_kind("application/octet-stream") == "other"

    def test_pdf_magic(self) -> None:
        assert looks_like_pdf(b"%PDF-1.7\n...") is True
        assert looks_like_pdf(b"<html>") is False
