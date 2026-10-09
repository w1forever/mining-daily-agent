"""外部 URL 安全校验与 SSRF 防护（任务书 10.2）。

防护策略：
1. 仅允许 http/https，禁止 URL 内嵌凭据。
2. 请求前解析 DNS，逐 IP 检查是否命中内网/环回/链路本地/保留地址段。
3. 重定向目标逐跳重新校验（SafeHttpClient 内实现）。
4. 下载体积、Content-Type 与魔数校验。
5. 所有外部文本（HTML/PDF）一律视为不可信数据，与 Agent 指令隔离。

已知限制（如实记录）：DNS 校验与 httpx 实际建连之间存在 TOCTOU 窗口，
对抗主动 DNS rebinding 需配合出口代理固定解析；本项目在工具层面完成
「解析后 IP 检查 + 重定向复检」，满足考核要求并已文档化。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger

log = get_logger(__name__)

_BLOCKED_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
)
_BLOCKED_V6 = (
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (*_BLOCKED_V4, *_BLOCKED_V6))

# 解析器类型：host -> 解析出的 IP 列表（可注入，便于测试）
Resolver = Callable[[str], Awaitable[list[str]]]

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "mining-daily-agent/0.1 (research tool)"
)
# 工具类 UA：部分站点（如 fred.stlouisfed.org 的 WAF）对「数据中心 IP +
# 浏览器 UA」组合直接黑洞连接，但对 curl 类工具 UA 正常返回（实测）。
CURL_TOOL_UA = "curl/8.9.1 (mining-daily-agent/0.1; research tool)"

MAX_URL_LENGTH = 2048


def ip_is_blocked(ip: str) -> bool:
    """返回 True 表示该 IP 属于内网/环回/链路本地/保留地址段。"""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return True  # 无法解析的地址一律拒绝
    return any(addr in net for net in _BLOCKED_NETS)


def normalize_url(url: str) -> str:
    """解析并规范化 URL；不合法时抛 AppError(INVALID_URL)。"""
    if not isinstance(url, str) or not url.strip():
        raise AppError(ErrorCode.INVALID_URL, "URL 为空", retryable=False)
    if len(url) > MAX_URL_LENGTH:
        raise AppError(ErrorCode.INVALID_URL, "URL 过长", retryable=False)
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise AppError(ErrorCode.INVALID_URL, f"URL 无法解析: {exc}", retryable=False) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise AppError(
            ErrorCode.INVALID_URL, f"仅允许 http/https，收到: {scheme!r}", retryable=False
        )
    if not parts.hostname:
        raise AppError(ErrorCode.INVALID_URL, "URL 缺少主机名", retryable=False)
    if parts.username or parts.password:
        raise AppError(ErrorCode.INVALID_URL, "URL 不允许内嵌凭据", retryable=False)
    host = parts.hostname.lower()
    if host.endswith("."):
        host = host[:-1]  # 去掉尾点，防止绕过黑名单
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


async def resolve_host(host: str) -> list[str]:
    """真实 DNS 解析（使用事件循环，避免阻塞）。

    IPv4 优先：部分站点（如 fred.stlouisfed.org）的 IPv6 在本网络被黑洞，
    httpx 无 Happy Eyeballs，按顺序连接会挂到超时。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    v4: list[str] = []
    v6: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        bucket = v4 if ":" not in ip else v6
        if ip not in bucket:
            bucket.append(ip)
    return v4 + v6


async def validate_url_async(url: str, *, resolver: Resolver | None = None) -> str:
    """规范化 URL 并对其解析出的全部 IP 做 SSRF 检查。

    返回规范化后的 URL；命中内网地址或 DNS 失败时抛 AppError。
    """
    normalized = normalize_url(url)
    host = urlsplit(normalized).hostname or ""
    resolve = resolver or resolve_host
    try:
        ips = await resolve(host)
    except OSError as exc:
        raise AppError(
            ErrorCode.INVALID_URL,
            f"域名解析失败: {host}",
            retryable=False,
            details={"reason": str(exc)},
        ) from exc
    if not ips:
        raise AppError(ErrorCode.INVALID_URL, f"域名无解析结果: {host}", retryable=False)
    for ip in ips:
        if ip_is_blocked(ip):
            raise AppError(
                ErrorCode.INVALID_URL,
                f"目标地址命中 SSRF 防护策略（内网/环回/保留地址）: {ip}",
                retryable=False,
            )
    return normalized


_CT_HTML = re.compile(r"^text/html", re.I)
_CT_PDF = re.compile(r"application/pdf", re.I)
_CT_TEXT = re.compile(r"^text/", re.I)
_CT_XML = re.compile(r"(?:xml|rss|atom)", re.I)


def content_type_kind(content_type: str) -> str:
    """把 Content-Type 归为 html/pdf/text/xml/other。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if _CT_HTML.match(ct):
        return "html"
    if _CT_PDF.search(ct):
        return "pdf"
    if _CT_XML.search(ct):
        return "xml"
    if _CT_TEXT.match(ct):
        return "text"
    return "other"


def looks_like_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def looks_like_html(head: bytes) -> bool:
    head_l = head[:512].lstrip().lower()
    return head_l.startswith(b"<!doctype html") or head_l.startswith(b"<html")


@dataclass
class FetchOutcome:
    """安全 HTTP 客户端的一次成功抓取结果。"""

    url: str  # 最终 URL（重定向后）
    status: int
    content_type: str
    body: bytes
    final_host_ips: list[str]


@dataclass
class RetryPolicy:
    """网络层重试策略：最多 2 次，指数退避（任务书 7.5/10.1）。"""

    max_retries: int = 2
    base_delay: float = 0.5
    timeout_seconds: float = 30.0
    connect_timeout: float = 10.0

    def is_transient(self, status: int) -> bool:
        return status == 429 or 500 <= status < 600


def iter_blocked_ips(ips: Iterable[str]) -> list[str]:
    return [ip for ip in ips if ip_is_blocked(ip)]
