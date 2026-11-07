from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .fingerprint import stable_id
from .schemas import MarketRuleSnapshot


_SSE_COMMON_STOCK_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZSE_COMMON_STOCK_PREFIXES = ("000", "001", "002", "003", "300", "301")


def normalize_cn_symbol(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:]
    return digits if len(digits) == 6 else ""


def is_supported_cn_common_stock(symbol: str) -> bool:
    """Return whether V1 can safely classify the symbol as a common A-share."""
    digits = normalize_cn_symbol(symbol)
    if not digits:
        return False
    return digits.startswith(
        (*_SSE_COMMON_STOCK_PREFIXES, *_SZSE_COMMON_STOCK_PREFIXES, "4", "8", "92")
    )


def resolve_cn_market_rules(
    symbol: str,
    trade_date: date,
    *,
    previous_close: Decimal | None = None,
    st_status: bool = False,
    st_status_verified: bool = False,
    listing_status: str = "listed",
    listing_status_verified: bool = False,
    instrument_type: str = "unknown",
    instrument_type_verified: bool = False,
    metadata_observed_on: date | None = None,
    suspended: bool = False,
) -> MarketRuleSnapshot:
    digits = normalize_cn_symbol(symbol)
    if not digits:
        raise ValueError(f"invalid A-share symbol: {symbol}")
    venue = "sse" if digits.startswith(("5", "6", "9")) else "szse"
    if digits.startswith(("300", "301")):
        board = "chinext"
        limit = Decimal("0.20")
    elif digits.startswith(("688", "689")):
        board = "star"
        limit = Decimal("0.20")
    elif digits.startswith(("8", "4", "92")):
        board = "bse"
        limit = Decimal("0.30")
        venue = "bse"
    else:
        board = "main"
        limit = Decimal("0.10")
    if st_status:
        limit = Decimal("0.05")
    limit_up = _limit_price(previous_close, Decimal("1") + limit) if previous_close else None
    limit_down = _limit_price(previous_close, Decimal("1") - limit) if previous_close else None
    payload = {
        "symbol": digits,
        "trade_date": trade_date,
        "venue": venue,
        "board": board,
        "limit": limit,
        "previous_close": previous_close,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "st_status": st_status,
        "st_status_verified": st_status_verified,
        "listing_status": listing_status,
        "listing_status_verified": listing_status_verified,
        "instrument_type": instrument_type,
        "instrument_type_verified": instrument_type_verified,
        "metadata_observed_on": metadata_observed_on,
        "suspended": suspended,
        "version": "cn-market-rules.v1",
    }
    return MarketRuleSnapshot(
        snapshot_id=stable_id("market-rules", payload, length=20),
        symbol=digits,
        venue=venue,
        board=board,
        trade_date=trade_date,
        tick_size=Decimal("0.01"),
        buy_lot_size=100,
        price_limit_ratio=limit,
        previous_close=previous_close,
        limit_up=limit_up,
        limit_down=limit_down,
        t_plus_one=True,
        st_status=st_status,
        st_status_verified=st_status_verified,
        listing_status=listing_status,
        listing_status_verified=listing_status_verified,
        instrument_type=instrument_type,
        instrument_type_verified=instrument_type_verified,
        metadata_observed_on=metadata_observed_on,
        suspended=suspended,
        calendar_version="cn-calendar.v1",
        rule_source=(
            "local_stock_index_and_closed_bar"
            if st_status_verified and listing_status_verified and instrument_type_verified
            else "symbol_board_inference"
        ),
        rule_version="cn-market-rules.v1",
    )


def _limit_price(previous_close: Decimal, multiplier: Decimal) -> Decimal:
    return (previous_close * multiplier).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def current_cn_common_stock_metadata(
    symbol: str,
    *,
    trade_date: date,
) -> dict[str, object] | None:
    """Resolve current stock identity without applying it to historical dates."""
    from src.core.trading_calendar import get_effective_trading_date, get_market_now
    from src.data.stock_index_loader import get_index_stock_name

    current_session = get_effective_trading_date(
        "cn",
        current_time=get_market_now("cn"),
    )
    if trade_date != current_session or not is_supported_cn_common_stock(symbol):
        return None
    name = get_index_stock_name(normalize_cn_symbol(symbol))
    if not name:
        return None
    normalized_name = str(name).strip().upper()
    st_status = normalized_name.startswith(("ST", "*ST", "S*ST"))
    return {
        "st_status": st_status,
        "st_status_verified": True,
        "listing_status": "listed",
        "listing_status_verified": True,
        "instrument_type": "common_stock",
        "instrument_type_verified": True,
        "metadata_observed_on": get_market_now("cn").date(),
    }
