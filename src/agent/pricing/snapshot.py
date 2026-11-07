from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import pandas as pd

from src.agent.structure import analyze_price_structure, coerce_price_bars
from src.core.trading_calendar import (
    get_effective_trading_date,
    get_market_now,
    is_market_open,
)
from src.services.sequoia_price_rebaser import resolve_amount_volume_cash_price_rebase

from .market_rules import current_cn_common_stock_metadata, resolve_cn_market_rules
from .params import PRICE_PLAN_PARAMS_V1, PricePlanParams
from .schemas import ChipSnapshot, PriceBarSnapshot, PricePlanSnapshot


HistoryLoader = Callable[..., tuple[pd.DataFrame | None, str]]
ChipLoader = Callable[[str, date], dict[str, Any] | None]
MarketRulesLoader = Callable[[str, date], Any]


class PricePlanSnapshotBuilder:
    """Own all IO and as-of normalization before pure price evaluation."""

    def __init__(
        self,
        history_loader: HistoryLoader | None = None,
        chip_loader: ChipLoader | None = None,
        market_rules_loader: MarketRulesLoader | None = None,
        *,
        fallback_to_network: bool = True,
        params: PricePlanParams = PRICE_PLAN_PARAMS_V1,
    ) -> None:
        self._history_loader = history_loader
        self._chip_loader = chip_loader
        self._market_rules_loader = market_rules_loader
        self._fallback_to_network = fallback_to_network
        self._params = params

    def build(self, symbol: str, *, as_of: Any) -> PricePlanSnapshot:
        requested_date = _coerce_date(as_of)
        target_date = resolve_completed_cn_as_of(as_of, requested_date)
        required_daily = (
            self._params.daily_evaluation_bars + self._params.daily_warmup_bars
        )
        loader = self._history_loader or _default_history_loader
        kwargs = {
            "days": required_daily,
            "target_date": target_date,
            "fallback_to_network": self._fallback_to_network,
            "require_target_date": True,
        }
        if self._history_loader is None:
            kwargs["minimum_records"] = required_daily
        frame, source = loader(symbol, **kwargs)
        daily = _normalize_daily_frame(frame, target_date).tail(required_daily)
        if daily.empty:
            return _empty_snapshot(
                symbol,
                requested_date=requested_date,
                actual_date=target_date,
                source=source,
            )
        actual_as_of = daily["date"].iloc[-1].date()
        price_basis, scale_verified = _verify_price_scale(daily)
        bars = _frame_to_bars(daily)
        weekly = _completed_weekly(daily, actual_as_of)
        weekly_bars = _frame_to_bars(weekly)[
            -(self._params.weekly_evaluation_bars + self._params.weekly_warmup_bars):
        ]
        structure = analyze_price_structure(
            coerce_price_bars(daily.to_dict("records")),
            include_raw=False,
        )
        chip = _load_chip(
            self._chip_loader,
            symbol=symbol,
            target_date=target_date,
            actual_as_of=actual_as_of,
        )
        missing_fields: list[str] = []
        if chip is None:
            missing_fields.append("chip_distribution")
        adjustment_factor = _last_non_null(daily, "price_adjustment_factor")
        market_metadata = current_cn_common_stock_metadata(
            symbol,
            trade_date=actual_as_of,
        ) or {}
        # A live provider can lag by one completed session after the calendar
        # target has rolled forward. Reuse today's verified identity metadata
        # for that live fallback, while keeping the rules trade date tied to
        # the actual OHLCV session. Historical replays never enter this path.
        if not market_metadata and actual_as_of < target_date:
            market_metadata = current_cn_common_stock_metadata(
                symbol,
                trade_date=target_date,
            ) or {}
        persisted_rules = (
            self._market_rules_loader(symbol, actual_as_of)
            if self._market_rules_loader is not None
            else None
        )
        market_rules = persisted_rules or resolve_cn_market_rules(
            symbol,
            actual_as_of,
            previous_close=(bars[-2].close if len(bars) >= 2 else None),
            suspended=bars[-1].volume <= 0,
            **market_metadata,
        )
        return PricePlanSnapshot(
            symbol=_normalize_symbol(symbol),
            requested_as_of=requested_date,
            actual_as_of_session=actual_as_of,
            daily_bars=bars,
            weekly_bars=weekly_bars,
            last_cash_price=bars[-1].close,
            price_basis=price_basis,
            price_scale_verified=scale_verified,
            adjustment_factor_semantics=(
                "provider_rebased_to_as_of_cash"
                if adjustment_factor is not None
                else "cash_ohlcv_verified_by_amount_volume"
            ),
            adjustment_factor_version=(
                str(adjustment_factor) if adjustment_factor is not None else None
            ),
            history_source=source or "none",
            history_source_version="history-loader.v1",
            market_rules=market_rules,
            chip=chip,
            structure_evidence=structure,
            missing_fields=missing_fields,
        )


def _default_history_loader(*args: Any, **kwargs: Any) -> tuple[pd.DataFrame | None, str]:
    from src.services.history_loader import load_history_df

    return load_history_df(*args, **kwargs)


def _default_chip_loader(symbol: str, target_date: date) -> dict[str, Any] | None:
    # Current providers do not guarantee historical snapshots. Never call them
    # for a past as-of date and accidentally explain history with today's chips.
    if target_date < date.today():
        return None
    try:
        from src.agent.tools.data_tools import _handle_get_chip_distribution

        return _handle_get_chip_distribution(symbol)
    except Exception:
        return None


