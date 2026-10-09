"""API 合同测试（FastAPI TestClient，Mock MCP 环境）。

验证：/health、/health/dependencies、/report 正常与错误路径
（含空 query → 400 INVALID_ARGUMENT 的修复回归）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mda.api.main import create_app
from mda.common.settings import Settings


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MDA_MOCK_PROVIDERS", "1")  # Mock 数据源（明确标注）
    settings = Settings(
        log_level="WARNING",
        llm_provider="mock",
        mcp_transport="stdio",
        news_cache_dir=str(tmp_path / "cache" / "news"),
        price_cache_dir=str(tmp_path / "cache" / "price"),
        pdf_cache_dir=str(tmp_path / "cache" / "pdf"),
        extract_cache_dir=str(tmp_path / "cache" / "extract"),
        reports_dir=str(tmp_path / "reports"),
    )
    app = create_app(settings)
    return TestClient(app)


class TestHealth:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_dependencies(self, client: TestClient) -> None:
        r = client.get("/health/dependencies")
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == "ok"
        servers = {s["server"]: s for s in payload["mcp_servers"]}
        assert set(servers) == {"mining-news", "mineral-pdf", "lme-price"}
        for s in servers.values():
            assert s["connected"] is True
            assert s["missing_tools"] == []


class TestReportErrors:
    def test_empty_query_returns_400(self, client: TestClient) -> None:
        """修复回归：空 query 必须 400 INVALID_ARGUMENT，不产生外部调用。"""
        r = client.post("/report", json={"query": "   "})
        assert r.status_code == 400
        payload = r.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "INVALID_ARGUMENT"
        assert payload["report_markdown"] == ""
