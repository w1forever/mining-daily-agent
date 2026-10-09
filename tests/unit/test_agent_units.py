"""Agent 侧单元测试：报告确定性校验 / Planner 兜底 / 状态（任务书 8.4/13.1）。"""

from __future__ import annotations

import json

from mda.agent.planner import Planner, fallback_plan
from mda.agent.state import new_state
from mda.agent.verify import (
    collect_evidence_numbers,
    verify_report,
)
from mda.servers.lme_price.commodities import COMMODITY_ALIASES


class TestReportVerifier:
    """确定性校验：脚本化证据与报告（明确标注为测试数据）。"""

    def _state(self, markdown: str, **kw) -> dict:
        state = new_state("test", "2026-10-08")
        state["subject"] = "Pilbara 锂矿"
        state["report_markdown"] = markdown
        state["evidence"] = kw.pop("evidence", [])
        state["missing_data"] = kw.pop("missing_data", [])
        state["sources"] = kw.pop("sources", [])
        state.update(kw)
        return state

    def _evidence(self) -> list[dict]:
        return [
            {
                "evidence_id": "EV-P-001",
                "source_type": "price",
                "source_name": "GFEX",
                "source_url": "https://finance.sina.com.cn/futures/quotes/LC0.shtml",
                "document_date": "2026-10-08",
                "field_name": "price_trend",
                "field_value": {
                    "commodity": "lithium_carbonate",
                    "start_price": 143480.0,
                    "end_price": 117300.0,
                    "change_absolute": -26180.0,
                    "change_percent": -18.25,
                    "actual_start_date": "2026-09-08",
                    "actual_end_date": "2026-10-08",
                    "price_type": "futures_close",
                },
                "unit": "CNY CNY/tonne",
                "page_number": None,
                "supporting_text": "GFEX",
                "validation_status": "validated",
                "extra": {},
            }
        ]

    def _compliant_markdown(self) -> str:
        return (
            "# Pilbara 锂矿每日简报\n"
            "日期：2026-10-08\n"
            "数据截至：2026-10-08\n\n"
            "## 一、执行摘要\n摘要\n"
            "## 二、矿业新闻动态\n无新闻\n"
            "## 三、矿产资源量\n无资源量数据\n"
            "## 四、商品价格走势\n"
            "碳酸锂期货 117,300 CNY/吨，较 143,480 下跌 26,180（-18.25%）\n"
            "## 五、主要风险\n风险\n"
            "## 六、数据缺失与限制\n"
            "- 未检索到与 Pilbara 锂矿相关的近期新闻\n"
            "## 七、参考来源\n"
            "[GFEX](https://finance.sina.com.cn/futures/quotes/LC0.shtml)\n"
        )

    def test_compliant_report_passes(self) -> None:
        state = self._state(
            self._compliant_markdown(),
            evidence=self._evidence(),
            missing_data=["未检索到与 Pilbara 锂矿相关的近期新闻（数据缺失）"],
            sources=[{"url": "https://finance.sina.com.cn/futures/quotes/LC0.shtml"}],
        )
        failures, ok = verify_report(state)
        assert ok, failures

    def test_fabricated_number_detected(self) -> None:
        md = self._compliant_markdown().replace(
            "## 一、执行摘要\n摘要",
            "## 一、执行摘要\n资源量高达 9,999,999 吨",
        )
        state = self._state(md, evidence=self._evidence())
        failures, ok = verify_report(state)
        assert not ok
        assert any("999" in f for f in failures)

    def test_fabricated_url_detected(self) -> None:
        md = self._compliant_markdown() + "\n参考 [假来源](https://fake-news.example.com/x)\n"
        state = self._state(md, evidence=self._evidence())
        failures, ok = verify_report(state)
        assert not ok
        assert any("URL" in f for f in failures)

    def test_missing_section_detected(self) -> None:
        md = self._compliant_markdown().replace("## 五、主要风险\n", "")
        state = self._state(md, evidence=self._evidence())
        failures, ok = verify_report(state)
        assert not ok
        assert any("主要风险" in f for f in failures)

    def test_missing_data_not_disclosed(self) -> None:
        md = self._compliant_markdown().replace(
            "- 未检索到与 Pilbara 锂矿相关的近期新闻", "- （空）"
        )
        state = self._state(
            md,
            evidence=self._evidence(),
            missing_data=["铜价数据不可用：无免费行情源"],
        )
        failures, ok = verify_report(state)
        assert not ok
        assert any("缺失" in f for f in failures)

    def test_report_date_required(self) -> None:
        md = self._compliant_markdown().replace("日期：2026-10-08", "日期：1999-01-01")
        state = self._state(md, evidence=self._evidence())
        failures, ok = verify_report(state)
        assert not ok
        assert any("2026-10-08" in f for f in failures)

    def test_number_collection_with_scales(self) -> None:
        """'$264 billion' 与 '2640亿' 应视为同一数值。"""
        ev = [
            {
                "source_type": "news",
                "field_value": "title",
                "supporting_text": "worth $264 billion",
                "extra": {},
            }
        ]
        numbers = collect_evidence_numbers(ev)
        assert 264 * 1e9 in numbers
        assert abs(2640 * 1e8 - 264 * 1e9) < 1e-6


