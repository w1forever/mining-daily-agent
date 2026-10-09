"""资源量校验单元测试：换算公式/合计一致性/单位冲突（任务书 5.4/13.1）。"""

from __future__ import annotations

import pytest
from mda.servers.mineral_pdf.schemas import ResourceRecord
from mda.servers.mineral_pdf.validator import (
    expected_contained,
    validate_records,
)


def _record(**kw) -> ResourceRecord:
    base = dict(
        property_name="P",
        deposit_name="D",
        report_date="2026-01-01",
        effective_date="2025-06-30",
        resource_category="Indicated",
        ore_tonnage=1_000_000.0,
        ore_unit="t",
        grade=1.0,
        grade_unit="% Li2O",
        contained_metal=24.7,
        metal_unit="kt LCE",
        commodity="lithium",
        source_pdf_url="https://example.com/r.pdf",
        page_number=36,
        table_title="Test Resource Estimate",
        evidence_text="1.0 | Indicated | 1,000,000 | 1.0 | 24.7",
        validation_status="validated",
    )
    base.update(kw)
    return ResourceRecord(**base)


class TestGoldConversion:
    """黄金: contained_oz ≈ tonnage_t × grade_g_per_t / 31.1035。"""

    def test_formula(self) -> None:
        # 1,000,000 t @ 1.0 g/t = 1,000,000 g = 32,150.75 oz
        oz = expected_contained(1_000_000.0, "t", 1.0, "g/t", "oz")
        assert oz == pytest.approx(1_000_000 / 31.1035, rel=1e-6)

    def test_moz_units(self) -> None:
        oz = expected_contained(1_000_000.0, "t", 31.1035, "g/t", "Moz")
        assert oz == pytest.approx(1.0, rel=1e-6)

    def test_record_passes(self) -> None:
        rec = _record(
            resource_category="Indicated",
            grade=31.1035,
            grade_unit="g/t",
            contained_metal=1.0,
            metal_unit="Moz",
            commodity="gold",
        )
        outcome = validate_records([rec])
        assert outcome.records[0].validation_status == "validated"


class TestPercentMetal:
    """百分比品位: contained_t ≈ tonnage_t × grade% / 100。"""

    def test_formula(self) -> None:
        t = expected_contained(2_000_000.0, "t", 1.5, "%", "t")
        assert t == pytest.approx(30_000.0)

    def test_mt_scaling(self) -> None:
        # 2 Mt @ 1.5% = 30,000 t
        t = expected_contained(2.0, "Mt", 1.5, "%", "t")
        assert t == pytest.approx(30_000.0)


class TestLceConversion:
    """Li2O -> LCE: contained_kt_LCE ≈ tonnage_t × grade_% × 2.473 / 1000。"""

    def test_formula(self) -> None:
        # 10,193,000 t @ 1.59% = 400.8 kt LCE（Sigma 报告原文值）
        kt = expected_contained(10_193_000.0, "t", 1.59, "% Li2O", "kt LCE")
        assert kt == pytest.approx(400.8, rel=0.01)


class TestValidationRules:
    def test_conflicting_conversion_flagged(self) -> None:
        """金属量与品位/矿石量换算不一致 -> needs_review。"""
        rec = _record(contained_metal=999.0)  # 期望 ~24.7，偏差巨大
        outcome = validate_records([rec])
        assert outcome.records[0].validation_status == "needs_review"
        assert any("换算不一致" in w for w in outcome.records[0].validation_warnings)

    def test_missing_tonnage_flagged(self) -> None:
        rec = _record(ore_tonnage=None)
        outcome = validate_records([rec])
        assert outcome.records[0].validation_status == "needs_review"
        assert any("矿石量" in w for w in outcome.records[0].validation_warnings)

    def test_missing_unit_flagged(self) -> None:
        rec = _record(ore_unit="")
        outcome = validate_records([rec])
        assert outcome.records[0].validation_status == "needs_review"

    def test_missing_page_flagged(self) -> None:
        rec = _record(page_number=0)
        outcome = validate_records([rec])
        assert any("页码" in w for w in outcome.records[0].validation_warnings)

    def test_aggregate_vs_detail_consistency(self) -> None:
        """合计行与明细行矿石量一致性（防重复计算）。"""
        detail1 = _record(
            deposit_name="D",
            resource_category="Measured",
            ore_tonnage=100.0,
            grade=1.0,
            contained_metal=2.5,
        )
        detail2 = _record(
            deposit_name="D",
            resource_category="Indicated",
            ore_tonnage=50.0,
            grade=1.0,
            contained_metal=1.2,
        )
        agg = _record(
            deposit_name="D",
            resource_category="Measured+Indicated",
            is_aggregate=True,
            ore_tonnage=150.0,
            grade=1.0,
            contained_metal=3.7,
        )
        outcome = validate_records([detail1, detail2, agg])
        assert not any("合计行" in w for w in outcome.warnings)

    def test_aggregate_conflict_warned(self) -> None:
        detail = _record(resource_category="Measured", ore_tonnage=100.0)
        agg = _record(resource_category="Measured+Indicated", is_aggregate=True, ore_tonnage=9999.0)
        outcome = validate_records([detail, agg])
        assert any("合计行" in w for w in outcome.warnings)

    def test_effective_date_mixing_warned(self) -> None:
        rec = _record()
        outcome = validate_records([rec], distinct_effective_dates=["2019-01-10", "2022-10-31"])
        assert any("生效日期" in w for w in outcome.warnings)

    def test_reserve_category_in_resource_table(self) -> None:
        """资源量表出现 Proven（储量类别）-> 警告。"""
        rec = _record(resource_category="Proven", table_title="Mineral Resource Estimate")
        outcome = validate_records([rec])
        assert any("储量" in w or "Reserve" in w for w in outcome.records[0].validation_warnings)
