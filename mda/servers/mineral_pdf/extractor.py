"""资源量抽取编排（任务书 5.3 第五阶段 + 5.4）。

流程：安全下载 -> 文本/表格解析 -> （必要时）VLM 兜底 -> 合并去重 ->
数值校验 -> 统一契约输出。

失败处理原则：找不到表格 / 关键字段缺失 / 单位不明确 / 数据矛盾时，
返回 partial/error + validation_warnings + needs_review，绝不猜测资源量。
"""

from __future__ import annotations

from pathlib import Path

from mda.common.cache import JsonFileCache
from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger
from mda.common.models import ToolResult
from mda.common.security import validate_url_async
from mda.common.settings import Settings
from mda.servers.mineral_pdf.downloader import PdfDownloader
from mda.servers.mineral_pdf.parser import PdfParser, RawTableRow
from mda.servers.mineral_pdf.schemas import ExtractData, ResourceRecord
from mda.servers.mineral_pdf.validator import validate_records
from mda.servers.mineral_pdf.vlm import VLMExtractor

log = get_logger(__name__)

PARSER_VERSION = "parser-v1"
VLM_MAX_PAGES = 6  # 仅对前几个候选页使用 VLM，控制成本与延迟


def make_synthetic_pdf(path: Path) -> None:
    """生成合成测试 PDF（仅 Mock 模式合同测试使用，明确标注为合成数据）。

    绘制带真实网格线的表格，使 pdfplumber（lines 策略）能检测到表格，
    从而让 Mock 模式走完整解析管线。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()

    # 封面信息与表格标题（文本层）
    page.insert_text((72, 60), "SYNTHETIC NI 43-101 TECHNICAL REPORT (TEST FIXTURE)", fontsize=14)
    page.insert_text((72, 82), "Effective Date: 2026-01-31", fontsize=10)
    page.insert_text((72, 110), "Table 1-1: Test Deposit Mineral Resource Estimate", fontsize=10)
    page.insert_text(
        (72, 128),
        "Notes: Mineral Resources have an effective date of January 31, 2026.",
        fontsize=8,
    )
    page.insert_text(
        (72, 140),
        "Synthetic data for MCP contract testing only - NOT real resource data.",
        fontsize=8,
    )

    # 网格表格：列 x 边界与行 y 边界
    cols = [72, 130, 210, 300, 430, 540]
    row_ys = [160, 178, 196, 214, 232, 250]
    top, bottom = row_ys[0], row_ys[-1]
    # 水平线
    for y in row_ys:
        page.draw_line((cols[0], y), (cols[-1], y), color=(0, 0, 0), width=0.6)
    # 垂直线
    for x in cols:
        page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=0.6)

    header = ["Cut-off", "Category", "Tonnage", "Average", "LCE"]
    rows = [
        ["0.5", "Measured", "4,175,000", "1.17", "120.8"],
        ["0.5", "Indicated", "1,390,000", "1.05", "36.1"],
        ["0.5", "Measured + Indicated", "5,565,000", "1.14", "156.9"],
        ["0.5", "Inferred", "900,000", "0.95", "21.2"],
    ]
    # 表头行（多级表头：第二行带单位）
    header2 = ["Li2O (%)", "", "(t)", "Grade Li2O (%)", "(Kt)"]
    for ci, (h1, h2) in enumerate(zip(header, header2, strict=True)):
        x = cols[ci] + 4
        page.insert_text((x, top + 10), h1, fontsize=7)
        if h2:
            page.insert_text((x, top + 18), h2, fontsize=6)
    for ri, row in enumerate(rows):
        y = row_ys[ri + 1] + 12
        for ci, cell in enumerate(row):
            x = cols[ci] + 4
            page.insert_text((x, y), cell, fontsize=7)
    doc.save(str(path))
    doc.close()


class ExtractionService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        mock: bool = False,
        resolver=None,  # 可注入 DNS 解析器（测试用）
    ) -> None:
        self.settings = settings or Settings()
        self.mock = mock
        self.resolver = resolver
        self.downloader = PdfDownloader(self.settings, resolver=resolver)
        self.parser = PdfParser(max_pages=self.settings.pdf_max_pages)
        self.vlm = VLMExtractor(self.settings)
        self.cache = JsonFileCache(self.settings.resolved_dir(self.settings.extract_cache_dir))

    async def extract_resources(self, pdf_url: str) -> ToolResult:
        # 1. URL 安全校验（可注入解析器）
        try:
            await validate_url_async(pdf_url, resolver=self.resolver)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False)

        # Mock 模式：合成 PDF 走完整解析管线（明确标注为合成数据）
        if self.mock:
            return await self._extract_mock(pdf_url)

        # 2. 安全下载（错误码映射在 downloader 内）
        try:
            downloaded = await self.downloader.download(pdf_url)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=exc.retryable, source=pdf_url)

        # 3. 抽取结果缓存（按文档哈希 + 解析器版本）
        cache_key = f"{downloaded.sha256}|{PARSER_VERSION}"
        cached = self.cache.get(cache_key)
        if cached is not None and "extract" in cached:
            data = ExtractData.model_validate(cached["extract"])
            return ToolResult.ok(
                data,
                source=pdf_url,
                is_cached=True,
                warnings=cached.get("warnings") or None,
            )

        # 4. 文本/表格解析
        try:
            report = self.parser.parse(downloaded.path)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False, source=pdf_url)

        rows: list[RawTableRow] = list(report.rows)
        method = "text_tables"

        # 5. VLM 兜底触发条件（任务书 5.3 第三阶段）
        vlm_used = False
        vlm_warning = ""
        if not rows and self.settings.pdf_use_vlm and self.vlm.available:
            pages = report.candidate_pages[:VLM_MAX_PAGES]
            if pages:
                try:
                    vlm_rows = await self.vlm.extract_from_pages(downloaded.path, pages)
                    rows = vlm_rows
                    method = "vlm_only"
                    vlm_used = True
                except AppError as exc:
                    vlm_warning = f"VLM 兜底失败: {exc.message}"

        if not rows:
            warnings = []
            if vlm_warning:
                warnings.append(vlm_warning)
            if not report.candidate_pages:
                return ToolResult.fail(
                    ErrorCode.RESOURCE_TABLE_NOT_FOUND,
                    "未在文档中找到资源量/储量表格（关键词页面定位无候选页）",
                    retryable=False,
                    source=pdf_url,
                    warnings=warnings,
                )
            return ToolResult.fail(
                ErrorCode.RESOURCE_TABLE_NOT_FOUND,
                "定位到候选页但未能解析出资源量行（可能为扫描版或复杂表头）"
                + (f"；{vlm_warning}" if vlm_warning else ""),
                retryable=False,
                source=pdf_url,
                warnings=warnings,
            )

        # 6. 合并去重（文本 vs VLM：按 deposit+category+page+tonnage）
        if vlm_used:
            rows = self._merge_rows(report.rows, rows)
            method = "text_tables+vlm"
        # 跨页去重：同一 (deposit, category, tonnage, grade, metal) 只保留
        # 首次出现（技术报告中摘要章节通常在前，正文重复引用在后）。
        rows = self._dedupe_across_pages(rows)

        # 7. 构建记录 + 校验
        distinct_effective = sorted({r.effective_date for r in rows if r.effective_date})
        records = [
            ResourceRecord(
                property_name=report.report_title or "",
                deposit_name=r.deposit_name,
                report_date=report.issue_date,
                effective_date=r.effective_date or report.effective_date,
                resource_category=r.category,
                ore_tonnage=r.ore_tonnage,
                ore_unit=r.ore_unit,
                grade=r.grade,
                grade_unit=r.grade_unit,
                contained_metal=r.contained_metal,
                metal_unit=r.metal_unit,
                commodity=self._detect_commodity(r.grade_unit, r.metal_unit),
                source_pdf_url=pdf_url,
                page_number=r.page_number,
                table_title=r.table_title,
                evidence_text=r.evidence_text,
                validation_status="validated",
                is_aggregate=r.is_aggregate,
            )
            for r in rows
        ]
        outcome = validate_records(
            records,
            report_effective_date=report.effective_date,
            distinct_effective_dates=distinct_effective,
        )

        needs_review = any(r.validation_status == "needs_review" for r in outcome.records)
        warnings = list(outcome.warnings)
        if vlm_warning:
            warnings.append(vlm_warning)

        data = ExtractData(
            pdf_url=pdf_url,
            sha256=downloaded.sha256,
            report_standard=report.standard,
            report_title=report.report_title,
            property_name=report.report_title,
            effective_date=report.effective_date,
            issue_date=report.issue_date,
            records=outcome.records,
            needs_review=needs_review,
            extraction_method=method,
            pages_processed=report.candidate_pages[:VLM_MAX_PAGES],
            warnings=warnings,
        )

        # 8. 标准披露：JORC 不得伪装为 NI 43-101（任务书 5.5）
        if report.standard == "JORC":
            data.warnings.append(
                "该报告采用 JORC 标准，不是 NI 43-101；本工具能力验收以 NI 43-101 "
                "为准，请勿将 JORC 数据标注为 NI 43-101。"
            )
        elif report.standard == "unknown":
            data.warnings.append("未能确认报告采用 NI 43-101 标准，请人工核对披露标准。")

        self.cache.set(
            cache_key,
            {"extract": data.model_dump(), "warnings": warnings},
        )
        return ToolResult.ok(
            data,
            source=pdf_url,
            warnings=data.warnings or None,
        )

    async def _extract_mock(self, pdf_url: str) -> ToolResult:
        """合成 PDF 全管线解析（仅合同测试）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            synthetic = Path(tmp) / "synthetic.pdf"
            make_synthetic_pdf(synthetic)
            report = self.parser.parse(synthetic)
        rows = report.rows
        records = [
            ResourceRecord(
                property_name="Synthetic Test Project",
                deposit_name=r.deposit_name or "Test Deposit",
                report_date="2026-02-15",
                effective_date=r.effective_date or "2026-01-31",
                resource_category=r.category,
                ore_tonnage=r.ore_tonnage,
                ore_unit=r.ore_unit,
                grade=r.grade,
                grade_unit=r.grade_unit,
                contained_metal=r.contained_metal,
                metal_unit=r.metal_unit,
                commodity="lithium",
                source_pdf_url=pdf_url,
                page_number=r.page_number,
                table_title=r.table_title,
                evidence_text=r.evidence_text,
                validation_status="validated",
                is_aggregate=r.is_aggregate,
            )
            for r in rows
        ]
        outcome = validate_records(records)
        data = ExtractData(
            pdf_url=pdf_url,
            sha256="synthetic-mock",
            report_standard="NI 43-101",
            report_title="Synthetic NI 43-101 Technical Report (TEST FIXTURE)",
            property_name="Synthetic Test Project",
            effective_date="2026-01-31",
            issue_date="2026-02-15",
            records=outcome.records,
            needs_review=False,
            extraction_method="text_tables",
            pages_processed=[0],
            warnings=[
                "合成测试数据（MDA_MOCK_PROVIDERS=1），仅用于 MCP 协议合同测试，不是真实资源量。"
            ],
        )
        return ToolResult.ok(data, source=pdf_url, warnings=data.warnings)

    # ---- 内部 ----

    @staticmethod
    def _merge_rows(text_rows: list[RawTableRow], vlm_rows: list[RawTableRow]) -> list[RawTableRow]:
        """文本与 VLM 结果合并：文本优先；同键冲突时保留文本并标记。"""
        key = lambda r: (  # noqa: E731
            r.deposit_name,
            r.category,
            r.page_number,
            r.ore_tonnage,
        )
        text_keys = {key(r) for r in text_rows}
        merged = list(text_rows)
        for vr in vlm_rows:
            if key(vr) not in text_keys:
                merged.append(vr)
        return merged

    @staticmethod
    def _dedupe_across_pages(rows: list[RawTableRow]) -> list[RawTableRow]:
        """跨页去重：同键保留页码最小者（摘要章节）。"""
        key = lambda r: (  # noqa: E731
            r.deposit_name,
            r.category,
            r.ore_tonnage,
            r.grade,
            r.contained_metal,
        )
        best: dict[tuple, RawTableRow] = {}
        for r in rows:
            k = key(r)
            if k not in best or r.page_number < best[k].page_number:
                best[k] = r
        return sorted(best.values(), key=lambda r: (r.page_number, r.category))

    @staticmethod
    def _detect_commodity(grade_unit: str, metal_unit: str) -> str:
        gu = (grade_unit or "").lower()
        mu = (metal_unit or "").lower()
        if "li2o" in gu or "lce" in mu:
            return "lithium"
        if "au" in gu or "oz" in mu and "lce" not in mu:
            return "gold"
        if "ag" in gu:
            return "silver"
        if "cu" in gu:
            return "copper"
        return ""
