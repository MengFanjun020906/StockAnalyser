"""Build as-of-safe feature snapshots for deterministic stock scoring."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from src.agent.structure import analyze_price_structure, coerce_price_bars
from src.agent.scoring.strategy4 import (
    DAILY_LOOKBACK_BARS,
    EMA_WARMUP_BARS,
    WEEKLY_EMA_WARMUP_BARS,
    WEEKLY_MIN_BARS,
    RULE_VERSION,
    compute_strategy4_features,
)
from src.schemas.composite_verdict import (
    ChanFeatureSnapshot,
    ChanSignal,
    CompositeScoreSnapshot,
    S4FeatureSnapshot,
)


S4_UNIMPLEMENTED_FEATURES = ()


class CompositeSnapshotBuilder:
    """Load, truncate and normalize all inputs before pure evaluation."""

    def __init__(
        self,
        history_loader: Optional[Callable[..., Tuple[Optional[pd.DataFrame], str]]] = None,
        *,
        fallback_to_network: bool = True,
        minimum_history_bars: int = DAILY_LOOKBACK_BARS,
    ) -> None:
        self._history_loader = history_loader
        self._fallback_to_network = fallback_to_network
        self._minimum_history_bars = max(1, int(minimum_history_bars))

    def build(
        self,
        symbol: str,
        *,
        as_of: Any,
        quote_basis: str = "unknown",
        holding: bool = False,
    ) -> CompositeScoreSnapshot:
        target_date = _coerce_date(as_of)
        loader = self._history_loader or _default_history_loader
        loader_kwargs = {
            "days": DAILY_LOOKBACK_BARS,
            "target_date": target_date,
            "fallback_to_network": self._fallback_to_network,
        }
        # Keep injected test loaders and local adapters backwards-compatible;
        # the production loader alone owns the strict cache contract.
        if self._history_loader is None:
            loader_kwargs["minimum_records"] = self._minimum_history_bars
        frame, source = loader(symbol, **loader_kwargs)
        daily = _normalize_daily_frame(frame, target_date).tail(DAILY_LOOKBACK_BARS)
        if daily.empty:
            return _empty_snapshot(
                symbol=symbol,
                target_date=target_date,
                quote_basis=quote_basis,
                holding=holding,
                source=source,
            )

        daily_structure = analyze_price_structure(
            coerce_price_bars(daily.to_dict("records")),
            min_bars=30,
        )
        weekly = _resample_weekly(daily)
        weekly_structure = (
            analyze_price_structure(coerce_price_bars(weekly.to_dict("records")), min_bars=30)
            if len(weekly) >= 30
            else {"status": "insufficient_data", "chan": {"signals": []}}
        )
        last_date = daily["date"].iloc[-1].date()
        weekly_complete = last_date.weekday() == 4
        chan_signals = _snapshot_chan_signals(
            daily_structure,
            weekly_structure,
            weekly_complete=weekly_complete,
        )
        s4 = compute_strategy4_features(
            daily,
            weekly,
            weekly_complete=weekly_complete,
        )
        quality = (
            "sufficient"
            if len(daily) >= DAILY_LOOKBACK_BARS
            else "limited"
            if len(daily) >= 30
            else "insufficient"
        )
        adjustment_mode = _adjustment_mode(daily)
        if adjustment_mode == "unknown" and quality == "sufficient":
            quality = "limited"
        daily_structure_ok = daily_structure.get("status") == "ok"
        chan_status = (
            "partial"
            if daily_structure_ok and not weekly_complete
            else "ok"
            if daily_structure_ok
            else "insufficient"
        )

        return CompositeScoreSnapshot(
            symbol=symbol,
            as_of=target_date.isoformat(),
            quote_basis=quote_basis,
            data_quality=quality,
            holding=holding,
            s4=s4,
            chan=ChanFeatureSnapshot(
                status=chan_status,
                signals=chan_signals,
            ),
            metadata={
                "source": source,
                "strategy4_rule_version": RULE_VERSION,
                "history_start": daily["date"].iloc[0].date().isoformat(),
                "history_end": last_date.isoformat(),
                "daily_bar_count": len(daily),
                "weekly_bar_count": len(weekly),
                "daily_lookback_bars": DAILY_LOOKBACK_BARS,
                "ema_warmup_bars": EMA_WARMUP_BARS,
                "ema_warmup_bars_available": min(len(daily), EMA_WARMUP_BARS),
                "weekly_ema_warmup_bars_required": WEEKLY_EMA_WARMUP_BARS,
                "weekly_ema_warmup_bars_available": min(len(weekly), WEEKLY_EMA_WARMUP_BARS),
                "weekly_min_bars_required": WEEKLY_MIN_BARS,
                "weekly_last_bar_complete": weekly_complete,
                "adjustment_mode": adjustment_mode,
                "price_adjustment_basis": adjustment_mode,
                "daily_structure_status": daily_structure.get("status"),
                "weekly_structure_status": weekly_structure.get("status"),
            },
        )


def _default_history_loader(*args: Any, **kwargs: Any) -> Tuple[Optional[pd.DataFrame], str]:
    from src.services.history_loader import load_history_df

    return load_history_df(*args, **kwargs)


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(str(value)).date()
    except Exception as exc:
        raise ValueError(f"invalid as_of date: {value}") from exc


def _normalize_daily_frame(frame: Optional[pd.DataFrame], target_date: date) -> pd.DataFrame:
    columns = ["date", "open", "high", "low", "close", "volume"]
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
            normalized[column] = 0.0
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    normalized = normalized[normalized["date"].dt.date <= target_date]
    normalized = normalized.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return normalized.reset_index(drop=True)


def _resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return daily.copy()
    working = daily.copy()
    working["week"] = working["date"].dt.to_period("W-FRI")
    weekly = working.groupby("week", sort=True).agg(
        date=("date", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return weekly.reset_index(drop=True)


def _snapshot_chan_signals(
    daily_structure: Dict[str, Any],
    weekly_structure: Dict[str, Any],
    *,
    weekly_complete: bool,
) -> list:
    daily_chan = daily_structure.get("chan") if isinstance(daily_structure.get("chan"), dict) else {}
    weekly_chan = weekly_structure.get("chan") if isinstance(weekly_structure.get("chan"), dict) else {}
    signals = [ChanSignal.model_validate(item) for item in daily_chan.get("signals") or [] if isinstance(item, dict)]
    weekly_buy_names = {"first_buy", "second_buy", "third_buy"}
    weekly_buy = next(
        (item for item in weekly_chan.get("signals") or [] if isinstance(item, dict) and item.get("name") in weekly_buy_names),
        None,
    )
    if weekly_buy:
        signals.append(ChanSignal(
            name="weekly_confirmation",
            timeframe="1w",
            triggered=True,
            metrics={
                "confirmed_signal": weekly_buy.get("name"),
                "provisional": not weekly_complete,
            },
            evidence=list(weekly_buy.get("evidence") or []),
        ))
    return signals


def _adjustment_mode(daily: pd.DataFrame) -> str:
    if "price_adjustment_basis" not in daily.columns:
        return "unknown"
    values = [str(value) for value in daily["price_adjustment_basis"].dropna().unique() if str(value)]
    return values[-1] if values else "unknown"


def _empty_snapshot(
    *,
    symbol: str,
    target_date: date,
    quote_basis: str,
    holding: bool,
    source: str,
) -> CompositeScoreSnapshot:
    missing = [
        "guide_line",
        "ma57",
        "ma57_rising",
        "buy_line",
        "sell_line",
        "daily_bull",
        "daily_strong",
        "weekly_bull",
        "weekly_strong",
        "region_pass",
        "resonance_count",
        "volume_ratio_5d",
        "volatility_pct",
        "gain_10d_pct",
        "golden_cross_today",
        "all_ok",
    ]
    return CompositeScoreSnapshot(
        symbol=symbol,
        as_of=target_date.isoformat(),
        quote_basis=quote_basis,
        data_quality="insufficient",
        holding=holding,
        s4=S4FeatureSnapshot(status="not_implemented", missing_features=missing),
        chan=ChanFeatureSnapshot(status="insufficient"),
        metadata={
            "source": source,
            "strategy4_rule_version": RULE_VERSION,
            "daily_bar_count": 0,
            "weekly_bar_count": 0,
            "daily_lookback_bars": DAILY_LOOKBACK_BARS,
            "ema_warmup_bars": EMA_WARMUP_BARS,
            "weekly_ema_warmup_bars_required": WEEKLY_EMA_WARMUP_BARS,
            "weekly_min_bars_required": WEEKLY_MIN_BARS,
            "error": "no_history",
        },
    )


__all__ = ["CompositeSnapshotBuilder", "DAILY_LOOKBACK_BARS", "EMA_WARMUP_BARS"]
