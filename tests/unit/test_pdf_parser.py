"""PDF 解析单元测试：表格定位/类别分类/单位检测（任务书 13.1）。

夹具：tests/fixtures/ni43_101_sample.pdf（真实报告，隔离外部网络），
以及 make_synthetic_pdf 生成的合成表格 PDF（明确标注）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mda.servers.mineral_pdf.extractor import make_synthetic_pdf
from mda.servers.mineral_pdf.parser import PdfParser


class TestRealFixtureParsing:
    """真实 NI 43-101 报告（本地夹具）的解析正确性。"""

    @pytest.fixture(scope="class")
    def report(self, ni43_101_pdf_path: Path):
        return PdfParser(max_pages=800).parse(ni43_101_pdf_path)

    def test_standard_detected(self, report) -> None:
        assert report.standard == "NI 43-101"

    def test_effective_date(self, report) -> None:
        assert report.effective_date == "2022-10-31"

    def test_candidate_pages_located(self, report) -> None:
        assert report.candidate_pages, "应定位到资源量表候选页"
        assert 34 in report.candidate_pages  # 摘要章节 1.11（0 基）

    def test_records_extracted(self, report) -> None:
        rows = report.rows
        assert len(rows) >= 19

    def test_xuxa_values_exact(self, report) -> None:
        """与报告原文逐值核对（Xuxa 表，页 36）。"""
        xuxa_measured = [
            r
            for r in report.rows
            if r.deposit_name == "xuxa" and r.category == "Measured" and r.page_number == 36
        ]
        assert len(xuxa_measured) == 1
        r = xuxa_measured[0]
        assert r.ore_tonnage == 10_193_000.0
        assert r.grade == 1.59
        assert r.grade_unit == "% Li2O"
        assert r.contained_metal == 400.8
        assert r.metal_unit == "kt LCE"

    def test_aggregate_rows_detected(self, report) -> None:
        aggs = [r for r in report.rows if r.is_aggregate]
        assert aggs, "应识别 Measured+Indicated 合计行"
        assert all(r.category in ("Measured+Indicated",) for r in aggs)

    def test_reserves_not_mixed_with_resources(self, report) -> None:
        """资源量行不得包含储量类别（Proven/Probable）。"""
        resource_rows = [r for r in report.rows if "reserve" not in r.table_title.lower()]
        assert all(
            r.category not in ("Proven", "Probable", "Proven+Probable") for r in resource_rows
        )


class TestSyntheticGridTable:
    """合成网格表格 PDF（明确标注：合成数据）走完整解析管线。"""

    @pytest.fixture()
    def synthetic_pdf(self, tmp_path: Path) -> Path:
        path = tmp_path / "synthetic.pdf"
        make_synthetic_pdf(path)
        return path

    def test_parse(self, synthetic_pdf: Path) -> None:
        report = PdfParser(max_pages=10).parse(synthetic_pdf)
        assert report.standard == "NI 43-101"
        assert report.effective_date == "2026-01-31"
        rows = report.rows
        assert len(rows) == 4
        measured = next(r for r in rows if r.category == "Measured")
        assert measured.ore_tonnage == 4_175_000.0
        assert measured.ore_unit == "t"
        assert measured.grade == 1.17
        assert measured.grade_unit == "% Li2O"
        assert measured.contained_metal == 120.8
        assert measured.metal_unit == "kt LCE"
        assert any(r.is_aggregate and r.category == "Measured+Indicated" for r in rows)


class TestPdfWithoutResourceTables:
    def test_no_candidate_pages(self, tmp_path: Path) -> None:
        """不含资源量关键词的 PDF -> 无候选页（上层返回 RESOURCE_TABLE_NOT_FOUND）。"""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Quarterly activity report - drill results", fontsize=12)
        page.insert_text(
            (72, 130), "Company operations update, no resource tables here.", fontsize=10
        )
        path = tmp_path / "no_tables.pdf"
        doc.save(str(path))
        doc.close()

        report = PdfParser(max_pages=10).parse(path)
        assert report.candidate_pages == []
        assert report.rows == []


class TestCategoryCanonicalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Measured", "Measured"),
            ("measured", "Measured"),
            ("Measured + Indicated", "Measured+Indicated"),
            ("Inferred", "Inferred"),
            ("Proven", "Proven"),
            ("Probable", "Probable"),
            ("Total", None),
            ("", None),
        ],
    )
    def test_mapping(self, raw: str, expected: str | None) -> None:
        assert PdfParser._canonical_category(raw) == expected
