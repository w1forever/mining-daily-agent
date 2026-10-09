"""PDF 文本与表格解析（任务书 5.3 第二阶段）。

- PyMuPDF 文本提取 + 关键词页面定位
- pdfplumber 表格提取
- 表头列映射（Cut-off/Category/Tonnage/Grade/Contained）
- 类别识别（Resources: Measured/Indicated/Inferred；Reserves: Proven/Probable）
- 合计行识别（如 "Measured + Indicated"），防止重复计算
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from mda.common.errors import AppError, ErrorCode
from mda.common.logging import get_logger
from mda.servers.mineral_pdf.schemas import KNOWN_CATEGORIES

log = get_logger(__name__)

# 关键词权重（用于候选页打分）
PAGE_KEYWORDS: dict[str, float] = {
    "mineral resource": 5.0,
    "mineral resources": 5.0,
    "resource estimate": 4.0,
    "indicated": 2.0,
    "inferred": 2.0,
    "measured": 1.5,
    "tonnage": 1.0,
    "grade": 1.0,
    "contained": 1.0,
    "cut-off": 1.5,
    "mineral reserve": -3.0,  # 储量章节单独处理，不混入资源量
    "mineral reserves": -3.0,
}

# 类别识别（表格行内文本）
_CAT_RE = re.compile(
    r"^(measured\s*\+\s*indicated|proven\s*\+\s*probable|measured|indicated|"
    r"inferred|proven|probable)$",
    re.I,
)
_TOTAL_RE = re.compile(r"total|combined|measured\s*\+\s*indicated|proven\s*\+\s*probable", re.I)

# 表标题
_TABLE_CAPTION_RE = re.compile(
    r"table\s+[\w.-]*\s*:\s*(?P<title>[^\n]+?)\s*(resource estimate|mineral resource"
    r"|mineral reserve|reserve estimate)",
    re.I,
)
_DEPOSIT_RE = re.compile(
    r"(?P<deposit>[\w\s.'’\-–]+?)\s+(deposit|project)\s+(mineral resource estimate"
    r"|mineral reserve estimate)",
    re.I,
)

# 报告元信息
_STANDARD_RE = re.compile(r"NI\s*43-?101", re.I)
_JORC_RE = re.compile(r"\bJORC\b", re.I)
_EFFECTIVE_RE = re.compile(
    r"(?:effective\s+date[:\s]+|mineral\s+resources\s+have\s+an\s+effective\s+date\s+of\s+)"
    r"(?P<date>(?:\d{1,2}\s+)?(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)
_ISSUE_RE = re.compile(
    r"(?:issue\s+date[:\s]+|issued?[:\s]+)(?P<date>(?:\d{1,2}\s+)?(?:january|february|"
    r"march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?"
    r"\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_UNIT_TONNAGE_RE = re.compile(r"\b(Mt|kt|Kt|t)\b")
_GRADE_UNITS = ("%", "g/t", "ppm")
_METAL_UNITS = ("Moz", "koz", "oz", "Kt", "kt", "Mt", "t", "Mlb", "lb")


@dataclass
class RawTableRow:
    """解析出的表格行（未校验）。"""

    deposit_name: str = ""
    table_title: str = ""
    page_number: int = 0
    category: str = ""
    is_aggregate: bool = False
    ore_tonnage: float | None = None
    ore_unit: str = ""
    grade: float | None = None
    grade_unit: str = ""
    contained_metal: float | None = None
    metal_unit: str = ""
    evidence_text: str = ""
    is_reserve: bool = False
    effective_date: str = ""  # 表级生效日期（从表后注释提取）


@dataclass
class ParseReport:
    rows: list[RawTableRow] = field(default_factory=list)
    candidate_pages: list[int] = field(default_factory=list)
    table_count: int = 0
    standard: str = "unknown"
    report_title: str = ""
    effective_date: str = ""
    issue_date: str = ""
    full_text_len: int = 0


def _to_number(text: str) -> float | None:
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date_iso(match: re.Match | None) -> str:
    if match is None:
        return ""
    raw = match.group("date")
    raw = raw.strip()
    try:
        from datetime import datetime

        for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %B, %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        return raw
    except Exception:  # noqa: BLE001
        return raw


class PdfParser:
    """PDF 文本/表格解析器。"""

    def __init__(self, max_pages: int = 400) -> None:
        self.max_pages = max_pages

    def parse(self, pdf_path: Path) -> ParseReport:
        report = ParseReport()
        try:
            doc = pymupdf.open(str(pdf_path))
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.PDF_PARSE_FAILED,
                f"PDF 打开失败: {exc}",
                retryable=False,
            ) from exc
        try:
            if doc.page_count > self.max_pages:
                raise AppError(
                    ErrorCode.PDF_PARSE_FAILED,
                    f"PDF 页数 {doc.page_count} 超过上限 {self.max_pages}",
                    retryable=False,
                )
            if doc.is_encrypted:
                raise AppError(
                    ErrorCode.AUTH_REQUIRED,
                    "PDF 已加密，无法解析",
                    retryable=False,
                )
            pages_text: dict[int, str] = {}
            for pno in range(min(doc.page_count, self.max_pages)):
                try:
                    text = doc[pno].get_text()
                except Exception:  # noqa: BLE001 - 单页失败不中断整体
                    text = ""
                pages_text[pno] = text
                report.full_text_len += len(text)

            full_text = "\n".join(pages_text.values())
            report.standard = self._detect_standard(full_text)
            report.effective_date = _parse_date_iso(_EFFECTIVE_RE.search(full_text))
            report.issue_date = _parse_date_iso(_ISSUE_RE.search(full_text))

            # 首页取报告标题（封面标题行）
            report.report_title = self._detect_title(pages_text.get(0, ""))

            report.candidate_pages = self._locate_candidate_pages(pages_text)
            if not report.candidate_pages:
                return report  # 未找到目标表格 -> RESOURCE_TABLE_NOT_FOUND 由上层处理

            import pdfplumber

            with pdfplumber.open(str(pdf_path)) as pdf:
                for pno in report.candidate_pages:
                    page = pdf.pages[pno]
                    try:
                        found = page.find_tables()
                    except Exception:  # noqa: BLE001, S112 - 单页解析失败不中断整体
                        continue
                    if not found:
                        continue
                    page_text = pages_text[pno]
                    captions = self._caption_positions(doc[pno], page_text)
                    for table in found:
                        report.table_count += 1
                        try:
                            cells = table.extract()
                        except Exception:  # noqa: BLE001, S112 - 单表解析失败不中断整体
                            continue
                        rows = self._parse_table(cells, pno, page_text, table.bbox, captions)
                        report.rows.extend(rows)
            return report
        finally:
            doc.close()

    # ---- 内部 ----

    @staticmethod
    def _detect_standard(full_text: str) -> str:
        if _STANDARD_RE.search(full_text):
            return "NI 43-101"
        if _JORC_RE.search(full_text):
            return "JORC"
        return "unknown"

    @staticmethod
    def _detect_title(page0_text: str) -> str:
        lines = [ln.strip() for ln in page0_text.splitlines() if ln.strip()]
        if not lines:
            return ""
        # 封面通常前几行是报告名（大写/长标题）
        candidates = [ln for ln in lines[:8] if 8 <= len(ln) <= 120]
        return candidates[0] if candidates else ""

    @staticmethod
    def _locate_candidate_pages(pages_text: dict[int, str]) -> list[int]:
        scored: list[tuple[int, float]] = []
        for pno, text in pages_text.items():
            text_l = text.lower()
            score = 0.0
            for kw, weight in PAGE_KEYWORDS.items():
                if kw in text_l:
                    score += weight
            # 表格标题行是强信号
            if _TABLE_CAPTION_RE.search(text_l):
                score += 4.0
            if score >= 6.0:
                scored.append((pno, score))
        scored.sort(key=lambda x: -x[1])
        return [p for p, _ in scored]

    def _parse_table(
        self,
        table: list[list[str | None]],
        pno: int,
        page_text: str,
        bbox: tuple | None = None,
        captions: list[tuple[float, str]] | None = None,
    ) -> list[RawTableRow]:
        """解析单个表格。

        真实 NI 43-101 报告中 pdfplumber 的表头单元格与数据单元格列索引
        经常错位（表头文字与数字列的 x 对齐不一致）。因此采用稳健策略：
        1) 表头行仅用于确定「字段顺序」与「单位」；
        2) 数据行按非空单元格顺序映射：类别 -> 类别之前为 cut-off，
           之后按表头字段顺序依次为 tonnage/grade/contained，
           数字个数少于字段数时用量级启发式消歧。
        """
        if not table or len(table) < 2:
            return []
        cleaned = [[(c or "").replace("\n", " ").strip() for c in row] for row in table]
        header_idx = self._find_header_row(cleaned)
        if header_idx is None:
            return []
        header = [c.lower() for c in cleaned[header_idx]]
        # 单位行（多级表头第二行）合并进单位检测
        header_plus = list(header)
        if header_idx + 1 < len(cleaned):
            for i, c in enumerate(cleaned[header_idx + 1]):
                if c and i < len(header_plus):
                    header_plus[i] = f"{header_plus[i]} {c.lower()}"

        # 字段顺序（从多级表头合并行构建，从左到右，去重）
        field_order: list[str] = []
        for cell in header_plus:
            field = self._field_of(cell)
            if field is None or field in field_order:
                continue
            field_order.append(field)
        if "category" not in field_order or "tonnage" not in field_order:
            return []

        # 单位：从表头（含第二行）的对应字段列提取
        tonnage_unit = self._unit_from_header(header_plus, "tonnage", ("Mt", "kt", "t"))
        grade_unit = self._unit_from_header(header_plus, "grade")
        metal_unit = self._unit_from_header(header_plus, "contained")

        # 用 bbox 定位本表上方最近的标题（避免整页第一个标题被误配到所有表）
        deposit, title = self._table_context_near(page_text, bbox, captions)

        is_reserve_section = bool(
            re.search(r"mineral\s+reserve", title, re.I) or any("reserve" in c for c in header_plus)
        )

        numeric_fields = [
            f for f in field_order if f in ("cutoff", "tonnage", "grade", "contained")
        ]

        rows: list[RawTableRow] = []
        for row in cleaned[header_idx + 1 :]:
            cells = [c for c in row if c]
            if not cells:
                continue
            # 找类别单元格（可能被拆分，如 "Measured + Indicated" 跨单元格）
            cat_idx = self._find_category_cell(cells)
            if cat_idx is None:
                continue
            category_raw = cells[cat_idx]
            match = _CAT_RE.match(category_raw.strip())
            if match is None:
                continue
            cat_match = match.group(1)
            is_aggregate = bool(_TOTAL_RE.search(cat_match)) or "+" in cat_match
            canonical = self._canonical_category(cat_match)
            if canonical is None:
                continue

            post = cells[cat_idx + 1 :]  # 类别之后的数值

            tonnage = grade = contained = None
            parsed_nums = [_to_number(c) for c in post]
            numbers: list[float] = [n for n in parsed_nums if n is not None]
            data_fields = [f for f in numeric_fields if f != "cutoff"]
            if numbers:
                if len(numbers) >= len(data_fields):
                    assign = dict(zip(data_fields, numbers, strict=False))
                elif len(numbers) == 2 and len(data_fields) == 3:
                    # 缺一个字段：量级启发式区分 [tonnage, grade] 与 [tonnage, contained]
                    if numbers[1] > numbers[0] * 10:
                        assign = {"tonnage": numbers[0], "contained": numbers[1]}
                    else:
                        assign = {"tonnage": numbers[0], "grade": numbers[1]}
                else:
                    assign = dict(zip(data_fields, numbers, strict=False))
                tonnage = assign.get("tonnage")
                grade = assign.get("grade")
                contained = assign.get("contained")

            evidence = " | ".join(cells)[:400]
            rows.append(
                RawTableRow(
                    deposit_name=deposit,
                    table_title=title,
                    page_number=pno + 1,
                    category=canonical,
                    is_aggregate=is_aggregate,
                    ore_tonnage=tonnage,
                    ore_unit=tonnage_unit,
                    grade=grade,
                    grade_unit=grade_unit,
                    contained_metal=contained,
                    metal_unit=metal_unit,
                    evidence_text=evidence,
                    is_reserve=is_reserve_section,
                    effective_date=self._table_effective_date(page_text, title, pno),
                )
            )
        return rows

    @staticmethod
    def _hit(cell: str, key: str) -> bool:
        if len(key) <= 2:
            return re.search(rf"\b{re.escape(key)}\b", cell) is not None
        return key in cell

    @classmethod
    def _field_of(cls, cell: str) -> str | None:
        """识别表头单元格所属字段（cutoff/category/tonnage/grade/contained）。"""
        if cls._hit(cell, "cut-off") or cls._hit(cell, "cutoff"):
            return "cutoff"
        if cls._hit(cell, "category") or cls._hit(cell, "class"):
            return "category"
        if cls._hit(cell, "tonnage") or cls._hit(cell, "tonnes") or cls._hit(cell, "tons"):
            return "tonnage"
        if cls._hit(cell, "lce") or cls._hit(cell, "li2co3"):
            return "contained"
        if (
            cls._hit(cell, "grade")
            or cls._hit(cell, "li2o")
            or cls._hit(cell, "au")
            or cls._hit(cell, "ag")
        ):
            return "grade"
        if cls._hit(cell, "contained") or cls._hit(cell, "metal") or cls._hit(cell, "oz"):
            return "contained"
        return None

    @classmethod
    def _find_category_cell(cls, cells: list[str]) -> int | None:
        """定位类别单元格；"Measured + Indicated" 可能被拆到相邻两个单元格。"""
        for idx, c in enumerate(cells):
            if _CAT_RE.match(c.strip()):
                return idx
        # 拆分情形："Measured +" 与 "Indicated" 相邻
        for idx in range(len(cells) - 1):
            joined = f"{cells[idx]} {cells[idx + 1]}"
            if _CAT_RE.match(joined.strip()):
                cells[idx] = joined
                cells.pop(idx + 1)
                return idx
        return None

    @classmethod
    def _unit_from_header(
        cls, header: list[str], field: str, tonnage_units: tuple[str, ...] = ()
    ) -> str:
        """从表头（含多级表头行）提取指定字段的单位。

        只在该字段自身的单元格内匹配单位，避免从其他字段单元格串取
        （如 Tonnage '(t)' 与 LCE '(Kt)' 混淆）。
        """
        for cell in header:
            if cls._field_of(cell) != field:
                continue
            if field == "tonnage":
                for unit in tonnage_units:
                    if re.search(rf"\({unit}\)", cell, re.I):
                        return unit
            elif field == "grade":
                if cls._hit(cell, "li2o"):
                    return "% Li2O"
                if "g/t" in cell:
                    return "g/t"
                if "ppm" in cell:
                    return "ppm"
                if "%" in cell:
                    return "%"
            elif field == "contained":
                if cls._hit(cell, "lce"):
                    return "kt LCE"
                for unit in _METAL_UNITS:
                    if re.search(rf"\b{re.escape(unit.lower())}\b", cell):
                        return unit
        return ""

    @staticmethod
    def _caption_positions(doc_page: pymupdf.Page, page_text: str) -> list[tuple[float, str]]:
        """用 PyMuPDF search_for 获取每个表格标题的精确 y 坐标。

        pdfplumber 的 page.search 在真实报告中常因换行/字序匹配失败，
        PyMuPDF search_for 按矩形定位更稳健；标题跨行时用矿床名作为检索词。
        """
        out: list[tuple[float, str]] = []
        for m in _TABLE_CAPTION_RE.finditer(page_text.lower()):
            caption = m.group("title").strip()
            dep_m = re.match(r"(?P<dep>[a-z0-9'’\- ]+?)\s+(?:deposit|project)\b", caption, re.I)
            needle = dep_m.group("dep").strip() if dep_m else caption[:40]
            if not needle:
                continue
            try:
                rects = doc_page.search_for(needle)
            except Exception:  # noqa: BLE001, S112 - 标题定位失败降级为整页匹配
                continue
            if not rects:
                continue
            top = min(rect.y0 for rect in rects)
            out.append((top, caption))
        out.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _table_context_near(
        page_text: str,
        bbox: tuple | None,
        captions: list[tuple[float, str]] | None,
    ) -> tuple[str, str]:
        """返回表格上方最近标题的矿床名与标题文本。"""
        title = ""
        deposit = ""
        m2 = _DEPOSIT_RE.search(page_text)
        if m2:
            deposit = m2.group("deposit").strip()
        if captions:
            if bbox is not None and len(bbox) >= 4:
                table_top = bbox[1]
                for top, cap in captions:
                    if top <= table_top:
                        title = cap
                    else:
                        break
            else:
                title = captions[-1][1]
        # 标题形如 "Xuxa Deposit Mineral Resource Estimate" —— 从中提取矿床名
        m_title = re.match(r"(?P<dep>[A-Za-z0-9'’\- ]+?)\s+(?:deposit|project)\b", title, re.I)
        if m_title:
            deposit = m_title.group("dep").strip()
        return deposit, title

    @staticmethod
    def _table_effective_date(page_text: str, title: str, pno: int) -> str:
        """表后注释中的生效日期（如 'effective date of January 10, 2019'）。"""
        if not title:
            return ""
        idx = page_text.lower().find(title.lower())
        if idx < 0:
            return ""
        tail = page_text[idx : idx + 2500]
        m = _EFFECTIVE_RE.search(tail)
        return _parse_date_iso(m) if m else ""

    @staticmethod
    def _find_header_row(rows: list[list[str]]) -> int | None:
        for idx, row in enumerate(rows[:6]):
            joined = " ".join(c.lower() for c in row)
            if "category" in joined and ("tonnage" in joined or "grade" in joined):
                return idx
        return None

    @staticmethod
    def _find_col(
        header: list[str], keys: tuple[str, ...], *, exclude: tuple[str, ...] = ()
    ) -> int | None:
        def hit(cell: str, key: str) -> bool:
            if len(key) <= 2:
                # 短键（au/ag/t 等）要求词边界，避免 "ag" 命中 "Tonnage"
                return re.search(rf"\b{re.escape(key)}\b", cell) is not None
            return key in cell

        for idx, cell in enumerate(header):
            if exclude and any(hit(cell, k) for k in exclude):
                continue
            if any(hit(cell, k) for k in keys):
                return idx
        return None

    @staticmethod
    def _canonical_category(raw: str) -> str | None:
        r = re.sub(r"\s+", "", raw.lower().strip())
        mapping = {
            "measured": "Measured",
            "indicated": "Indicated",
            "inferred": "Inferred",
            "measured+indicated": "Measured+Indicated",
            "proven": "Proven",
            "probable": "Probable",
            "proven+probable": "Proven+Probable",
        }
        canonical = mapping.get(r)
        if canonical is None or canonical not in KNOWN_CATEGORIES:
            return None
        return canonical

    @staticmethod
    def _table_context(page_text: str, pno: int) -> tuple[str, str]:
        """从页面文本提取表格标题与矿床名。"""
        text_l = page_text.lower()
        title = ""
        deposit = ""
        m = _TABLE_CAPTION_RE.search(text_l)
        if m:
            title = m.group("title").strip()
        m2 = _DEPOSIT_RE.search(page_text)
        if m2:
            deposit = m2.group("deposit").strip()
        return deposit, title

    @staticmethod
    def _detect_tonnage_unit(row: list[str], header: list[str], col_tonnage: int | None) -> str:
        # 只在矿石量列表头内匹配单位，防止从其他列（如 LCE (Kt)）串取
        if col_tonnage is not None and col_tonnage < len(header):
            cell = header[col_tonnage]
            for unit in ("Mt", "kt", "t"):
                if re.search(rf"\({unit}\)", cell, re.I):
                    return unit
        joined = " ".join(row)
        m = _UNIT_TONNAGE_RE.search(joined)
        return m.group(1) if m else ""

    @staticmethod
    def _detect_grade_unit(row: list[str], header: list[str], col_grade: int | None) -> str:
        cell = header[col_grade] if col_grade is not None and col_grade < len(header) else ""
        cell_l = cell.lower()
        if "li2o" in cell_l:
            return "% Li2O"
        if "g/t" in cell_l:
            return "g/t"
        if "ppm" in cell_l:
            return "ppm"
        if "%" in cell_l:
            return "%"
        return ""

    @staticmethod
    def _detect_metal_unit(row: list[str], header: list[str], col_contained: int | None) -> str:
        cell = (
            header[col_contained]
            if col_contained is not None and col_contained < len(header)
            else ""
        )
        cell_l = cell.lower()
        if "lce" in cell_l:
            return "kt LCE"
        for unit in _METAL_UNITS:
            if re.search(rf"\b{re.escape(unit.lower())}\b", cell_l):
                return unit
        return ""
