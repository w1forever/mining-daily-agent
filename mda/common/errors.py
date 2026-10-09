"""统一错误码与业务异常。

所有 MCP 工具返回的错误都必须使用这里的 ErrorCode，保证机器可识别。
禁止将异常堆栈作为业务响应暴露给调用方。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """机器可识别的错误码（契约 3.2）。"""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_URL = "INVALID_URL"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    PDF_PARSE_FAILED = "PDF_PARSE_FAILED"
    RESOURCE_TABLE_NOT_FOUND = "RESOURCE_TABLE_NOT_FOUND"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    MCP_CONNECTION_FAILED = "MCP_CONNECTION_FAILED"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    UNSUPPORTED_COMMODITY = "UNSUPPORTED_COMMODITY"
    # ---- 扩展错误码 ----
    UPSTREAM_ERROR = "UPSTREAM_ERROR"  # 上游 5xx / 响应无法解析
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 数据源整体不可用（网络被阻断等）
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    MODEL_CONFIG_MISSING = "MODEL_CONFIG_MISSING"
    CONTENT_TYPE_UNEXPECTED = "CONTENT_TYPE_UNEXPECTED"

    @property
    def retryable_by_default(self) -> bool:
        """按错误类别给出默认重试建议（调用方可覆盖）。"""
        return self in {
            ErrorCode.UPSTREAM_TIMEOUT,
            ErrorCode.UPSTREAM_RATE_LIMITED,
            ErrorCode.UPSTREAM_ERROR,
            ErrorCode.UPSTREAM_UNAVAILABLE,
            ErrorCode.MCP_CONNECTION_FAILED,
        }


class AppError(Exception):
    """携带机器可识别错误码的业务异常。

    不重试类错误（参数非法、无权限、文档不存在等）与可重试类错误
    通过 ``retryable`` 区分，见任务书 7.5。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code.retryable_by_default if retryable is None else retryable
        self.details = details or {}

    def __repr__(self) -> str:  # 日志中不暴露堆栈细节
        return (
            f"AppError(code={self.code.value!r}, message={self.message!r}, "
            f"retryable={self.retryable!r})"
        )
