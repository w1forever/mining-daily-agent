"""统一数据契约（任务书 3.1）。

所有 MCP 工具都返回 ``ToolResult[T]`` 结构：
- success：data 为业务数据，error 为 null
- partial：data 存在但可能不完整，必须携带 warnings
- error：data 为 null，error 携带机器可识别错误码

时间一律为带时区的 ISO 8601；数值字段必须携带单位。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from mda.common.errors import ErrorCode


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """带时区的 ISO 8601 时间字符串（UTC）。"""
    return utc_now().isoformat()


class ToolError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict = Field(default_factory=dict)


class ToolMetadata(BaseModel):
    source: str = ""
    retrieved_at: str = Field(default_factory=iso_now)
    is_cached: bool = False


class ToolResult(BaseModel):
    """统一工具返回契约。"""

    status: Literal["success", "partial", "error"] = "success"
    data: object | None = None
    error: ToolError | None = None
    metadata: ToolMetadata = Field(default_factory=ToolMetadata)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def ok(
        cls,
        data: object,
        *,
        source: str = "",
        is_cached: bool = False,
        warnings: list[str] | None = None,
    ) -> ToolResult:
        status: Literal["success", "partial"] = "partial" if warnings else "success"
        return cls(
            status=status,
            data=data,
            metadata=ToolMetadata(source=source, is_cached=is_cached),
            warnings=warnings or [],
        )

    @classmethod
    def fail(
        cls,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        source: str = "",
        details: dict | None = None,
        warnings: list[str] | None = None,
    ) -> ToolResult:
        if not isinstance(code, ErrorCode):
            raise ValueError(f"非法错误码（必须是 ErrorCode 枚举）: {code!r}")
        return cls(
            status="error",
            data=None,
            error=ToolError(
                code=code,
                message=message,
                retryable=code.retryable_by_default if retryable is None else retryable,
                details=details or {},
            ),
            metadata=ToolMetadata(source=source),
            warnings=warnings or [],
        )

    def to_mcp_text(self) -> str:
        """序列化为 MCP TextContent 的 JSON 文本。"""
        return self.model_dump_json()


def tool_result_from_dict(payload: dict) -> ToolResult:
    """从工具调用结果反序列化（Agent 侧使用）。"""
    return ToolResult.model_validate(payload)
