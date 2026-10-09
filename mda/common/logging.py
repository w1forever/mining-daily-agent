"""结构化日志（JSON 输出到 stdout，任务书 10.3）。

- 支持 request_id / tool_name / server_name / elapsed_ms / status /
  error_code / retry_count / source 等字段。
- 自动脱敏：任何 key 命中 api_key/token/authorization/secret 的值被替换。
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from typing import Any

import structlog

_SENSITIVE_KEY_PATTERNS = ("key", "token", "secret", "password", "authorization", "credential")


def _redact(_: logging.Logger, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if any(pat in key.lower() for pat in _SENSITIVE_KEY_PATTERNS):
            event_dict[key] = "<redacted>"
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    # 注意：日志一律写 stderr —— stdio 传输下 stdout 只能承载 JSON-RPC 消息。
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact,  # type: ignore[list-item]  # structlog processor 类型桩
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # 压低第三方库噪音
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


@contextlib.contextmanager
def request_context(request_id: str, **extra: Any) -> Iterator[None]:
    """绑定请求级上下文（request_id 等），用于追踪一次完整日报请求。"""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id, **extra)
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
