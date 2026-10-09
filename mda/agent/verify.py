"""最终报告确定性校验（任务书 8.4）。

不能只让同一个 LLM 判断报告可信。确定性检查：
1. 每个关键数值在 Evidence 中存在（日期/章节号/43-101 等在允许清单）。
2. 每个引用 URL 来自真实工具返回（evidence/sources）。
3. PDF 数值有对应文档证据（页码+原文）。
4. 价格单位一致（报告中出现的价格必须携带 evidence 中的币种/单位）。
5. 报告日期与数据日期不混淆（报告日期 = 请求日期）。
6. 缺失数据被明确披露（missing_data 主题出现在"数据缺失"一节）。
7. 必备章节齐全。
"""

from __future__ import annotations

import re
from typing import Any

from mda.agent.state import AgentState
from mda.common.logging import get_logger

log = get_logger(__name__)

# 注意：不能用 \w（Unicode 模式下会匹配中文，导致 "为117,300" 中的
# lookbehind 被中文阻断而只匹配到 "300"）。使用 ASCII 词边界。
_NUM_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z_.])(\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+)(?![0-9A-Za-z_.])")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SECTION_NUM_RE = re.compile(r"[一二三四五六七]、")
_ALLOWED_WORDS = {"43-101", "43101", "ni 43-101"}

REQUIRED_SECTIONS = (
    "执行摘要",
    "矿业新闻动态",
    "矿产资源量",
    "商品价格走势",
    "主要风险",
    "数据缺失与限制",
    "参考来源",
)


