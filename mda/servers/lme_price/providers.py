"""价格 Provider 实现（任务书 6.2）。

- FredProvider：FRED CSV（fredgraph.csv），免费无密钥，月度全球均价。
  仅使用已实测验证的 6 个序列（铜/铝/镍/锌/锡/铅）。
- SinaGfexProvider：新浪财经公开行情接口（json.php 纯净 JSON 变体），
  GFEX 碳酸锂主力连续日线。注意：无效 symbol 返回 HTTP 200 + 空数组，
  必须显式检查空数组。
- MockPriceProvider：确定性夹具（仅合同测试，明确标注，默认关闭）。

不得把模拟价格作为真实市场价格输出；模拟数据仅存在于 Mock Provider 且
仅在 MDA_MOCK_PROVIDERS=1 时生效。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from typing import Protocol

from mda.common.errors import AppError, ErrorCode
from mda.common.http_client import SafeHttpClient
from mda.common.logging import get_logger
from mda.common.settings import Settings
from mda.servers.lme_price.commodities import CommoditySpec
from mda.servers.lme_price.schemas import PricePoint

log = get_logger(__name__)

FRED_CSV_TEMPLATE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
SINA_KLINE_TEMPLATE = (
    "https://stock2.finance.sina.com.cn/futures/api/json.php/"
    "InnerFuturesNewService.getDailyKLine?symbol={symbol}"
)


class PriceProvider(Protocol):
    name: str

    async def get_series(self, spec: CommoditySpec, start: date, end: date) -> list[PricePoint]: ...


def _parse_fred_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _round_price(value: float) -> float:
    return round(value, 2)


class FredProvider:
    """FRED 全球商品月度均价（USD）。

    注意：FRED 的 WAF 会黑洞「数据中心 IP + 浏览器 UA」的连接，
    必须使用 curl 类工具 UA（实测，见 DATA_SOURCES.md）。
    """

    name = "fred"

    def __init__(self, client: SafeHttpClient) -> None:
        from mda.common.security import CURL_TOOL_UA

        self.client = SafeHttpClient(
            user_agent=CURL_TOOL_UA,
            connect_timeout=client.connect_timeout,
            read_timeout=client.read_timeout,
            max_bytes=client.max_bytes,
        )

    async def get_series(self, spec: CommoditySpec, start: date, end: date) -> list[PricePoint]:
        url = FRED_CSV_TEMPLATE.format(series_id=spec.series_id)
        try:
            outcome = await self.client.fetch(url, max_bytes=2 * 1024 * 1024, kind_hint="json")
            text = outcome.body.decode("utf-8", errors="replace")
        except AppError as exc:
            raise AppError(
                exc.code,
                f"FRED 数据获取失败（{spec.series_id}）: {exc.message}",
                retryable=exc.retryable,
                details=exc.details,
            ) from exc
        points: list[PricePoint] = []
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or len(header) < 2 or header[0].lower() != "observation_date":
            raise AppError(
                ErrorCode.UPSTREAM_ERROR,
                f"FRED CSV 格式异常（{spec.series_id}）",
                retryable=True,
            )
        for row in reader:
            if len(row) < 2:
                continue
            d = _parse_fred_date(row[0])
            try:
                price = float(row[1])
            except ValueError:
                continue
            if d is None or not (start <= d <= end):
                continue
            points.append(
                PricePoint(
                    date=d.isoformat(),
                    price=_round_price(price),
                    currency=spec.currency,
                    unit=spec.unit,
                    price_type=spec.price_type,
                    source=spec.source_name,
                )
            )
        points.sort(key=lambda p: p.date)
        return points


class SinaGfexProvider:
    """新浪财经公开行情接口：GFEX 期货日线（人民币计价）。

    已知行为（实测）：无效 symbol 返回 HTTP 200 + 空数组 —— 必须显式判空。
    """

    name = "sina_gfex"

    def __init__(self, client: SafeHttpClient) -> None:
        self.client = client

    async def get_series(self, spec: CommoditySpec, start: date, end: date) -> list[PricePoint]:
        url = SINA_KLINE_TEMPLATE.format(symbol=spec.series_id)
        try:
            outcome = await self.client.fetch(url, max_bytes=2 * 1024 * 1024, kind_hint="json")
            text = outcome.body.decode("utf-8", errors="replace")
        except AppError as exc:
            raise AppError(
                exc.code,
                f"行情接口获取失败（{spec.series_id}）: {exc.message}",
                retryable=exc.retryable,
                details=exc.details,
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AppError(
                ErrorCode.UPSTREAM_ERROR,
                f"行情接口返回非 JSON（{spec.series_id}）",
                retryable=True,
            ) from exc
        if not isinstance(payload, list) or not payload:
            raise AppError(
                ErrorCode.DATA_UNAVAILABLE,
                f"行情接口无数据（symbol={spec.series_id}，接口对无效代码也返回 200+空数组）",
                retryable=False,
            )
        points: list[PricePoint] = []
        for bar in payload:
            try:
                d = date.fromisoformat(str(bar["d"])[:10])
                close = float(bar["c"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= d <= end:
                points.append(
                    PricePoint(
                        date=d.isoformat(),
                        price=_round_price(close),
                        currency=spec.currency,
                        unit=spec.unit,
                        price_type=spec.price_type,
                        source=spec.source_name,
                    )
                )
        points.sort(key=lambda p: p.date)
        if not points:
            raise AppError(
                ErrorCode.DATA_UNAVAILABLE,
                f"请求区间内无行情数据（{spec.canonical}，{start}~{end}）",
                retryable=False,
            )
        return points


# ---- 以下为确定性 Mock（仅用于 MCP 协议合同测试，明确标注） ----


def _make_mock_series(spec: CommoditySpec, start: date, end: date, base: float) -> list[PricePoint]:
    points: list[PricePoint] = []
    d = start
    while d <= end:
        drift = 1.0 + 0.01 * ((d - start).days % 7)  # 确定性波动
        points.append(
            PricePoint(
                date=d.isoformat(),
                price=round(base * drift, 2),
                currency=spec.currency,
                unit=spec.unit,
                price_type=spec.price_type,
                source=f"mock:{spec.canonical}",
            )
        )
        d += timedelta(days=1)
    return points


_MOCK_BASES = {
    "copper": 9000.0,
    "aluminum": 2500.0,
    "nickel": 16000.0,
    "zinc": 2900.0,
    "tin": 32000.0,
    "lead": 2000.0,
    "lithium_carbonate": 115000.0,
}


class MockPriceProvider:
    """确定性价格夹具 —— 仅用于合同/单元测试隔离外部依赖，默认关闭。"""

    name = "mock_price"

    async def get_series(self, spec: CommoditySpec, start: date, end: date) -> list[PricePoint]:
        base = _MOCK_BASES.get(spec.canonical)
        if base is None:
            raise AppError(
                ErrorCode.UNSUPPORTED_COMMODITY,
                f"Mock 未覆盖商品: {spec.canonical}",
                retryable=False,
            )
        return _make_mock_series(spec, start, end, base)


def build_price_providers(
    settings: Settings, client: SafeHttpClient, *, mock: bool
) -> dict[str, PriceProvider]:
    if mock:
        p = MockPriceProvider()
        return {name: p for name in ("fred", "sina_gfex")}
    providers: dict[str, PriceProvider] = {}
    if settings.fred_enabled:
        providers["fred"] = FredProvider(client)
    if settings.sina_gfex_enabled:
        providers["sina_gfex"] = SinaGfexProvider(client)
    return providers
