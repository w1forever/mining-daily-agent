"""pytest 公共夹具。

注意：所有测试双（Mock/Fake/Fixture）均在此明确标注，仅用于隔离外部依赖。
真实外部数据测试在 tests/integration（RUN_LIVE_TESTS=1 时执行）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mda.common.settings import Settings

# ---------- 路径 ----------

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
NI43_101_PDF = FIXTURES / "ni43_101_sample.pdf"
NI43_101_URL_FILE = FIXTURES / "ni43_101_sample.url.txt"


@pytest.fixture(scope="session")
def ni43_101_pdf_path() -> Path:
    assert NI43_101_PDF.exists(), "需要先下载真实 NI 43-101 PDF 夹具（见 tests/fixtures/README.md）"
    return NI43_101_PDF


@pytest.fixture(scope="session")
def ni43_101_pdf_url() -> str:
    if NI43_101_URL_FILE.exists():
        return NI43_101_URL_FILE.read_text(encoding="utf-8").strip()
    return "https://sigmalithiumresources.com/wp-content/uploads/2023/05/2023-01-SGML-Updated-Technical-Report-1.pdf"


# ---------- 设置（隔离目录，避免污染 data/） ----------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """测试设置：所有缓存目录指向临时目录。"""
    return Settings(
        log_level="WARNING",
        llm_provider="mock",
        mcp_transport="stdio",
        news_cache_dir=str(tmp_path / "cache" / "news"),
        price_cache_dir=str(tmp_path / "cache" / "price"),
        pdf_cache_dir=str(tmp_path / "cache" / "pdf"),
        extract_cache_dir=str(tmp_path / "cache" / "extract"),
        reports_dir=str(tmp_path / "reports"),
        news_fetch_timeout_seconds=5.0,
        http_read_timeout_seconds=5.0,
        http_connect_timeout_seconds=2.0,
    )


@pytest.fixture()
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """明确标注：启用 Server 侧确定性 Mock Provider。"""
    monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")


# ---------- 确定性 Fake LLM（明确标注：测试双，脚本化响应） ----------


class ScriptedLLM(BaseChatModel):
    """脚本化 Fake LLM：按输入内容返回预设响应；可编程异常。"""

    responses: list[str] = []  # 顺序响应队列
    default_response: str = ""
    fail_next: bool = False  # 下一次调用抛异常（模拟 LLM 故障）

    @property
    def _llm_type(self) -> str:
        return "scripted-fake-llm"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟 LLM 故障")
        text = self.responses.pop(0) if self.responses else self.default_response
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def _json_response(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


VALID_PLAN = {
    "subject": "Pilbara 锂矿",
    "commodity": "lithium",
    "date_range_days": 7,
    "news_queries": ["Pilbara lithium"],
    "need_resources": True,
    "need_prices": True,
    "price_commodities": ["lithium_carbonate"],
    "tools": ["search", "fetch_article", "extract_resources", "get_price", "get_trend"],
    "notes": "测试计划（脚本化）",
}


@pytest.fixture()
def scripted_llm() -> ScriptedLLM:
    """脚本化 LLM（计划 JSON -> 报告）。"""
    llm = ScriptedLLM()
    llm.responses = [_json_response(VALID_PLAN)]
    llm.default_response = "mock report"
    return llm


class EvidenceAwareReportLLM(ScriptedLLM):
    """根据提示词中的 evidence JSON 生成可通过确定性校验的报告（测试双）。

    明确标注：这是为 e2e 测试编写的脚本化模型，不是真实 LLM。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟 LLM 故障")
        # 修订轮次中证据 JSON 在非末尾消息里，扫描全部消息
        joined = "\n".join(str(m.content if hasattr(m, "content") else m) for m in messages)
        report = build_compliant_report(joined)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=report))])


def build_compliant_report(prompt_text: str) -> str:
    """从提示词中的证据 JSON 构建合规报告（脚本化测试双）。

    锚定 `{"subject"` 开头，避免误匹配提示词模板中的 `{主题}` 等占位符。
    """
    m = re.search(r"\{\s*\"subject\"[\s\S]*\"evidence\"[\s\S]*\}", prompt_text)
    ctx = json.loads(m.group(0)) if m else {}
    evidence = ctx.get("evidence", [])
    sources = ctx.get("sources", [])
    missing = ctx.get("missing_data", [])
    subject = ctx.get("subject", "主题")
    report_date = ctx.get("report_date", "")

    lines = [f"# {subject}每日简报", f"日期：{report_date}", "数据截至：（测试）", ""]
    lines.append("## 一、执行摘要")
    lines.append("基于已获取并验证的信息生成（脚本化测试报告）。")
    lines.append("## 二、矿业新闻动态")
    for ev in evidence:
        if ev.get("source_type") == "news":
            lines.append(f"- {ev.get('field_value', '')} [来源]({ev.get('source_url', '')})")
    lines.append("## 三、矿产资源量")
    for ev in evidence:
        if ev.get("source_type") == "pdf":
            fv = ev.get("field_value") or {}
            lines.append(
                f"- {fv.get('deposit_name', '')} {fv.get('resource_category', '')}: "
                f"{fv.get('ore_tonnage')} {ev.get('unit', '')} "
                f"品位 {fv.get('grade')} {fv.get('grade_unit', '')} "
                f"金属量 {fv.get('contained_metal')} {fv.get('metal_unit', '')} "
                f"(第 {ev.get('page_number', '?')} 页) [{ev.get('evidence_id', '')}]"
            )
    lines.append("## 四、商品价格走势")
    for ev in evidence:
        if ev.get("source_type") == "price" and ev.get("field_name") == "price_trend":
            fv = ev.get("field_value") or {}
            lines.append(
                f"- {fv.get('commodity', '')}: {fv.get('start_price')} -> "
                f"{fv.get('end_price')} {ev.get('unit', '')} "
                f"(变化 {fv.get('change_absolute')}, {fv.get('change_percent')}%)"
            )
    lines.append("## 五、主要风险")
    lines.append("基于证据的风险分析。")
    lines.append("## 六、数据缺失与限制")
    if missing:
        for item in missing:
            lines.append(f"- {item}")
    else:
        lines.append("- 无")
    lines.append("## 七、参考来源")
    for s in sources:
        lines.append(f"- [{s.get('name', '')}]({s.get('url', '')})")
    return "\n".join(lines)


@pytest.fixture()
def evidence_aware_llm() -> EvidenceAwareReportLLM:
    """计划 JSON + 证据感知报告的脚本化 LLM（e2e 用）。"""
    llm = EvidenceAwareReportLLM()
    llm.responses = [_json_response(VALID_PLAN)]
    return llm
