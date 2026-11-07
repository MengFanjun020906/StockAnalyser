"""Frozen, deterministic Strategy-4 indicator calculations."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd

from src.schemas.composite_verdict import S4FeatureSnapshot


RULE_VERSION = "strategy4.v1"
DAILY_LOOKBACK_BARS = 520
EMA_WARMUP_BARS = 200
WEEKLY_EMA_WARMUP_BARS = 40
WEEKLY_MIN_BARS = 60


def compute_strategy4_features(
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    *,
    weekly_complete: bool,
) -> S4FeatureSnapshot:
    """Calculate the frozen S4 snapshot from already truncated bars.

    The 40-week warmup is the approved normalization of the 200 daily-bar
    warmup for the weekly series. The required 60 weekly bars still dominate
    because MA57 rising needs three prior weekly observations.
    """
    daily_indicators = _series_indicators(daily, ema_warmup=EMA_WARMUP_BARS)
    weekly_indicators = _series_indicators(weekly, ema_warmup=WEEKLY_EMA_WARMUP_BARS)

    daily_bull = _compare(daily_indicators["close"], daily_indicators["guide_line"], "gt")
    daily_strong = _compare(daily_indicators["guide_line"], daily_indicators["ma57"], "gt")
    weekly_bull = _compare(weekly_indicators["close"], weekly_indicators["guide_line"], "gt")
    weekly_strong = _compare(weekly_indicators["guide_line"], weekly_indicators["ma57"], "gt")

    region_pass = _and(
        _compare(daily_indicators["guide_line"], daily_indicators["ma57"], "gt"),
        daily_indicators["ma57_rising"],
    )
    resonance_count = _resonance_count(daily_bull, daily_strong, weekly_bull, weekly_strong)
    volume_ratio = _volume_ratio_5d(daily)
    volatility = _atr14_over_ma20(daily)
    gain_10d = _gain_10d(daily)
    golden_cross = _golden_cross(daily_indicators)
    all_ok = _all_ok(
        region_pass=region_pass,
        resonance_count=resonance_count,
        volume_ratio=volume_ratio,
        volatility=volatility,
        gain_10d=gain_10d,
    )

    missing: list[str] = []
    required_values = {
        "guide_line": daily_indicators["guide_line"],
        "ma57": daily_indicators["ma57"],
        "ma57_rising": daily_indicators["ma57_rising"],
        "buy_line": daily_indicators["buy_line"],
        "sell_line": daily_indicators["sell_line"],
        "daily_bull": daily_bull,
        "daily_strong": daily_strong,
        "weekly_guide_line": weekly_indicators["guide_line"],
        "weekly_ma57": weekly_indicators["ma57"],
        "weekly_ma57_rising": weekly_indicators["ma57_rising"],
        "weekly_buy_line": weekly_indicators["buy_line"],
        "weekly_sell_line": weekly_indicators["sell_line"],
        "weekly_bull": weekly_bull,
        "weekly_strong": weekly_strong,
        "region_pass": region_pass,
        "resonance_count": resonance_count,
        "volume_ratio_5d": volume_ratio,
        "volatility_pct": volatility,
        "gain_10d_pct": gain_10d,
        "golden_cross_today": golden_cross,
        "all_ok": all_ok,
    }
    missing.extend(name for name, value in required_values.items() if value is None)

    daily_ready = len(daily) >= EMA_WARMUP_BARS
    weekly_ready = len(weekly) >= WEEKLY_MIN_BARS and len(weekly) >= WEEKLY_EMA_WARMUP_BARS
    if not daily_ready:
        status = "not_implemented"
    elif not weekly_ready or not weekly_complete or missing:
        status = "partial"
    else:
        status = "ok"

    return S4FeatureSnapshot(
        status=status,
        region_pass=region_pass,
        resonance_count=resonance_count,
        volume_ratio_5d=volume_ratio,
        volatility_pct=volatility,
        gain_10d_pct=gain_10d,
        golden_cross_today=golden_cross,
        all_ok=all_ok,
        guide_line=daily_indicators["guide_line"],
        ma57=daily_indicators["ma57"],
        ma57_rising=daily_indicators["ma57_rising"],
        buy_line=daily_indicators["buy_line"],
        sell_line=daily_indicators["sell_line"],
        daily_bull=daily_bull,
        daily_strong=daily_strong,
        weekly_bull=weekly_bull,
        weekly_strong=weekly_strong,
        weekly_guide_line=weekly_indicators["guide_line"],
        weekly_ma57=weekly_indicators["ma57"],
        weekly_ma57_rising=weekly_indicators["ma57_rising"],
        weekly_buy_line=weekly_indicators["buy_line"],
        weekly_sell_line=weekly_indicators["sell_line"],
        weekly_provisional=not weekly_complete,
        missing_features=list(dict.fromkeys(missing)),
    )


def strategy4_signal_triggered(features: S4FeatureSnapshot) -> Optional[bool]:
    """Return the frozen signal trigger, preserving missing data as ``None``."""
    if features.golden_cross_today is None or features.all_ok is None:
        return None
    return bool(features.golden_cross_today and features.all_ok)


def _series_indicators(frame: pd.DataFrame, *, ema_warmup: int) -> Dict[str, Any]:
    if frame is None or frame.empty or "close" not in frame.columns:
        return {name: None for name in ("close", "guide_line", "ma57", "ma57_rising", "buy_line", "sell_line")}
    close = pd.to_numeric(frame["close"], errors="coerce").dropna().reset_index(drop=True)
    if close.empty:
        return {name: None for name in ("close", "guide_line", "ma57", "ma57_rising", "buy_line", "sell_line")}

    guide = _last_ema(close, 13, ema_warmup)
    ma57_series = close.rolling(57, min_periods=57).mean()
    ma57 = _rounded_last(ma57_series)
    ma57_rising = (
        _rounded_last(ma57_series) is not None
        and _rounded_value(ma57_series.iloc[-4]) is not None
        and _rounded_last(ma57_series) > _rounded_value(ma57_series.iloc[-4])
        if len(ma57_series) >= 4
        else None
    )
    # The explicit 57 + 3 requirement is enforced independently of pandas' NaN.
    if len(close) < 60:
        ma57_rising = None

    dif, dea = _macd_series(close, ema_warmup)
    return {
        "close": _rounded_last(close),
        "guide_line": guide,
        "ma57": ma57,
        "ma57_rising": ma57_rising,
        "buy_line": _rounded_last(dif),
        "sell_line": _rounded_last(dea),
        "dif": dif,
        "dea": dea,
    }


def _macd_series(close: pd.Series, ema_warmup: int) -> Tuple[pd.Series, pd.Series]:
    if len(close) < ema_warmup:
        empty = pd.Series(dtype=float)
        return empty, empty
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea


def _golden_cross(indicators: Dict[str, Any]) -> Optional[bool]:
    dif = indicators.get("dif")
    dea = indicators.get("dea")
    if not isinstance(dif, pd.Series) or not isinstance(dea, pd.Series) or len(dif) < 2 or len(dea) < 2:
        return None
    current_gap = _rounded_value(dif.iloc[-1] - dea.iloc[-1])
    previous_gap = _rounded_value(dif.iloc[-2] - dea.iloc[-2])
    if current_gap is None or previous_gap is None:
        return None
    return current_gap > 0 and previous_gap <= 0


def _resonance_count(*values: Optional[bool]) -> Optional[int]:
    if any(value is None for value in values):
        return None
    return sum(bool(value) for value in values)


def _all_ok(
    *,
    region_pass: Optional[bool],
    resonance_count: Optional[int],
    volume_ratio: Optional[float],
    volatility: Optional[float],
    gain_10d: Optional[float],
) -> Optional[bool]:
    if any(value is None for value in (region_pass, resonance_count, volume_ratio, volatility, gain_10d)):
        return None
    return bool(
        region_pass
        and resonance_count >= 3
        and volume_ratio >= 1.2
        and volatility <= 6.0
        and gain_10d <= 9.0
    )


def _compare(left: Optional[float], right: Optional[float], operator: str) -> Optional[bool]:
    if left is None or right is None:
        return None
    if operator == "gt":
        return left > right
    raise ValueError(f"unsupported comparison operator: {operator}")


def _and(left: Optional[bool], right: Optional[bool]) -> Optional[bool]:
    if left is None or right is None:
        return None
    return bool(left and right)


def _last_ema(values: pd.Series, span: int, warmup: int) -> Optional[float]:
    if len(values) < warmup:
        return None
    return _rounded_last(values.ewm(span=span, adjust=False).mean())


def _rounded_last(values: Any) -> Optional[float]:
    if values is None or len(values) == 0:
        return None
    return _rounded_value(values.iloc[-1] if hasattr(values, "iloc") else values[-1])


def _rounded_value(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if pd.notna(number) else None


def _volume_ratio_5d(daily: pd.DataFrame) -> Optional[float]:
    if len(daily) < 6:
        return None
    baseline = float(pd.to_numeric(daily["volume"], errors="coerce").iloc[-6:-1].mean())
    latest = float(pd.to_numeric(daily["volume"], errors="coerce").iloc[-1])
    return round(latest / baseline, 6) if baseline > 0 else None


def _atr14_over_ma20(daily: pd.DataFrame) -> Optional[float]:
    if len(daily) < 20:
        return None
    close = pd.to_numeric(daily["close"], errors="coerce")
    high = pd.to_numeric(daily["high"], errors="coerce")
    low = pd.to_numeric(daily["low"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = float(true_range.tail(14).mean())
    ma20 = float(close.tail(20).mean())
    return round(atr14 / ma20 * 100.0, 6) if ma20 > 0 else None


def _gain_10d(daily: pd.DataFrame) -> Optional[float]:
    if len(daily) < 11:
        return None
    close = pd.to_numeric(daily["close"], errors="coerce")
    base = float(close.iloc[-11])
    latest = float(close.iloc[-1])
    return round((latest / base - 1.0) * 100.0, 6) if base > 0 else None


__all__ = [
    "DAILY_LOOKBACK_BARS",
    "EMA_WARMUP_BARS",
    "RULE_VERSION",
    "WEEKLY_EMA_WARMUP_BARS",
    "WEEKLY_MIN_BARS",
    "compute_strategy4_features",
    "strategy4_signal_triggered",
]
