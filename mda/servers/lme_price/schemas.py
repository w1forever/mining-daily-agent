"""lme-price-mcp 数据 Schema（任务书 6.3/6.4）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PricePoint(BaseModel):
    """单个价格数据点。数值字段必须携带单位。"""

    date: str  # ISO 8601 日期（实际报价日期）
    price: float
    currency: str
    unit: str  # 如 USD/metric tonne、CNY/tonne、USD/troy ounce
    price_type: str  # 如 futures_close / monthly_average
    source: str


class PriceSnapshot(BaseModel):
    """get_price 输出：目标日期报价。

    区分请求日期与实际报价日期：非交易日/数据延迟时
    actual_price_date != requested_date，且必须显式标记，绝不
    用相邻日价格静默冒充目标日期报价。
    """

    commodity: str
    requested_date: str
    actual_price_date: str | None = None
    price: float | None = None
    currency: str = ""
    unit: str = ""
    price_type: str = ""
    source: str = ""
    source_url: str = ""
    retrieved_at: str = ""
    is_delayed: bool = True  # 数据源自身是否延迟发布
    is_estimated: bool = False  # True 表示并非目标日真实报价（最近可用报价）


class PriceTrend(BaseModel):
    """get_trend 输出：历史价格走势。

    change_percent = (end_price - start_price) / start_price × 100；
    start_price 为 0 时 change_percent 为 null 并携带 warning。
    """

    commodity: str
    requested_days: int
    actual_start_date: str | None = None
    actual_end_date: str | None = None
    start_price: float | None = None
    end_price: float | None = None
    change_absolute: float | None = None
    change_percent: float | None = None
    currency: str = ""
    unit: str = ""
    price_type: str = ""
    data_points: list[PricePoint] = Field(default_factory=list)
    source: str = ""
    source_url: str = ""
    retrieved_at: str = ""
