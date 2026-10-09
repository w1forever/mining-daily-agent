"""资源量抽取结果校验（任务书 5.4）。

至少校验：
1. Indicated 与 Inferred 等类别正确区分（Resources/Reserves 不混用）
2. ore_tonnage 为数字
3. grade / metal 单位明确
4. 合计行与明细行不重复计算（合计 vs 明细一致性）
5. 数据来自报告对应页码（页码证据存在）
6. 不同生效日期的数据不混用
7. 数值与原文不发生冲突（换算量级校验）

换算公式（仅用于量级/一致性校验，绝不覆盖原始文档数据）：
- 黄金: contained_oz ≈ tonnage_t × grade_g_per_t / 31.1035
- 百分比品位金属: contained_metal_t ≈ tonnage_t × grade_percent / 100
- Li2O -> LCE: contained_kt_LCE ≈ tonnage_t × grade_%Li2O × 2.473 / 1000
容差 ±10%（考虑报告四舍五入与计量基准差异）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mda.common.logging import get_logger
from mda.servers.mineral_pdf.schemas import ResourceRecord

log = get_logger(__name__)

TOLERANCE = 0.10  # ±10%


def _tonnage_to_tonnes(value: float, unit: str) -> float:
    u = unit.lower()
    if u == "mt":
        return value * 1_000_000
    if u == "kt":
        return value * 1_000
    return value  # t


def _metal_to_base(value: float, unit: str) -> float:
    """金属量 -> 基准单位（oz 或 t，取决于 unit 体系）。"""
    u = unit.lower()
    if "moz" in u:
        return value * 1_000_000
    if "koz" in u:
        return value * 1_000
    return value  # oz 或 t/kt-LCE 体系


def expected_contained(
    tonnage: float, ore_unit: str, grade: float, grade_unit: str, metal_unit: str
) -> float | None:
    """按商品换算规则计算期望金属量（与报告同一单位）。返回 None 表示无适用规则。"""
    tonnes = _tonnage_to_tonnes(tonnage, ore_unit)
    gu = grade_unit.lower()
    mu = metal_unit.lower()

    if "g/t" in gu and "oz" in mu and "lce" not in mu:
        base_oz = tonnes * grade / 31.1035
        return base_oz / _metal_to_base(1.0, mu)  # 换算回报告单位（oz/koz/Moz）
    if "%" in gu and "lce" in mu:
        # kt LCE 基准：金属量按 kt 计
        if "kt" in mu:
            return tonnes * grade / 100 * 2.473 / 1000
        return tonnes * grade / 100 * 2.473
    if "%" in gu:
        # 通用百分比金属：吨
        return tonnes * grade / 100
    return None


@dataclass
class ValidationOutcome:
    records: list[ResourceRecord]
    warnings: list[str]


def validate_records(
    records: list[ResourceRecord],
    *,
    report_effective_date: str = "",
    distinct_effective_dates: list[str] | None = None,
) -> ValidationOutcome:
    warnings: list[str] = []
    out: list[ResourceRecord] = []

    for rec in records:
        rec_warnings: list[str] = []
        status = "validated"

        # 1. 类别与章节一致性：资源量表不得出现储量类别，储量表不得出现资源量类别
        table_is_resource = bool(
            re.search(r"mineral\s+resource", rec.table_title, re.I)
            and not re.search(r"mineral\s+reserve", rec.table_title, re.I)
        )
        table_is_reserve = bool(re.search(r"mineral\s+reserve", rec.table_title, re.I))
        if table_is_resource and rec.resource_category in ("Proven", "Probable", "Proven+Probable"):
            rec_warnings.append("储量类别出现在资源量表中，请人工核对章节归属")
        if table_is_reserve and rec.resource_category in (
            "Measured",
            "Indicated",
            "Inferred",
            "Measured+Indicated",
        ):
            rec_warnings.append("资源量类别出现在储量表中，请人工核对章节归属")

        # 2. 矿石量必须为数字
        if rec.ore_tonnage is None:
            rec_warnings.append("矿石量缺失或非数字")
        # 3. 单位必须明确
        if not rec.ore_unit:
            rec_warnings.append("矿石量单位不明确")
        if rec.grade is not None and not rec.grade_unit:
            rec_warnings.append("品位单位不明确")
        if rec.contained_metal is not None and not rec.metal_unit:
            rec_warnings.append("金属量单位不明确")

        # 4. 页码证据
        if not rec.page_number:
            rec_warnings.append("无法定位证据页码")
        if not rec.evidence_text:
            rec_warnings.append("缺少表格行原文证据")

        # 5. 生效日期
        if not rec.effective_date and report_effective_date:
            rec.effective_date = report_effective_date
        if not rec.effective_date:
            rec_warnings.append("生效日期缺失")

        # 6. 量级一致性（不覆盖原始数据，仅校验）
        if (
            rec.ore_tonnage is not None
            and rec.grade is not None
            and rec.contained_metal is not None
            and rec.ore_unit
            and rec.grade_unit
            and rec.metal_unit
        ):
            expected = expected_contained(
                rec.ore_tonnage, rec.ore_unit, rec.grade, rec.grade_unit, rec.metal_unit
            )
            if expected is not None and expected > 0:
                ratio = rec.contained_metal / expected
                if not (1 - TOLERANCE <= ratio <= 1 + TOLERANCE):
                    rec_warnings.append(
                        f"金属量与品位/矿石量换算不一致（报告 {rec.contained_metal} "
                        f"{rec.metal_unit} vs 换算期望 {expected:.1f}，"
                        f"偏差 {abs(ratio - 1) * 100:.0f}%），"
                        "可能单位口径不同，请人工复核"
                    )
                else:
                    # 量级一致：通过（在 warning 列表不追加任何内容）
                    pass

        if rec_warnings:
            status = "needs_review"
            rec.validation_status = status
            rec.validation_warnings = rec_warnings
        out.append(rec)

    # 7. 合计 vs 明细一致性（防重复计算）
    by_deposit: dict[tuple[str, int], list[ResourceRecord]] = {}
    for rec in out:
        by_deposit.setdefault((rec.deposit_name, rec.page_number), []).append(rec)
    for (deposit, page), recs in by_deposit.items():
        aggregates = [r for r in recs if r.is_aggregate]
        details = [r for r in recs if not r.is_aggregate]
        for agg in aggregates:
            detail_sum = sum(
                (r.ore_tonnage or 0.0)
                for r in details
                if _categories_compatible(r.resource_category, agg.resource_category)
            )
            if detail_sum and agg.ore_tonnage:
                ratio = agg.ore_tonnage / detail_sum
                if not (1 - TOLERANCE <= ratio <= 1 + TOLERANCE):
                    warnings.append(
                        f"{deposit or '未命名矿床'} 第 {page} 页：合计行矿石量 "
                        f"{agg.ore_tonnage} 与明细之和 {detail_sum} 不一致（偏差 "
                        f"{abs(ratio - 1) * 100:.0f}%），请核对是否重复计算或遗漏明细"
                    )

    # 8. 生效日期混用
    if distinct_effective_dates and len(set(distinct_effective_dates)) > 1:
        warnings.append(
            "文中出现多个不同的生效日期（"
            + ", ".join(sorted(set(distinct_effective_dates)))
            + "），请确认是否存在不同日期数据混用"
        )

    return ValidationOutcome(records=out, warnings=warnings)


def _categories_compatible(a: str, b: str) -> bool:
    """判断明细类别是否属于合计类别的组成部分。"""
    if b == "Measured+Indicated":
        return a in ("Measured", "Indicated")
    if b == "Proven+Probable":
        return a in ("Proven", "Probable")
    return a == b
