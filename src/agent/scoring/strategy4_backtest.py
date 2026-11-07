"""Signal-level Strategy-4 backtest with explicit data sufficiency rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from src.agent.scoring.strategy4 import compute_strategy4_features, strategy4_signal_triggered
from src.schemas.composite_verdict import BacktestWinRate


BACKTEST_RULE_VERSION = "backtest_win_rate.v1"
BACKTEST_LOOKBACK_DAYS = 365 * 3
BACKTEST_MIN_SAMPLES = 20
BACKTEST_HOLDING_DAYS = 10
BUY_FEE = 0.00025
SELL_FEE = 0.00025
STAMP_DUTY = 0.00050
SLIPPAGE = 0.00100


def evaluate_strategy4_backtest(
    frame: Optional[pd.DataFrame],
    *,
    as_of: Any,
    listing_date: Optional[Any] = None,
    is_st: Optional[bool] = None,
    limit_pct: Optional[float] = None,
) -> BacktestWinRate:
    """Evaluate historical Strategy-4 entry signals without future leakage.

    Listing/board metadata is mandatory for the A-share exclusion rules. When
    it is unavailable, the result is explicitly ``insufficient_data``.
    """
    target = _coerce_date(as_of)
    if frame is None or frame.empty:
        return _result("insufficient_data", "history_missing")
    if listing_date is None or is_st is None or limit_pct is None:
        return _result("insufficient_data", "listing_or_limit_metadata_missing")

    bars = _normalize(frame, target)
    start = target - timedelta(days=BACKTEST_LOOKBACK_DAYS)
    if bars.empty or bars["date"].iloc[0].date() > start:
        return _result("insufficient_data", "three_year_history_missing")
    if bool(is_st):
        return _result("insufficient_data", "st_stock_excluded")

    listing = _coerce_date(listing_date)
    signals = []
    skipped = {"limit_up": 0, "suspended": 0, "listing_age": 0}
    for index in range(len(bars) - BACKTEST_HOLDING_DAYS):
        signal_date = bars["date"].iloc[index].date()
        if signal_date < start or signal_date > target:
            continue
        listing_bars = int((bars.iloc[: index + 1]["date"].dt.date >= listing).sum()) if listing else 60
        if listing and listing_bars < 60:
            skipped["listing_age"] += 1
            continue
        history = bars.iloc[: index + 1]
        weekly = _resample_weekly(history)
        features = compute_strategy4_features(
            history,
            weekly,
            weekly_complete=signal_date.weekday() == 4,
        )
        if strategy4_signal_triggered(features) is not True:
            continue

        entry_index = index + 1
        exit_index = index + BACKTEST_HOLDING_DAYS
        entry = bars.iloc[entry_index]
        exit_bar = bars.iloc[exit_index]
        if float(entry["volume"] or 0.0) <= 0:
            skipped["suspended"] += 1
            continue
        previous_close = float(bars["close"].iloc[index])
        if _is_one_word_limit_up(entry, previous_close, float(limit_pct)):
            skipped["limit_up"] += 1
            continue
        entry_price = float(entry["open"])
        exit_price = float(exit_bar["close"])
        if entry_price <= 0 or exit_price <= 0:
            continue
        entry_cost = entry_price * (1.0 + BUY_FEE + SLIPPAGE)
        exit_proceeds = exit_price * (1.0 - SELL_FEE - STAMP_DUTY - SLIPPAGE)
        net_return = exit_proceeds / entry_cost - 1.0
        signals.append(net_return)

    wins = sum(1 for value in signals if value > 0)
    losses = len(signals) - wins
    win_rate = round(wins / len(signals) * 100.0, 2) if signals else None
    status = "ok" if len(signals) >= BACKTEST_MIN_SAMPLES else "insufficient_sample"
    return BacktestWinRate(
        status=status,
        sample_count=len(signals),
        win_count=wins,
        loss_count=losses,
        win_rate_pct=win_rate,
        reason=None if status == "ok" else "minimum_sample_not_reached",
        rule_version=BACKTEST_RULE_VERSION,
    )


def _normalize(frame: pd.DataFrame, target: date) -> pd.DataFrame:
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    normalized = normalized[normalized["date"].dt.date <= target]
    return normalized.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)


def _resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    working = daily.copy()
    working["week"] = working["date"].dt.to_period("W-FRI")
    return working.groupby("week", sort=True).agg(
        date=("date", "max"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    ).reset_index(drop=True)


def _is_one_word_limit_up(row: pd.Series, previous_close: float, limit_pct: float) -> bool:
    if previous_close <= 0 or limit_pct <= 0:
        return False
    one_word = float(row["open"]) == float(row["high"]) == float(row["low"])
    return one_word and float(row["open"]) / previous_close - 1.0 >= limit_pct


def _result(status: str, reason: str) -> BacktestWinRate:
    return BacktestWinRate(status=status, reason=reason, rule_version=BACKTEST_RULE_VERSION)


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(str(value)).date()


__all__ = ["evaluate_strategy4_backtest"]
