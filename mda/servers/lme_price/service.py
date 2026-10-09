"""lme-price-mcp 服务层：get_price / get_trend（任务书 6.3/6.4）。"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from mda.common.cache import JsonFileCache
from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.logging import get_logger
from mda.common.models import ToolResult, iso_now
from mda.common.settings import Settings
from mda.servers.lme_price.commodities import CommoditySpec, resolve_commodity
from mda.servers.lme_price.providers import build_price_providers
from mda.servers.lme_price.schemas import PriceSnapshot, PriceTrend

log = get_logger(__name__)

MAX_TREND_POINTS = 120
LOOKBACK_FOR_SNAPSHOT_DAYS = 40


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"日期格式非法（应为 YYYY-MM-DD）: {value!r}",
            retryable=False,
        ) from exc


def _trend_change(start_price: float, end_price: float) -> tuple[float, float | None]:
    change_abs = round(end_price - start_price, 2)
    if start_price == 0:
        return change_abs, None  # 零值起点：涨跌幅无意义
    return change_abs, round((end_price - start_price) / start_price * 100, 2)


class PriceService:
    def __init__(self, settings: Settings | None = None, *, mock: bool = False) -> None:
        self.settings = settings or Settings()
        self.client = SafeHttpClient(
            connect_timeout=self.settings.http_connect_timeout_seconds,
            read_timeout=self.settings.http_read_timeout_seconds,
        )
        self.providers = build_price_providers(self.settings, self.client, mock=mock)
        self.cache = JsonFileCache(
            self.settings.resolved_dir(self.settings.price_cache_dir),
            ttl=timedelta(hours=self.settings.price_cache_ttl_hours),
        )

    async def _series(self, spec: CommoditySpec, start: date, end: date) -> list:
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise AppError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"数据源未启用: {spec.provider}",
                retryable=False,
            )
        key = f"{spec.provider}|{spec.series_id}|{start.isoformat()}|{end.isoformat()}"
        if provider.name != "mock_price":
            cached = self.cache.get(key)
            if cached is not None and "points" in cached:
                from mda.servers.lme_price.schemas import PricePoint

                points = [PricePoint.model_validate(p) for p in cached["points"]]
                return points
        try:
            points = await asyncio.wait_for(
                provider.get_series(spec, start, end),
                timeout=self.settings.http_read_timeout_seconds + 10,
            )
        except asyncio.TimeoutError as exc:
            raise AppError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"{spec.source_name} 请求超时",
                retryable=True,
            ) from exc
        if provider.name != "mock_price":
            self.cache.set(
                key,
                {"points": [p.model_dump() for p in points], "source": spec.source_name},
            )
        return points

    async def get_price(self, commodity: str, date_str: str) -> ToolResult:
        try:
            spec = resolve_commodity(commodity)
            target = parse_date(date_str)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False)

        today = date.today()
        if target > today:
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT,
                "请求日期不能晚于今天",
                retryable=False,
            )
        start = target - timedelta(days=LOOKBACK_FOR_SNAPSHOT_DAYS)
        try:
            points = await self._series(spec, start, min(target, today))
            if not points:
                # 数据源发布滞后（如 FRED 月度数据滞后数月）：显式扩展回溯窗口
                points = await self._series(spec, target - timedelta(days=220), min(target, today))
        except AppError as exc:
            return ToolResult.fail(
                exc.code, exc.message, retryable=exc.retryable, source=spec.source_name
            )

        exact = [p for p in points if p.date == target.isoformat()]
        if exact:
            p = exact[0]
            snapshot = PriceSnapshot(
                commodity=spec.canonical,
                requested_date=target.isoformat(),
                actual_price_date=p.date,
                price=p.price,
                currency=p.currency,
                unit=p.unit,
                price_type=p.price_type,
                source=p.source,
                source_url=spec.source_url,
                retrieved_at=iso_now(),
                is_delayed=spec.is_delayed,
                is_estimated=False,
            )
            return ToolResult.ok(snapshot, source=spec.source_name)

        if points:
            # 非交易日/数据缺失：返回最近可用报价，但显式标记 is_estimated，
            # 绝不静默冒充目标日期报价（任务书 6.3）。
            nearest = points[-1]
            snapshot = PriceSnapshot(
                commodity=spec.canonical,
                requested_date=target.isoformat(),
                actual_price_date=nearest.date,
                price=nearest.price,
                currency=nearest.currency,
                unit=nearest.unit,
                price_type=nearest.price_type,
                source=nearest.source,
                source_url=spec.source_url,
                retrieved_at=iso_now(),
                is_delayed=spec.is_delayed,
                is_estimated=True,
            )
            return ToolResult.ok(
                snapshot,
                source=spec.source_name,
                warnings=[
                    f"{target.isoformat()} 无实际报价（非交易日或数据未发布），"
                    f"返回最近实际报价 {nearest.date}，已显式标记 is_estimated=true"
                ],
            )
        return ToolResult.fail(
            ErrorCode.DATA_UNAVAILABLE,
            f"{spec.canonical} 在 {target.isoformat()} 前后无可用报价",
            retryable=False,
            source=spec.source_name,
        )

    async def get_trend(self, commodity: str, days: int) -> ToolResult:
        try:
            spec = resolve_commodity(commodity)
        except AppError as exc:
            return ToolResult.fail(exc.code, exc.message, retryable=False)
        if not isinstance(days, int) or not (1 <= days <= 730):
            return ToolResult.fail(
                ErrorCode.INVALID_ARGUMENT,
                "days 必须是 1~730 的整数",
                retryable=False,
            )

        end = date.today()
        start = end - timedelta(days=days)
        try:
            points = await self._series(spec, start, end)
            window_extended = False
            if len(points) < 2:
                # 数据源发布滞后：窗口最多扩展一倍，且显式警告（日期如实标注）
                points = await self._series(spec, end - timedelta(days=days * 2), end)
                window_extended = True
        except AppError as exc:
            return ToolResult.fail(
                exc.code, exc.message, retryable=exc.retryable, source=spec.source_name
            )

        if len(points) < 2:
            return ToolResult.fail(
                ErrorCode.DATA_UNAVAILABLE,
                f"{spec.canonical} 区间内数据点不足（{len(points)} 个），无法计算趋势",
                retryable=False,
                source=spec.source_name,
            )

        first, last = points[0], points[-1]
        change_abs, change_pct = _trend_change(first.price, last.price)
        # 同口径（同币种/单位/价格类型）由 provider 保证；此处显式断言
        if first.currency != last.currency or first.unit != last.unit:
            return ToolResult.fail(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                "趋势序列内币种/单位不一致，拒绝计算",
                retryable=False,
            )
        trend = PriceTrend(
            commodity=spec.canonical,
            requested_days=days,
            actual_start_date=first.date,
            actual_end_date=last.date,
            start_price=first.price,
            end_price=last.price,
            change_absolute=change_abs,
            change_percent=change_pct,
            currency=first.currency,
            unit=first.unit,
            price_type=first.price_type,
            data_points=points[-MAX_TREND_POINTS:],
            source=first.source,
            source_url=spec.source_url,
            retrieved_at=iso_now(),
        )
        warnings: list[str] = []
        if window_extended:
            warnings.append(
                f"数据源发布滞后，趋势窗口已显式扩展至 "
                f"{trend.actual_start_date}~{trend.actual_end_date}"
            )
        if change_pct is None:
            warnings.append("起点价格为 0，涨跌幅无法计算（change_percent=null）")
        if spec.note:
            warnings.append(spec.note)
        return ToolResult.ok(trend, source=spec.source_name, warnings=warnings or None)
