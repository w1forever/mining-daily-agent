"""Planner：LLM 结构化计划 + 确定性兜底（任务书 7.3）。

- 结构化输出用 Pydantic Schema 约束（with_structured_output）。
- LLM 返回非法 JSON / 校验失败时重试一次，再失败使用确定性兜底计划，
  保证系统在 LLM 异常时仍可运行。
- 计划中的 PDF URL 一律来自证据注册表（known_pdfs），不信任 LLM 生成的 URL。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from mda.agent.prompts import PLANNER_FALLBACK, PLANNER_SYSTEM
from mda.agent.state import AgentState
from mda.common.errors import ErrorCode
from mda.common.logging import get_logger

log = get_logger(__name__)

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")


class PlanModel(BaseModel):
    subject: str = Field(description="查询主体，如 'Pilbara 锂矿'")
    commodity: str = Field(description="主矿种，如 lithium")
    date_range_days: int = Field(default=7, description="新闻回溯天数")
    news_queries: list[str] = Field(description="新闻检索词列表（中英文均可）")
    need_resources: bool = Field(default=True, description="是否需要矿产资源量")
    need_prices: bool = Field(default=True, description="是否需要价格数据")
    price_commodities: list[str] = Field(
        default_factory=list,
        description="价格商品规范名列表（lithium_carbonate/copper/aluminum/nickel/zinc/tin/lead），不带说明文字",
    )
    tools: list[str] = Field(default_factory=list, description="需要的 MCP 工具")
    notes: str = ""


def validate_plan(plan: PlanModel) -> PlanModel:
    if not plan.subject.strip():
        raise ValueError("subject 为空")
    if not plan.news_queries:
        raise ValueError("news_queries 为空")
    if not (1 <= plan.date_range_days <= 30):
        raise ValueError("date_range_days 越界")
    return plan


def fallback_plan() -> dict[str, Any]:
    return dict(PLANNER_FALLBACK)


class Planner:
    """解析用户请求 -> 结构化执行计划。"""

    def __init__(self, llm: BaseChatModel) -> None:
        self.llm = llm

    async def build_plan(self, state: AgentState | dict[str, Any]) -> dict[str, Any]:
        query = state["user_query"]
        report_date = state.get("report_date", "")
        # 已知可信 NI 43-101 PDF 注册表（agent 侧配置，禁止 LLM 自造 URL）
        registry = state.get("known_pdfs", [])
        registry_desc = json.dumps(registry, ensure_ascii=False, indent=2)

        user_prompt = (
            f"用户问题：{query}\n"
            f"报告日期：{report_date}\n"
            "可信 NI 43-101 PDF 注册表（如需资源量只能从这里选择，"
            f"没有匹配项时 need_resources 保持 True 并注明缺失）：\n{registry_desc}"
        )
        for attempt in (1, 2):
            try:
                plan = await self._llm_plan(user_prompt)
                validate_plan(plan)
                return plan.model_dump()
            except Exception as exc:  # noqa: BLE001 - 任何 LLM 异常都走重试/兜底
                log.warning(
                    "planner_failed",
                    attempt=attempt,
                    error=str(exc)[:200],
                    error_code=ErrorCode.MODEL_RESPONSE_INVALID.value,
                )
        fallback = fallback_plan()
        fallback["notes"] = "LLM 规划失败，使用确定性兜底计划"
        return fallback

    async def _llm_plan(self, user_prompt: str) -> PlanModel:
        try:
            structured = self.llm.with_structured_output(PlanModel)
            result = await structured.ainvoke([("system", PLANNER_SYSTEM), ("user", user_prompt)])
            return result if isinstance(result, PlanModel) else PlanModel.model_validate(result)
        except Exception:  # noqa: BLE001, S110 - 端点不支持结构化输出时退回 JSON 解析
            pass
        # 兜底：请求 JSON 并手工解析（兼容不支持 function-calling 的端点）
        resp = await self.llm.ainvoke(
            [
                ("system", PLANNER_SYSTEM + "\n只输出 JSON，不要任何其他文字。"),
                ("user", user_prompt),
            ]
        )
        text = str(resp.content if hasattr(resp, "content") else resp)
        return PlanModel.model_validate(self._extract_json(text))

    @staticmethod
    def _extract_json(text: str) -> dict:
        fenced = JSON_FENCE_RE.search(text)
        if fenced:
            return json.loads(fenced.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("响应中未找到 JSON")
