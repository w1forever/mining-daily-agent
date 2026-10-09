"""商品注册表：别名解析与数据源绑定（任务书 6.5）。

重要事实（已在 DATA_SOURCES.md 中记录）：
- LME 官方价格页对本环境 Cloudflare 阻断，且 LME 不提供无限制免费实时 API；
- 基本金属采用 FRED（世界银行/IMF 汇编的全球月度均价，USD/metric tonne）；
- 锂产品采用广期所 GFEX 碳酸锂期货日线（经新浪财经公开行情接口，CNY/tonne）；
- 锂精矿（spodumene concentrate）与碳酸锂是不同产品、不同计价单位，绝不混用；
- 黄金/白银的 FRED 序列已于 2022 年下架，当前无免费可用源 -> AUTH_REQUIRED 披露。
"""

from __future__ import annotations

from dataclasses import dataclass

from mda.common.errors import AppError, ErrorCode

# 别名 -> 规范商品名
COMMODITY_ALIASES: dict[str, str] = {
    # 中文
    "锂": "lithium_carbonate",
    "锂矿": "lithium_carbonate",
    "锂价": "lithium_carbonate",
    "碳酸锂": "lithium_carbonate",
    "氢氧化锂": "lithium_hydroxide",
    "铜": "copper",
    "铜价": "copper",
    "铝": "aluminum",
    "镍": "nickel",
    "锌": "zinc",
    "锡": "tin",
    "铅": "lead",
    "黄金": "gold",
    "金": "gold",
    "白银": "silver",
    # English
    "lithium": "lithium_carbonate",
    "lithium carbonate": "lithium_carbonate",
    "lithium hydroxide": "lithium_hydroxide",
    "spodumene": "spodumene_concentrate",
    "spodumene concentrate": "spodumene_concentrate",
    "copper": "copper",
    "aluminum": "aluminum",
    "aluminium": "aluminum",
    "nickel": "nickel",
    "zinc": "zinc",
    "tin": "tin",
    "lead": "lead",
    "gold": "gold",
    "silver": "silver",
}


@dataclass(frozen=True)
class CommoditySpec:
    canonical: str
    provider: str  # fred | sina_gfex | none
    series_id: str = ""  # FRED series 或 Sina symbol
    currency: str = ""
    unit: str = ""
    price_type: str = ""
    frequency: str = ""  # monthly | daily
    source_name: str = ""
    source_url: str = ""
    is_delayed: bool = True
    note: str = ""  # 披露口径说明（如 锂精矿≠碳酸锂）


COMMODITY_REGISTRY: dict[str, CommoditySpec] = {
    "copper": CommoditySpec(
        canonical="copper",
        provider="fred",
        series_id="PCOPPUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PCOPPUSDM",
        is_delayed=True,
    ),
    "aluminum": CommoditySpec(
        canonical="aluminum",
        provider="fred",
        series_id="PALUMUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PALUMUSDM",
        is_delayed=True,
    ),
    "nickel": CommoditySpec(
        canonical="nickel",
        provider="fred",
        series_id="PNICKUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PNICKUSDM",
        is_delayed=True,
    ),
    "zinc": CommoditySpec(
        canonical="zinc",
        provider="fred",
        series_id="PZINCUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PZINCUSDM",
        is_delayed=True,
    ),
    "tin": CommoditySpec(
        canonical="tin",
        provider="fred",
        series_id="PTINUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PTINUSDM",
        is_delayed=True,
    ),
    "lead": CommoditySpec(
        canonical="lead",
        provider="fred",
        series_id="PLEADUSDM",
        currency="USD",
        unit="USD/metric tonne",
        price_type="monthly_average",
        frequency="monthly",
        source_name="FRED (St. Louis Fed, IMF global price series)",
        source_url="https://fred.stlouisfed.org/series/PLEADUSDM",
        is_delayed=True,
    ),
    "lithium_carbonate": CommoditySpec(
        canonical="lithium_carbonate",
        provider="sina_gfex",
        series_id="lc0",
        currency="CNY",
        unit="CNY/tonne",
        price_type="futures_close",
        frequency="daily",
        source_name="GFEX 碳酸锂期货（广期所 LC 主力连续，经新浪财经公开行情接口）",
        source_url="https://finance.sina.com.cn/futures/quotes/LC0.shtml",
        is_delayed=False,
        note=(
            "碳酸锂期货价格（电池级碳酸锂交割品）不等于锂精矿（spodumene）价格；"
            "Pilbara 锂矿企业收入与锂精矿挂钩，此处为最接近的公开锂产品报价口径。"
        ),
    ),
    "lithium_hydroxide": CommoditySpec(
        canonical="lithium_hydroxide",
        provider="none",
        currency="",
        unit="",
        price_type="",
        frequency="",
        source_name="",
        source_url="",
        is_delayed=True,
        note="暂无免费可验证的氢氧化锂公开报价源（LME CIF 氢氧化锂报价需授权且本站被阻断）。",
    ),
    "spodumene_concentrate": CommoditySpec(
        canonical="spodumene_concentrate",
        provider="none",
        currency="",
        unit="",
        price_type="",
        frequency="",
        source_name="",
        source_url="",
        is_delayed=True,
        note="锂精矿（6% Li2O, CIF China）现货指数为商业订阅数据（Fastmarkets 等），无免费源。",
    ),
    "gold": CommoditySpec(
        canonical="gold",
        provider="none",
        currency="",
        unit="",
        price_type="",
        frequency="",
        source_name="",
        source_url="",
        is_delayed=True,
        note="FRED 黄金序列已于 2022 年下架（IBA/LBMA 数据移除），当前无免费授权源。",
    ),
    "silver": CommoditySpec(
        canonical="silver",
        provider="none",
        currency="",
        unit="",
        price_type="",
        frequency="",
        source_name="",
        source_url="",
        is_delayed=True,
        note="FRED 白银序列已于 2022 年下架（IBA/LBMA 数据移除），当前无免费授权源。",
    ),
}


def resolve_commodity(name: str) -> CommoditySpec:
    """别名解析 -> 商品规格；未知商品抛 UNSUPPORTED_COMMODITY。"""
    key = (name or "").strip().lower()
    canonical = COMMODITY_ALIASES.get(key, key)
    spec = COMMODITY_REGISTRY.get(canonical)
    if spec is None:
        known = ", ".join(sorted(COMMODITY_REGISTRY))
        raise AppError(
            ErrorCode.UNSUPPORTED_COMMODITY,
            f"不支持的商品: {name!r}（支持: {known}）",
            retryable=False,
        )
    if spec.provider == "none":
        raise AppError(
            ErrorCode.DATA_UNAVAILABLE,
            f"{spec.canonical} 暂无可用行情源: {spec.note}",
            retryable=False,
            details={"commodity": spec.canonical},
        )
    return spec
