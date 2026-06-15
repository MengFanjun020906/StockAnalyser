# -*- coding: utf-8 -*-
"""Forward-return probability layer for market regime evidence.

This module intentionally reuses ``src.agent.regime.detect_market_regime`` as
the regime classifier. It computes historical forward-return distributions for
the current regime and returns evidence only; callers must not translate the
payload directly into buy/sell actions.
"""

from __future__ import annotations

import math
from statistics import mean, median
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from src.agent.regime import MarketBar, detect_market_regime

RegimeClassifier = Callable[[Sequence[MarketBar]], str]

DEFAULT_WINDOWS = (7, 30, 60, 90)
MIN_CLASSIFIER_BARS = 80
MIN_SAMPLES_PER_WINDOW = 12


def compute_regime_return_frame(
    bars: Sequence[MarketBar],
    *,
    regime_classifier: Optional[RegimeClassifier] = None,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    market: str = "cn",
    min_classifier_bars: int = MIN_CLASSIFIER_BARS,
) -> List[Dict[str, Any]]:
    """Classify each historical anchor date and attach forward path returns."""
    sorted_bars = sorted(list(bars or []), key=lambda item: item.date)
    normalized_windows = _normalize_windows(windows)
    if len(sorted_bars) < min_classifier_bars + min(normalized_windows):
        return []

    classifier = regime_classifier or _default_regime_classifier(market=market)
    rows: List[Dict[str, Any]] = []
    max_window = max(normalized_windows)
    for idx in range(min_classifier_bars - 1, len(sorted_bars) - max_window):
        history = sorted_bars[: idx + 1]
        anchor = sorted_bars[idx]
        if anchor.close <= 0:
            continue
        regime = classifier(history)
        if not regime or regime == "unknown":
            continue
        row: Dict[str, Any] = {
            "date": anchor.date.isoformat(),
            "regime": regime,
            "close": anchor.close,
            "windows": {},
        }
        for window in normalized_windows:
            future = sorted_bars[idx + 1 : idx + window + 1]
            if len(future) < window:
                continue
            terminal = future[-1]
            closes = [bar.close for bar in future if bar.close > 0]
            highs = [bar.high for bar in future if bar.high > 0]
            lows = [bar.low for bar in future if bar.low > 0]
            if not closes or not highs or not lows or terminal.close <= 0:
                continue
            min_low = min(lows)
            max_high = max(highs)
            trough_idx = next((i for i, bar in enumerate(future, start=1) if bar.low == min_low), window)
            peak_idx = next((i for i, bar in enumerate(future, start=1) if bar.high == max_high), window)
            row["windows"][str(window)] = {
                "forward_return_pct": _pct(anchor.close, terminal.close),
                "min_return_pct": _pct(anchor.close, min_low),
                "max_return_pct": _pct(anchor.close, max_high),
                "days_to_trough": trough_idx,
                "days_to_peak": peak_idx,
                "window_days": window,
            }
        if row["windows"]:
            rows.append(row)
    return rows


def build_regime_probability(
    frame: Iterable[Dict[str, Any]],
    *,
    regime: str,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    min_samples: int = MIN_SAMPLES_PER_WINDOW,
) -> Dict[str, Any]:
    """Aggregate forward-return samples for one regime."""
    target_regime = str(regime or "").strip().lower()
    normalized_windows = _normalize_windows(windows)
    rows = [row for row in frame if str(row.get("regime") or "").lower() == target_regime]
    payload: Dict[str, Any] = {
        "regime": target_regime or "unknown",
        "sample_count": len(rows),
        "windows": {},
    }
    for window in normalized_windows:
        key = str(window)
        samples = [row.get("windows", {}).get(key) for row in rows if isinstance(row.get("windows"), dict)]
        samples = [sample for sample in samples if isinstance(sample, dict)]
        returns = [_finite(sample.get("forward_return_pct")) for sample in samples]
        returns = [value for value in returns if value is not None]
        min_returns = [_finite(sample.get("min_return_pct")) for sample in samples]
        min_returns = [value for value in min_returns if value is not None]
        max_returns = [_finite(sample.get("max_return_pct")) for sample in samples]
        max_returns = [value for value in max_returns if value is not None]
        days_to_trough = [_finite(sample.get("days_to_trough")) for sample in samples]
        days_to_trough = [value for value in days_to_trough if value is not None]
        days_to_peak = [_finite(sample.get("days_to_peak")) for sample in samples]
        days_to_peak = [value for value in days_to_peak if value is not None]
        n = len(returns)
        effective_n = n / max(1, window)
        payload["windows"][key] = {
            "n": n,
            "effective_n": round(effective_n, 2),
            "median_return_pct": _round(median(returns)) if returns else None,
            "mean_return_pct": _round(mean(returns)) if returns else None,
            "p_up": _round_prob(sum(1 for value in returns if value > 0) / n) if n else None,
            "p_below_current": _round_prob(sum(1 for value in min_returns if value < 0) / len(min_returns)) if min_returns else None,
            "p_down_gt_5pct": _round_prob(sum(1 for value in min_returns if value <= -5.0) / len(min_returns)) if min_returns else None,
            "p10_return_pct": _round(_quantile(returns, 0.10)) if returns else None,
            "p20_return_pct": _round(_quantile(returns, 0.20)) if returns else None,
            "p90_return_pct": _round(_quantile(returns, 0.90)) if returns else None,
            "min_return_median_pct": _round(median(min_returns)) if min_returns else None,
            "max_return_median_pct": _round(median(max_returns)) if max_returns else None,
            "days_to_trough_median": _round(median(days_to_trough), digits=1) if days_to_trough else None,
            "days_to_peak_median": _round(median(days_to_peak), digits=1) if days_to_peak else None,
            "low_confidence": n < min_samples or effective_n < 1.0,
        }
    return payload