def _load_chip(
    loader: ChipLoader | None,
    *,
    symbol: str,
    target_date: date,
    actual_as_of: date,
) -> ChipSnapshot | None:
    payload = (loader or _default_chip_loader)(symbol, target_date)
    if not isinstance(payload, dict) or str(payload.get("status") or "ok") not in {
        "ok",
        "partial",
    }:
        return None
    chip_date = _coerce_optional_date(payload.get("date")) or actual_as_of
    if chip_date > actual_as_of:
        return None
    source = str(payload.get("source") or "chip-provider")
    source_ref = str(
        payload.get("source_ref")
        or f"{source}:{_normalize_symbol(symbol)}:{chip_date.isoformat()}"
    )
    values = {
        key: _positive_decimal(payload.get(key))
        for key in (
            "avg_cost",
            "cost_70_low",
            "cost_70_high",
            "cost_90_low",
            "cost_90_high",
        )
    }
    if not any(value is not None for value in values.values()):
        return None
    return ChipSnapshot(
        trade_date=chip_date,
        source=source,
        source_ref=source_ref,
        **values,
    )


def _normalize_daily_frame(
    frame: pd.DataFrame | None, target_date: date
) -> pd.DataFrame:
    columns = ["date", "open", "high", "low", "close", "volume", "amount"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    normalized = frame.copy()
    if "date" not in normalized.columns:
        normalized = normalized.reset_index()
        if "date" not in normalized.columns:
            normalized = normalized.rename(columns={normalized.columns[0]: "date"})
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in columns[1:]:
        if column not in normalized.columns:
            normalized[column] = None if column == "amount" else 0
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    normalized = normalized[normalized["date"].dt.date <= target_date]
    normalized = normalized.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    positive = (
        (normalized["open"] > 0)
        & (normalized["high"] > 0)
        & (normalized["low"] > 0)
        & (normalized["close"] > 0)
    )
    normalized = normalized[positive]
    return normalized.reset_index(drop=True)


def _completed_weekly(daily: pd.DataFrame, actual_as_of: date) -> pd.DataFrame:
    if daily.empty:
        return daily.copy()
    working = daily.copy()
    working["week"] = working["date"].dt.to_period("W-FRI")
    working["week_end"] = working["week"].map(_last_cn_session_of_week)
    complete = working[working["week_end"] <= actual_as_of]
    if complete.empty:
        return pd.DataFrame(columns=daily.columns)
    aggregation: dict[str, tuple[str, str]] = {
        "date": ("date", "max"),
        "open": ("open", "first"),
        "high": ("high", "max"),
        "low": ("low", "min"),
        "close": ("close", "last"),
        "volume": ("volume", "sum"),
        "amount": ("amount", "sum"),
    }
    return complete.groupby("week", sort=True).agg(**aggregation).reset_index(drop=True)


def _last_cn_session_of_week(period: Any) -> date:
    start = period.start_time.date()
    end = period.end_time.date()
    current = end
    while current >= start:
        if current.weekday() < 5 and is_market_open("cn", current):
            return current
        current -= pd.Timedelta(days=1)
    return end


def _frame_to_bars(frame: pd.DataFrame) -> list[PriceBarSnapshot]:
    bars = []
    for row in frame.to_dict("records"):
        bars.append(
            PriceBarSnapshot(
                trade_date=_coerce_date(row["date"]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row.get("volume") or 0)),
                amount=(
                    Decimal(str(row["amount"]))
                    if pd.notna(row.get("amount")) and float(row.get("amount") or 0) >= 0
                    else None
                ),
                adjustment_factor=(
                    Decimal(str(row["price_adjustment_factor"]))
                    if pd.notna(row.get("price_adjustment_factor"))
                    else None
                ),
            )
        )
    return bars


def _verify_price_scale(frame: pd.DataFrame) -> tuple[str, bool]:
    explicit = _last_non_null(frame, "price_adjustment_basis")
    if explicit is not None:
        text = str(explicit).lower()
        unstable = "unstable" in text or "unknown" in text or "conflict" in text
        return str(explicit), not unstable
    rebase = resolve_amount_volume_cash_price_rebase(frame.to_dict("records"))
    if rebase is None:
        return "unverified", False
    unstable = "unstable" in str(rebase.basis).lower()
    # The history loader already applies any required rebase. Here we only
    # verify the final frame's scale and preserve its provenance.
    return str(rebase.basis), not unstable


def _empty_snapshot(
    symbol: str,
    *,
    requested_date: date,
    actual_date: date,
    source: str,
) -> PricePlanSnapshot:
    return PricePlanSnapshot(
        symbol=_normalize_symbol(symbol),
        requested_as_of=requested_date,
        actual_as_of_session=actual_date,
        daily_bars=[],
        weekly_bars=[],
        last_cash_price=None,
        price_basis="unverified",
        price_scale_verified=False,
        history_source=source or "none",
        market_rules=resolve_cn_market_rules(symbol, actual_date),
        missing_fields=["daily_history", "weekly_history", "cash_price", "chip_distribution"],
    )


def _last_non_null(frame: pd.DataFrame, column: str) -> Any:
    if column not in frame.columns:
        return None
    values = frame[column].dropna()
    return values.iloc[-1] if not values.empty else None


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _coerce_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return _coerce_date(value)
    except Exception:
        return None


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _normalize_symbol(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else str(symbol).strip().upper()


def resolve_completed_cn_as_of(
    value: Any,
    requested_date: date | None = None,
) -> date:
    """Return the latest session whose daily bar may be treated as complete."""
    requested_date = requested_date or _coerce_date(value)
    market_now = get_market_now("cn", current_time=value if isinstance(value, datetime) else None)
    if requested_date < market_now.date():
        return requested_date
    return min(requested_date, get_effective_trading_date("cn", current_time=market_now))