class TestPlannerFallback:
    """LLM 异常时的确定性兜底（脚本化 LLM，明确标注）。"""

    def test_invalid_json_twice_then_fallback(self) -> None:
        from tests.conftest import ScriptedLLM

        llm = ScriptedLLM(responses=["not json at all", "still {not json"], default_response="")
        planner = Planner(llm)
        import asyncio

        plan = asyncio.run(
            planner.build_plan(
                {"user_query": "Pilbara 锂矿", "report_date": "2026-10-08", "known_pdfs": []}
            )
        )
        assert plan["subject"] == "Pilbara 锂矿"
        assert "兜底" in plan["notes"]

    def test_llm_exception_fallback(self) -> None:
        from tests.conftest import ScriptedLLM

        llm = ScriptedLLM(responses=[], default_response="")
        llm.fail_next = True
        planner = Planner(llm)
        import asyncio

        plan = asyncio.run(
            planner.build_plan({"user_query": "x", "report_date": "2026-10-08", "known_pdfs": []})
        )
        assert plan == fallback_plan() or plan["subject"] == "Pilbara 锂矿"

    def test_valid_plan_used(self) -> None:
        from tests.conftest import ScriptedLLM

        llm = ScriptedLLM(
            responses=[
                json.dumps(
                    {
                        "subject": "铜",
                        "commodity": "copper",
                        "date_range_days": 7,
                        "news_queries": ["copper"],
                        "need_resources": False,
                        "need_prices": True,
                        "price_commodities": ["copper"],
                        "tools": ["get_trend"],
                        "notes": "",
                    },
                    ensure_ascii=False,
                )
            ],
            default_response="",
        )
        planner = Planner(llm)
        import asyncio

        plan = asyncio.run(
            planner.build_plan(
                {"user_query": "铜价", "report_date": "2026-10-08", "known_pdfs": []}
            )
        )
        assert plan["commodity"] == "copper"
        assert plan["need_resources"] is False


class TestCommodityAliasCoverage:
    """价格节点防御性归一化（LLM 可能输出带说明文字的商品名）。"""

    def test_alias_extraction(self) -> None:
        from mda.agent.nodes.prices import normalize_commodity_input

        assert (
            normalize_commodity_input("lithium carbonate futures (GFEX, CNY/tonne)")
            == "lithium_carbonate"
        )
        assert normalize_commodity_input("碳酸锂") == "lithium_carbonate"
        assert normalize_commodity_input("lithium_carbonate") == "lithium_carbonate"
        assert normalize_commodity_input("unknown thing") == "unknown thing"

    def test_all_canonical_registered(self) -> None:
        canonicals = set(COMMODITY_ALIASES.values())
        assert "lithium_carbonate" in canonicals
        assert "copper" in canonicals