def build_path_profile(
    frame: Iterable[Dict[str, Any]],
    *,
    regime: str,
    window: int = 90,
) -> Dict[str, Any]:
    key = str(int(window))
    rows = [
        row for row in frame
        if str(row.get("regime") or "").lower() == str(regime or "").lower()
        and isinstance(row.get("windows"), dict)
        and isinstance(row["windows"].get(key), dict)
    ]
    samples = [row["windows"][key] for row in rows]
    if not samples:
        return {"window": f"{key}d", "n": 0, "low_confidence": True}
    dip_then_up = 0
    up_no_dip = 0
    pop_then_down = 0
    down_no_pop = 0
    dips: List[float] = []
    pops: List[float] = []
    trough_days: List[float] = []
    peak_days: List[float] = []
    for sample in samples:
        fwd = _finite(sample.get("forward_return_pct")) or 0.0
        min_ret = _finite(sample.get("min_return_pct")) or 0.0
        max_ret = _finite(sample.get("max_return_pct")) or 0.0
        if min_ret < 0:
            dips.append(min_ret)
            trough_days.append(_finite(sample.get("days_to_trough")) or 0.0)
        if max_ret > 0:
            pops.append(max_ret)
            peak_days.append(_finite(sample.get("days_to_peak")) or 0.0)
        if min_ret < 0 and fwd > 0:
            dip_then_up += 1
        elif min_ret >= 0 and fwd > 0:
            up_no_dip += 1
        elif max_ret > 0 and fwd <= 0:
            pop_then_down += 1
        else:
            down_no_pop += 1
    n = len(samples)
    return {
        "window": f"{key}d",
        "n": n,
        "pct_dip_then_up": _round_prob(dip_then_up / n),
        "pct_up_no_dip": _round_prob(up_no_dip / n),
        "pct_pop_then_down": _round_prob(pop_then_down / n),
        "pct_down_no_pop": _round_prob(down_no_pop / n),
        "dip_median_pct": _round(median(dips)) if dips else None,
        "days_to_trough_median": _round(median(trough_days), digits=1) if trough_days else None,
        "pop_median_pct": _round(median(pops)) if pops else None,
        "days_to_peak_median": _round(median(peak_days), digits=1) if peak_days else None,
        "window_median_days": int(window),
        "low_confidence": n < MIN_SAMPLES_PER_WINDOW,
    }


def build_reentry_reference(
    probability: Dict[str, Any],
    *,
    current_price: Optional[float],
    window: int = 30,
    downside_quantile: float = 0.20,
) -> Dict[str, Any]:
    windows = probability.get("windows") if isinstance(probability.get("windows"), dict) else {}
    item = windows.get(str(int(window))) if isinstance(windows.get(str(int(window))), dict) else {}
    downside_pct = _finite(item.get("p20_return_pct"))
    price = _finite(current_price)
    low_confidence = bool(item.get("low_confidence", True))
    payload: Dict[str, Any] = {
        "current_price": price,
        "window": f"{int(window)}d",
        "downside_quantile": downside_quantile,
        "downside_pct": downside_pct,
        "p_below_current": item.get("p_below_current"),
        "low_confidence": low_confidence,
    }
    if price is not None and downside_pct is not None and downside_pct < 0:
        payload["reentry_price"] = round(price * (1 + downside_pct / 100.0), 4)
    else:
        payload["reentry_price"] = None
    return payload


def format_regime_probability_brief(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict) or payload.get("status") not in {"ok", "partial"}:
        return "Regime 概率层样本不足，不能作为主要裁决依据。"
    regime = payload.get("regime") or "unknown"
    windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
    item = windows.get("30") or windows.get("30d") or next(iter(windows.values()), {})
    if not isinstance(item, dict) or not item.get("n"):
        return f"regime={regime} 暂无足够 forward return 样本。"
    return (
        f"regime={regime}，30d样本 n={item.get('n')} / effective_n={item.get('effective_n')}，"
        f"上涨概率={item.get('p_up')}，跌破现价概率={item.get('p_below_current')}，"
        f"中位收益={item.get('median_return_pct')}%。"
    )


def _default_regime_classifier(*, market: str) -> RegimeClassifier:
    def classify(history: Sequence[MarketBar]) -> str:
        state = detect_market_regime(history, market=market, confirmation_bars=1)
        return state.regime.value

    return classify


def _normalize_windows(windows: Sequence[int]) -> List[int]:
    result: List[int] = []
    for value in windows or DEFAULT_WINDOWS:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result or list(DEFAULT_WINDOWS)


def _pct(base: float, value: float) -> float:
    if base <= 0:
        return 0.0
    return (value / base - 1.0) * 100.0


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _round(value: Any, *, digits: int = 2) -> Optional[float]:
    number = _finite(value)
    return round(number, digits) if number is not None else None


def _round_prob(value: Any) -> Optional[float]:
    number = _finite(value)
    return round(max(0.0, min(1.0, number)), 4) if number is not None else None
