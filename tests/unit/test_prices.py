"""价格服务单元测试：变化率/非交易日/零值/缺失/不支持商品（任务书 6.4/13.1）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from mda.common.errors import ErrorCode
from mda.common.settings import Settings
from mda.servers.lme_price.commodities import resolve_commodity
from mda.servers.lme_price.providers import MockPriceProvider
from mda.servers.lme_price.service import PriceService, _trend_change


class TestChangeCalc:
    def test_normal(self) -> None:
        change_abs, change_pct = _trend_change(100.0, 110.0)
        assert change_abs == 10.0
        assert change_pct == 10.0

    def test_zero_start(self) -> None:
        change_abs, change_pct = _trend_change(0.0, 10.0)
        assert change_abs == 10.0
        assert change_pct is None  # 零值起点：涨跌幅无意义

    def test_decline(self) -> None:
        change_abs, change_pct = _trend_change(143480.0, 117300.0)
        assert change_abs == -26180.0
        assert change_pct == pytest.approx(-18.24, abs=0.1)


class TestCommodityResolution:
    def test_aliases(self) -> None:
        assert resolve_commodity("锂").canonical == "lithium_carbonate"
        assert resolve_commodity("锂矿").canonical == "lithium_carbonate"
        assert resolve_commodity("lithium").canonical == "lithium_carbonate"
        assert resolve_commodity("copper").canonical == "copper"

    def test_unknown(self) -> None:
        from mda.common.errors import AppError

        with pytest.raises(AppError) as exc:
            resolve_commodity("unobtainium")
        assert exc.value.code == ErrorCode.UNSUPPORTED_COMMODITY

    def test_lithium_hydroxide_no_free_source(self) -> None:
        from mda.common.errors import AppError

        with pytest.raises(AppError) as exc:
            resolve_commodity("氢氧化锂")
        assert exc.value.code == ErrorCode.DATA_UNAVAILABLE


class TestPriceService:
    """Mock Provider（明确标注：确定性合成价格，仅测试）。"""

    def _service(self, tmp_path: Path) -> PriceService:
        return PriceService(
            Settings(price_cache_dir=str(tmp_path / "cache")),
            mock=True,
        )

    async def test_get_trend_shape(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        result = await svc.get_trend("copper", 30)
        assert result.status in ("success", "partial")
        trend = result.data
        assert trend.commodity == "copper"
        assert trend.start_price is not None and trend.end_price is not None
        assert trend.change_percent is not None
        assert len(trend.data_points) >= 2
        # 同口径：币种/单位一致
        units = {(p.currency, p.unit) for p in trend.data_points}
        assert len(units) == 1

    async def test_get_price_exact_match(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        # Mock 序列连续日期，取中间某日
        target = (date.today() - timedelta(days=3)).isoformat()
        result = await svc.get_price("lithium_carbonate", target)
        assert result.status in ("success", "partial")
        snap = result.data
        assert snap.actual_price_date == target
        assert snap.is_estimated is False
        assert snap.unit == "CNY/tonne"

    async def test_non_trading_day_marked_explicitly(self, tmp_path: Path) -> None:
        """非交易日：返回最近实际报价且 is_estimated=true（绝不静默冒充）。"""
        svc = self._service(tmp_path)

        # 用 FRED 月度数据制造非交易日：请求某月中旬（月度报价只存在月初）
        class _MonthlyMock(MockPriceProvider):
            async def get_series(self, spec, start, end):  # type: ignore[no-untyped-def]
                points = await super().get_series(spec, start, end)
                return [p for p in points if p.date.endswith("-01")]

        svc.providers["fred"] = _MonthlyMock()  # type: ignore[assignment]
        # 上个月中旬：必然是非报价日且早于今天
        first_of_month = date.today().replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        target = last_month.replace(day=15).isoformat()
        result = await svc.get_price("copper", target)
        assert result.status in ("success", "partial")
        snap = result.data
        assert snap.is_estimated is True
        assert snap.actual_price_date != target
        assert snap.actual_price_date is not None

    async def test_unsupported_commodity(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        result = await svc.get_trend("gold", 30)
        assert result.status == "error"
        assert result.error is not None
        assert result.error.code in (
            ErrorCode.DATA_UNAVAILABLE,
            ErrorCode.UNSUPPORTED_COMMODITY,
        )

    async def test_invalid_days(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        result = await svc.get_trend("copper", 0)
        assert result.status == "error"
        assert result.error.code == ErrorCode.INVALID_ARGUMENT

    async def test_invalid_date(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        result = await svc.get_price("copper", "not-a-date")
        assert result.status == "error"
        assert result.error.code == ErrorCode.INVALID_ARGUMENT

    async def test_future_date(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        future = (date.today() + timedelta(days=5)).isoformat()
        result = await svc.get_price("copper", future)
        assert result.status == "error"
        assert result.error.code == ErrorCode.INVALID_ARGUMENT

    async def test_empty_provider_data_unavailable(self, tmp_path: Path) -> None:
        """价格 Provider 返回空数据 -> DATA_UNAVAILABLE（不编造）。"""

        class _EmptyProvider:
            name = "mock_price"

            async def get_series(self, spec, start, end):  # type: ignore[no-untyped-def]
                return []

        svc = self._service(tmp_path)
        svc.providers["fred"] = _EmptyProvider()  # type: ignore[assignment]
        result = await svc.get_trend("copper", 30)
        assert result.status == "error"
        assert result.error.code == ErrorCode.DATA_UNAVAILABLE
