# -*- coding: utf-8 -*-
"""Kronos daily K-line data preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from data_provider.base import normalize_stock_code
from src.services.history_loader import load_history_df

KRONOS_DAILY_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
MIN_DAILY_BARS = 30


@dataclass(frozen=True)
class KronosPreparedData:
    """Normalized OHLCV payload accepted by KronosPredictor."""

    code: str
    market: str
    frequency: str
    bars_df: pd.DataFrame
    x_timestamp: pd.Series
    y_timestamp: pd.Series
    source: str
    as_of: str
    data_quality: Dict[str, Any] = field(default_factory=dict)


def normalize_a_share_code(stock_code: str) -> Optional[str]:
    """Return a six-digit A-share code, or None for unsupported markets."""
    code = normalize_stock_code(str(stock_code or "").strip())
    if code.isdigit() and len(code) == 6:
        return code
    return None


class KronosDataAdapter:
    """Prepare StockAnalyser daily history for Kronos inference."""

    def prepare_daily(
        self,
        stock_code: str,
        *,
        lookback: int,
        pred_len: int,
        target_date: Optional[date] = None,
        fallback_to_network: bool = True,
    ) -> KronosPreparedData:
        code = normalize_a_share_code(stock_code)
        if not code:
            raise ValueError(f"unsupported A-share symbol: {stock_code}")

        requested_days = max(int(lookback), MIN_DAILY_BARS)
        raw_df, source = load_history_df(
            code,
            days=requested_days,
            target_date=target_date,
            fallback_to_network=fallback_to_network,
        )
        if raw_df is None or raw_df.empty:
            quality = {
                "status": "missing",
                "bar_count": 0,
                "required_bars": max(MIN_DAILY_BARS, min(requested_days, lookback)),
                "source": source,
                "flags": ["history_missing"],
                "warnings": ["No daily K-line history available for Kronos analysis."],
            }
            return KronosPreparedData(
                code=code,
                market="cn",
                frequency="1d",
                bars_df=pd.DataFrame(columns=["timestamp", *KRONOS_DAILY_COLUMNS]),
                x_timestamp=pd.Series(dtype="datetime64[ns]"),
                y_timestamp=pd.Series(dtype="datetime64[ns]"),
                source=source,
                as_of="",
                data_quality=quality,
            )

        bars = _normalize_history_frame(raw_df, code=code)
        if target_date is not None and not bars.empty:
            cutoff = pd.Timestamp(target_date)
            bars = bars[bars["timestamp"] <= cutoff]
        bars = bars.tail(max(int(lookback), MIN_DAILY_BARS)).reset_index(drop=True)

        bar_count = len(bars)
        required_bars = max(MIN_DAILY_BARS, min(int(lookback), MIN_DAILY_BARS))
        flags: List[str] = []
        warnings: List[str] = []
        if bar_count < MIN_DAILY_BARS:
            flags.append("insufficient_daily_bars")
            warnings.append(f"Daily K-line history requires at least {MIN_DAILY_BARS} bars, got {bar_count}.")

        if bars.empty:
            as_of = ""
            x_timestamp = pd.Series(dtype="datetime64[ns]")
            y_timestamp = pd.Series(dtype="datetime64[ns]")
        else:
            latest_ts = pd.Timestamp(bars["timestamp"].iloc[-1])
            as_of = latest_ts.date().isoformat()
            x_timestamp = bars["timestamp"].copy()
            y_timestamp = pd.Series(_next_business_days(latest_ts.date(), int(pred_len)))
            flags.append("future_calendar_business_day_approximation")
            warnings.append("Future daily timestamps use a weekday approximation until an exchange calendar is wired in.")

        quality = {
            "status": "sufficient" if bar_count >= MIN_DAILY_BARS else "insufficient",
            "bar_count": bar_count,
            "required_bars": MIN_DAILY_BARS,
            "source": source,
            "flags": flags,
            "warnings": warnings,
            "as_of": as_of,
        }
        return KronosPreparedData(
            code=code,
            market="cn",
            frequency="1d",
            bars_df=bars,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            source=source,
            as_of=as_of,
            data_quality=quality,
        )


def _normalize_history_frame(df: pd.DataFrame, *, code: str) -> pd.DataFrame:
    frame = df.copy()
    timestamp_col = _first_existing_column(frame, ["timestamp", "timestamps", "date", "datetime", "trade_date", "time"])
    if timestamp_col is None:
        raise ValueError("Kronos daily history requires a timestamp/date column.")

    frame["timestamp"] = pd.to_datetime(frame[timestamp_col], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col not in frame.columns:
            frame[col] = 0.0
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame["volume"] = frame["volume"].fillna(0.0)
    amount_missing = frame["amount"].isna() | (frame["amount"] == 0)
    if amount_missing.any():
        mean_price = frame[["open", "high", "low", "close"]].mean(axis=1)
        frame.loc[amount_missing, "amount"] = frame.loc[amount_missing, "volume"] * mean_price.loc[amount_missing]

    frame["code"] = code
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return frame[["code", "timestamp", *KRONOS_DAILY_COLUMNS]].reset_index(drop=True)


def _first_existing_column(frame: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower_map = {str(col).lower(): str(col) for col in frame.columns}
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
        lowered = candidate.lower()
        if lowered in lower_map:
            return lower_map[lowered]
    return None


def _next_business_days(start: date, count: int) -> List[datetime]:
    days: List[datetime] = []
    current = start
    while len(days) < max(int(count), 0):
        current = current + timedelta(days=1)
        if current.weekday() >= 5:
            continue
        days.append(datetime(current.year, current.month, current.day))
    return days


def bars_to_records(bars: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert normalized bars to chart-safe JSON records."""
    records: List[Dict[str, Any]] = []
    if bars is None or bars.empty:
        return records
    for idx, row in bars.reset_index(drop=True).iterrows():
        timestamp = pd.Timestamp(row["timestamp"])
        records.append(
            {
                "index": int(idx),
                "time": timestamp.date().isoformat(),
                "open": _round(row.get("open")),
                "high": _round(row.get("high")),
                "low": _round(row.get("low")),
                "close": _round(row.get("close")),
                "volume": _round(row.get("volume"), digits=2),
                "amount": _round(row.get("amount"), digits=2),
            }
        )
    return records


def forecast_to_records(forecast: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert Kronos forecast DataFrame to chart-safe JSON records."""
    records: List[Dict[str, Any]] = []
    if forecast is None or forecast.empty:
        return records
    frame = forecast.copy()
    if "timestamp" not in frame.columns:
        frame["timestamp"] = frame.index
    for idx, row in frame.reset_index(drop=True).iterrows():
        timestamp = pd.Timestamp(row["timestamp"])
        record: Dict[str, Any] = {
            "index": int(idx),
            "time": timestamp.date().isoformat(),
        }
        for col in KRONOS_DAILY_COLUMNS:
            if col in row:
                record[col] = _round(row.get(col), digits=2 if col in {"volume", "amount"} else 4)
        records.append(record)
    return records


def _round(value: Any, *, digits: int = 4) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number):
        return None
    return round(number, digits)
