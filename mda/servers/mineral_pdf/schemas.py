"""mineral-pdf-mcp 数据 Schema（任务书 5.2）。

关键原则：
- Resources（资源量）与 Reserves（储量）绝不混为一谈；
- 每条记录必须携带证据文本与页码，禁止返回无证据的数字；
- 单位必须显式标注。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 资源量类别（Resources）
RESOURCE_CATEGORIES = ("Measured", "Indicated", "Inferred", "Measured+Indicated")
# 储量类别（Reserves）
RESERVE_CATEGORIES = ("Proven", "Probable", "Proven+Probable")

KNOWN_CATEGORIES = RESOURCE_CATEGORIES + RESERVE_CATEGORIES


class ResourceRecord(BaseModel):
    property_name: str
    deposit_name: str
    report_date: str = ""  # 报告发布日（issue date）
    effective_date: str = ""  # 资源量生效日（effective date）
    resource_category: str  # Measured/Indicated/Inferred/Proven/Probable 等
    ore_tonnage: float | None = None
    ore_unit: str = ""  # t / kt / Mt
    grade: float | None = None
    grade_unit: str = ""  # % Li2O / g/t Au / ppm 等
    contained_metal: float | None = None
    metal_unit: str = ""  # kt LCE / Moz / koz / t 等
    commodity: str = ""
    source_pdf_url: str = ""
    page_number: int = 0
    table_title: str = ""
    evidence_text: str = ""  # 表格行原文（证据）
    validation_status: str = "validated"  # validated | warning | needs_review
    validation_warnings: list[str] = Field(default_factory=list)
    is_aggregate: bool = False  # True = 合计行（如 Measured+Indicated），防重复计算


class ExtractData(BaseModel):
    pdf_url: str
    sha256: str = ""
    report_standard: str = "unknown"  # "NI 43-101" | "JORC" | "unknown"
    report_title: str = ""
    property_name: str = ""
    effective_date: str = ""
    issue_date: str = ""
    records: list[ResourceRecord] = Field(default_factory=list)
    needs_review: bool = False
    extraction_method: str = ""  # text_tables | text_tables+vlm | vlm_only
    pages_processed: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
