"""VLM 兜底抽取（任务书 5.3 第三阶段 / P1）。

触发条件（仅候选页）：
- 扫描版 PDF（文本层缺失）
- pdfplumber 未提取到任何资源量行
- 表格列错位/多级表头导致解析失败
- 文本与表格解析结果矛盾

实现：PyMuPDF 渲染候选页 -> PNG -> 多模态模型（默认 qwen-vl-plus，
OpenAI 兼容协议）-> 约束 JSON 输出 -> Pydantic 校验。
模型输出解析失败时重试一次，仍失败抛 MODEL_RESPONSE_INVALID。
页面图像与模型输出均视为不可信数据，仅作结构化抽取输入。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx
import pymupdf

from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger
from mda.common.settings import Settings
from mda.servers.mineral_pdf.parser import RawTableRow

log = get_logger(__name__)

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

VLM_PROMPT = """You are a data-extraction engine for mineral resource tables in technical reports.

Extract ONLY rows that appear in resource/reserve tables on this page image.
Return a JSON array of objects. Each object:
{
  "category": one of Measured|Indicated|Inferred|Measured+Indicated|Proven|Probable|Proven+Probable,
  "is_aggregate": true if the row is a total/combined row (e.g. "Measured + Indicated"),
  "ore_tonnage": number (no commas, may be null),
  "ore_unit": "Mt"|"kt"|"t" as printed,
  "grade": number (may be null),
  "grade_unit": as printed, e.g. "% Li2O" or "g/t",
  "contained_metal": number (may be null),
  "metal_unit": as printed, e.g. "kt LCE" or "Moz",
  "evidence_text": exact row text as printed, joined by " | "
}
Rules:
- Do NOT invent numbers. If a cell is blank or unreadable, use null and note it in evidence_text.
- Do NOT include note lines or footnote text as rows.
- If no resource table exists on this page, return [].
- Output ONLY the JSON array, no prose, no markdown fences.
"""


class VLMExtractor:
    """候选页图像 -> 结构化资源量行。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.vlm_model
        self.base_url = settings.vlm_base_url
        self.api_key = settings.effective_vlm_key
        self.timeout = settings.llm_timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _render_pages(self, pdf_path: Path, pages: list[int], zoom: float = 2.0) -> list[str]:
        images: list[str] = []
        doc = pymupdf.open(str(pdf_path))
        try:
            for pno in pages:
                page = doc[pno]
                mat = pymupdf.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                images.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        finally:
            doc.close()
        return images

    async def _call_vlm(self, images_b64: list[str]) -> list[dict]:
        content: list[dict] = [{"type": "text", "text": VLM_PROMPT}]
        for img in images_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img}"},
                }
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 4096,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout, connect=10.0),
                    trust_env=False,
                ) as client:
                    resp = await client.post(
                        f"{self.base_url.rstrip('/')}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
            except httpx.TimeoutException as exc:
                raise AppError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    "VLM 请求超时",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise AppError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"VLM 请求失败: {exc}",
                    retryable=True,
                ) from exc
            if resp.status_code == 429:
                raise AppError(
                    ErrorCode.UPSTREAM_RATE_LIMITED,
                    "VLM 接口频控",
                    retryable=True,
                )
            if resp.status_code == 401 or resp.status_code == 403:
                raise AppError(
                    ErrorCode.AUTH_REQUIRED,
                    "VLM 接口鉴权失败（请检查 VLM_API_KEY）",
                    retryable=False,
                )
            if resp.status_code != 200:
                raise AppError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"VLM 接口返回 {resp.status_code}",
                    retryable=True,
                )
            try:
                body = resp.json()
                text = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as exc:
                raise AppError(
                    ErrorCode.MODEL_RESPONSE_INVALID,
                    "VLM 响应结构异常",
                    retryable=False,
                ) from exc
            parsed = self._parse_json_array(text)
            if parsed is not None:
                return parsed
            log.warning("vlm_invalid_json", attempt=attempt)
        raise AppError(
            ErrorCode.MODEL_RESPONSE_INVALID,
            "VLM 两次尝试均未返回合法 JSON",
            retryable=False,
        )

    @staticmethod
    def _parse_json_array(text: str) -> list[dict] | None:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        return [item for item in parsed if isinstance(item, dict)]

    async def extract_from_pages(
        self, pdf_path: Path, pages: list[int], *, page_offset: int = 0
    ) -> list[RawTableRow]:
        """渲染候选页并请求 VLM 结构化抽取。pages 为 0 基页码。"""
        if not self.available:
            raise AppError(
                ErrorCode.AUTH_REQUIRED,
                "未配置 VLM_API_KEY，无法启用 VLM 兜底",
                retryable=False,
            )
        images = self._render_pages(pdf_path, pages)
        rows_data = await self._call_vlm(images)
        rows: list[RawTableRow] = []
        for idx, item in enumerate(rows_data):
            category = str(item.get("category", "")).strip()
            if not category:
                continue
            rows.append(
                RawTableRow(
                    deposit_name=str(item.get("deposit_name", "")),
                    table_title="VLM 抽取（页面图像）",
                    page_number=pages[min(idx, len(pages) - 1)] + 1,
                    category=category,
                    is_aggregate=bool(item.get("is_aggregate", False)),
                    ore_tonnage=item.get("ore_tonnage"),
                    ore_unit=str(item.get("ore_unit", "")),
                    grade=item.get("grade"),
                    grade_unit=str(item.get("grade_unit", "")),
                    contained_metal=item.get("contained_metal"),
                    metal_unit=str(item.get("metal_unit", "")),
                    evidence_text=str(item.get("evidence_text", ""))[:400],
                )
            )
        return rows