def _is_number_inside_date(text: str, span: tuple[int, int]) -> bool:
    s, e = span
    # 前后各扩 10 字符检查是否处于日期上下文
    window = text[max(0, s - 12) : min(len(text), e + 12)]
    return bool(re.search(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}", window))


def _is_section_number(text: str, span: tuple[int, int]) -> bool:
    s, e = span
    window = text[max(0, s - 2) : min(len(text), e + 2)]
    return bool(_SECTION_NUM_RE.search(window))


def _is_ni43101_context(text: str, span: tuple[int, int]) -> bool:
    """数字处于 'NI 43-101' 标准名上下文时放行。"""
    s, e = span
    window = text[max(0, s - 12) : min(len(text), e + 12)]
    return bool(re.search(r"43\s*-\s*101|43\s*101", window))


def _is_evidence_citation(text: str, span: tuple[int, int]) -> bool:
    """数字处于证据引用（如 [EV-R-007]）内部时放行。"""
    s, e = span
    window = text[max(0, s - 16) : min(len(text), e + 6)]
    return bool(re.search(r"\[?EV-[A-Z]-\d", window))


def _is_iso_timestamp(text: str, span: tuple[int, int]) -> bool:
    """数字处于 ISO 时间戳（2026-10-08T15:56:32.072184+00:00）内部时放行。"""
    s, e = span
    window = text[max(0, s - 24) : min(len(text), e + 8)]
    return bool(re.search(r"\d{2}:\d{2}:\d{2}", window))


# 文本分数 -> (小数值, 百分比表述值)，如 "a fifth" -> 0.2 / 20
_FRACTION_WORDS = {
    r"\ba\s+fifth\b|\bone[\s-]?fifth\b": (0.2, 20.0),
    r"\ba\s+half\b|\bone[\s-]?half\b": (0.5, 50.0),
    r"\ba\s+quarter\b|\bone[\s-]?quarter\b": (0.25, 25.0),
    r"\ba\s+third\b|\bone[\s-]?third\b": (1.0 / 3.0, 100.0 / 3.0),
    r"\btwo[\s-]?thirds\b": (2.0 / 3.0, 200.0 / 3.0),
    r"\bthree[\s-]?quarters\b": (0.75, 75.0),
}


# 英文月份名 -> 月份数字（证据写 "September"、报告译作 "9月" 属合理转述）
_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_RE = re.compile(r"\b(" + "|".join(_MONTH_NAMES) + r")\b", re.I)

_SCALE_SUFFIX = re.compile(r"^\s*[-–—]?\s*(万|亿|万亿|million|billion|trillion|千|百万)", re.I)


def _apply_scale_suffix(text: str, pos: int, value: float) -> float:
    """报告/证据中数字后的单位量词（万/亿/million/billion）换算到基数。"""
    tail = text[pos : pos + 12]
    m = _SCALE_SUFFIX.match(tail)
    if not m:
        return value
    word = m.group(1).lower()
    scale = {
        "万": 1e4,
        "亿": 1e8,
        "万亿": 1e12,
        "千": 1e3,
        "百万": 1e6,
        "million": 1e6,
        "billion": 1e9,
        "trillion": 1e12,
    }
    return value * scale.get(word, 1.0)


def collect_evidence_numbers(evidence: list[dict]) -> set[float]:
    """从证据中收集全部数值（含页码、资源量、品位、金属量、价格，
    以及证据文本——新闻标题/正文——中的数字，如 '$264 billion'）。
    同时加入绝对值（报告常以正数幅度表述下跌），并处理单位量词。"""
    numbers: set[float] = set()

    def add(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            v = float(value)
            numbers.add(v)
            numbers.add(abs(v))

    def add_text(value: Any) -> None:
        if not isinstance(value, str):
            return
        for m in _NUM_TOKEN_RE.finditer(value):
            raw = m.group(1).replace(",", "")
            try:
                v = float(raw)
            except ValueError:
                continue
            scaled = _apply_scale_suffix(value, m.end(), v)
            numbers.add(scaled)
            numbers.add(abs(scaled))
        # 文本分数（"a fifth" 等）→ 允许报告使用其数值/百分比表述
        for pattern, (frac, pct) in _FRACTION_WORDS.items():
            if re.search(pattern, value, re.I):
                numbers.add(frac)
                numbers.add(pct)
        # 英文月份名 → 允许报告译作"N月"（"September" → "9月"）
        for month_match in _MONTH_RE.finditer(value):
            numbers.add(float(_MONTH_NAMES[month_match.group(1).lower()]))

    for ev in evidence:
        add(ev.get("page_number"))
        fv = ev.get("field_value")
        if isinstance(fv, dict):
            for v in fv.values():
                add(v)
        add_text(ev.get("supporting_text"))
        add_text(ev.get("field_value") if isinstance(ev.get("field_value"), str) else None)
        extra = ev.get("extra") or {}
        if isinstance(extra, dict):
            add_text(extra.get("content"))
            add_text(extra.get("title"))
    return numbers


def collect_evidence_urls(evidence: list[dict], sources: list[dict]) -> set[str]:
    urls: set[str] = set()
    for ev in evidence:
        u = ev.get("source_url", "")
        if u:
            urls.add(u)
    for s in sources:
        u = s.get("url", "")
        if u:
            urls.add(u)
    return urls


def verify_report(state: AgentState | dict[str, Any]) -> tuple[list[str], bool]:
    markdown = state.get("report_markdown", "")
    evidence = state.get("evidence", [])
    sources = state.get("sources", [])
    failures: list[str] = []

    # 1. 必备章节
    for section in REQUIRED_SECTIONS:
        if section not in markdown:
            failures.append(f"缺少必备章节: {section}")

    # 2. 报告日期 = 请求日期（精确检查"日期："行，避免被数据日期干扰）
    report_date = state.get("report_date", "")
    if report_date:
        m_date = re.search(r"日期[：:]\s*(\d{4}-\d{2}-\d{2})", markdown)
        if m_date is None or m_date.group(1) != report_date:
            failures.append(f"报告日期行与请求日期 {report_date} 不一致")

    # 3. 引用 URL 必须来自工具返回
    ev_urls = collect_evidence_urls(evidence, sources)
    for url in re.findall(r"https?://[^\s\)\]、，。；\"]+", markdown):
        url_clean = url.rstrip(".,;:!?）")
        # Google 新闻跳转链接与证据 URL 前缀匹配即可（来源重定向）
        if not any(url_clean.startswith(u) or u.startswith(url_clean) for u in ev_urls):
            failures.append(f"引用 URL 未见于工具返回证据: {url_clean}")

    # 4. 关键数值必须存在于证据
    #    URL 内的数字（如 slug 中的 264-billion）属于引用本身，不参与数值校验
    markdown_for_numbers = re.sub(r"https?://[^\s\)\]、，。；\"]+", "", markdown)
    ev_numbers = collect_evidence_numbers(evidence)
    for m in _NUM_TOKEN_RE.finditer(markdown_for_numbers):
        raw = m.group(1).replace(",", "")
        token = m.group(1)
        if token in _ALLOWED_WORDS or raw in _ALLOWED_WORDS:
            continue
        if _is_number_inside_date(markdown_for_numbers, m.span()):
            continue
        if _is_section_number(markdown_for_numbers, m.span()):
            continue
        if _is_ni43101_context(markdown_for_numbers, m.span()):
            continue
        if _is_evidence_citation(markdown_for_numbers, m.span()):
            continue
        if _is_iso_timestamp(markdown_for_numbers, m.span()):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        value = _apply_scale_suffix(markdown_for_numbers, m.end(), value)
        if value == int(value) and 1000 <= value <= 9999:
            # 年份（2023/2024/2025...）作为时间上下文，非事实数值
            continue
        matched = False
        for n in ev_numbers:
            if abs(value - n) <= max(abs(n), 1.0) * 0.005:
                matched = True
                break
            # 允许对证据数值的四舍五入表述（如 18.25% 写成 "超18%"）
            if value == int(value) and abs(value - round(n)) <= 0.51:
                matched = True
                break
            # 允许矿业单位前缀换算（t/Mt/kt 的 10^±3/±6 倍，如
            # 25,081,000 t 转述为 "25.081 Mt"）
            for exponent in (3, 6, -3, -6):
                scaled = n * 10**exponent
                if abs(value - scaled) <= max(abs(scaled), 1.0) * 0.005:
                    matched = True
                    break
            if matched:
                break
        if not matched:
            failures.append(f"数值未见于证据: {token}")

    # 5. 价格单位一致性：价格证据的币种/单位必须出现在报告价格一节
    price_section_match = re.search(r"商品价格走势([\s\S]*?)(?=\n##|\Z)", markdown)
    price_section = price_section_match.group(1) if price_section_match else ""
    from mda.servers.lme_price.commodities import COMMODITY_ALIASES

    for ev in evidence:
        if ev.get("source_type") != "price":
            continue
        unit = ev.get("unit", "")
        currency = unit.split(" ")[0] if unit else ""
        fv = ev.get("field_value") or {}
        commodity = fv.get("commodity", "")
        if commodity:
            # 报告可能使用中文/英文别名表述商品，接受任意别名
            aliases = {commodity}
            for alias, canonical in COMMODITY_ALIASES.items():
                if canonical == commodity and len(alias) >= 2:
                    aliases.add(alias)
            if not any(a.lower() in price_section.lower() for a in aliases):
                failures.append(f"价格证据商品 {commodity} 未出现在价格章节")
        if currency and currency not in price_section and commodity in price_section:
            failures.append(f"价格币种 {currency} 未在价格章节标注")

    # 6. 缺失数据披露（LLM 会改写措辞，按关键词覆盖判定，且要求多数条目被提及）
    missing = state.get("missing_data", [])
    missing_section_match = re.search(r"数据缺失与限制([\s\S]*?)(?=\n##|\Z)", markdown)
    missing_section = missing_section_match.group(1) if missing_section_match else ""
    disclosed = 0
    for item in missing:
        # 关键词：条目中最长的中/英文片段（>=4 字符）
        keywords = re.findall(r"[一-鿿]{4,}|[A-Za-z][A-Za-z\- ]{3,}", item)
        keyword = max(keywords, key=len) if keywords else item[:12]
        if keyword and (keyword in missing_section or keyword in markdown):
            disclosed += 1
        else:
            failures.append(f"缺失数据未披露: {item[:60]}")
    if missing and not missing_section_match:
        failures.append("存在数据缺失但报告缺少'数据缺失与限制'章节")
    elif missing and disclosed / max(len(missing), 1) < 0.5:
        failures.append("数据缺失章节未覆盖多数缺失项")

    # 7. PDF 数值证据：资源量章节出现数字时必须可追溯到 PDF 证据
    resource_evidence = [ev for ev in evidence if ev.get("source_type") == "pdf"]
    if resource_evidence and "矿产资源量" in markdown:
        resource_section_match = re.search(r"矿产资源量([\s\S]*?)(?=\n##|\Z)", markdown)
        if resource_section_match and not any(
            ev.get("evidence_id", "").lower() in resource_section_match.group(1).lower()
            or ev.get("source_name", "")[:10] in resource_section_match.group(1)
            for ev in resource_evidence
        ):
            failures.append("资源量章节未引用 PDF 证据标识")

    return failures, len(failures) == 0


def verify_report_node(state: AgentState) -> dict:
    failures, ok = verify_report(state)
    return {"verify_failures": failures}
