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
    previous_regime: Optional[str] = None
    regime_persist_days = 0
    for idx in range(min_classifier_bars - 1, len(sorted_bars) - max_window):
        history = sorted_bars[: idx + 1]
        anchor = sorted_bars[idx]
        if anchor.close <= 0:
            continue
        regime = classifier(history)
        if not regime or regime == "unknown":
            continue
        if regime == previous_regime:
            regime_persist_days += 1
        else:
            previous_regime = regime
            regime_persist_days = 1
        anchor_atr_pct = _atr_pct_at(sorted_bars, idx)
        row: Dict[str, Any] = {
            "date": anchor.date.isoformat(),
            "regime": regime,
            "close": anchor.close,
            "anchor_index": idx,
            "anchor_atr_pct": _round(anchor_atr_pct),
            "regime_persist_days": regime_persist_days,
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
                "anchor_atr_pct": _round(anchor_atr_pct),
                "significant_move_threshold_pct": _round(_significant_move_threshold(anchor_atr_pct)),
                "sample_quality": _sample_quality_flags(anchor, future, atr_pct=anchor_atr_pct),
            }
        if row["windows"]:
            rows.append(row)
    _attach_forward_regime_persistence(rows, normalized_windows)
    return rows


def compute_symbol_regime_return_frame(
    symbol_bars: Sequence[MarketBar],
    regime_frame: Iterable[Dict[str, Any]],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> List[Dict[str, Any]]:
    """Attach single-symbol forward returns to externally supplied regime labels.

    ``regime_frame`` should come from ``compute_regime_return_frame`` on the
    market/index proxy.  This keeps the regime classifier anchored to the
    market state machine while measuring the forward path on the individual
    stock.
    """
    sorted_bars = sorted(list(symbol_bars or []), key=lambda item: item.date)
    normalized_windows = _normalize_windows(windows)
    if not sorted_bars or not normalized_windows:
        return []

    bars_by_date = {bar.date.isoformat(): (idx, bar) for idx, bar in enumerate(sorted_bars)}
    rows: List[Dict[str, Any]] = []
    for regime_row in regime_frame or []:
        if not isinstance(regime_row, dict):
            continue
        date_key = str(regime_row.get("date") or "")
        matched = bars_by_date.get(date_key)
        if matched is None:
            continue
        idx, anchor = matched
        if anchor.close <= 0:
            continue
        anchor_atr_pct = _atr_pct_at(sorted_bars, idx)
        row: Dict[str, Any] = {
            "date": date_key,
            "regime": str(regime_row.get("regime") or "").strip().lower(),
            "close": anchor.close,
            "anchor_index": idx,
            "market_anchor_index": regime_row.get("anchor_index"),
            "anchor_atr_pct": _round(anchor_atr_pct),
            "regime_persist_days": regime_row.get("regime_persist_days"),
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
            market_sample = (
                regime_row.get("windows", {}).get(str(window))
                if isinstance(regime_row.get("windows"), dict)
                else {}
            )
            row["windows"][str(window)] = {
                "forward_return_pct": _pct(anchor.close, terminal.close),
                "min_return_pct": _pct(anchor.close, min_low),
                "max_return_pct": _pct(anchor.close, max_high),
                "days_to_trough": trough_idx,
                "days_to_peak": peak_idx,
                "window_days": window,
                "anchor_atr_pct": _round(anchor_atr_pct),
                "significant_move_threshold_pct": _round(_significant_move_threshold(anchor_atr_pct)),
                "forward_regime_persist_days": (
                    market_sample.get("forward_regime_persist_days")
                    if isinstance(market_sample, dict)
                    else None
                ),
                "sample_quality": _sample_quality_flags(anchor, future, atr_pct=anchor_atr_pct),
            }
        if row["regime"] and row["windows"]:
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
        sample_rows = [
            row for row in rows
            if isinstance(row.get("windows"), dict) and isinstance(row["windows"].get(key), dict)
        ]
        samples = [row["windows"][key] for row in sample_rows]
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
        persist_days = [_finite(row.get("regime_persist_days")) for row in sample_rows]
        persist_days = [value for value in persist_days if value is not None]
        forward_persist_days = [_finite(sample.get("forward_regime_persist_days")) for sample in samples]
        forward_persist_days = [value for value in forward_persist_days if value is not None]
        atr_values = [_finite(sample.get("anchor_atr_pct")) for sample in samples]
        atr_values = [value for value in atr_values if value is not None]
        n = len(returns)
        effective_n = _non_overlapping_sample_count(sample_rows, window)
        payload["windows"][key] = {
            "n": n,
            "effective_n": effective_n,
            "effective_n_method": "non_overlapping_anchor_windows",
            "median_return_pct": _round(median(returns)) if returns else None,
            "mean_return_pct": _round(mean(returns)) if returns else None,
            "p_up": _round_prob(sum(1 for value in returns if value > 0) / n) if n else None,
            "p_below_current": _round_prob(sum(1 for value in min_returns if value < 0) / len(min_returns)) if min_returns else None,
            "p_down_gt_5pct": _round_prob(sum(1 for value in min_returns if value <= -5.0) / len(min_returns)) if min_returns else None,
            "p10_return_pct": _round(_quantile(returns, 0.10)) if returns else None,
            "p20_return_pct": _round(_quantile(returns, 0.20)) if returns else None,
            "p90_return_pct": _round(_quantile(returns, 0.90)) if returns else None,
            "p10_min_return_pct": _round(_quantile(min_returns, 0.10)) if min_returns else None,
            "p20_min_return_pct": _round(_quantile(min_returns, 0.20)) if min_returns else None,
            "min_return_median_pct": _round(median(min_returns)) if min_returns else None,
            "max_return_median_pct": _round(median(max_returns)) if max_returns else None,
            "days_to_trough_median": _round(median(days_to_trough), digits=1) if days_to_trough else None,
            "days_to_peak_median": _round(median(days_to_peak), digits=1) if days_to_peak else None,
            "regime_persist_days_at_anchor_median": _round(median(persist_days), digits=1) if persist_days else None,
            "forward_regime_persist_days_median": _round(median(forward_persist_days), digits=1) if forward_persist_days else None,
            "anchor_atr_pct_median": _round(median(atr_values)) if atr_values else None,
            "sample_quality": _aggregate_sample_quality(samples),
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
    thresholds: List[float] = []
    for sample in samples:
        fwd = _finite(sample.get("forward_return_pct")) or 0.0
        min_ret = _finite(sample.get("min_return_pct")) or 0.0
        max_ret = _finite(sample.get("max_return_pct")) or 0.0
        threshold = _finite(sample.get("significant_move_threshold_pct"))
        if threshold is None:
            threshold = _significant_move_threshold(_finite(sample.get("anchor_atr_pct")))
        thresholds.append(threshold)
        significant_dip = min_ret <= -threshold
        significant_pop = max_ret >= threshold
        if significant_dip:
            dips.append(min_ret)
            trough_days.append(_finite(sample.get("days_to_trough")) or 0.0)
        if significant_pop:
            pops.append(max_ret)
            peak_days.append(_finite(sample.get("days_to_peak")) or 0.0)
        if significant_dip and fwd > 0:
            dip_then_up += 1
        elif not significant_dip and fwd > 0:
            up_no_dip += 1
        elif significant_pop and fwd <= 0:
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
        "significant_move_threshold_pct_median": _round(median(thresholds)) if thresholds else None,
        "threshold_method": "anchor_atr_pct_floor_1pct_cap_8pct",
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
    downside_pct = _finite(item.get("p20_min_return_pct"))
    if downside_pct is None:
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


def build_sample_quality_summary(
    bars: Sequence[MarketBar],
    frame: Iterable[Dict[str, Any]],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    min_classifier_bars: int = MIN_CLASSIFIER_BARS,
) -> Dict[str, Any]:
    """Summarize sample coverage and quality markers for diagnostics."""
    sorted_bars = sorted(list(bars or []), key=lambda item: item.date)
    normalized_windows = _normalize_windows(windows)
    max_window = max(normalized_windows)
    usable_anchor_count = max(0, len(sorted_bars) - max_window - max(0, min_classifier_bars - 1))
    tail_lookahead_excluded = min(max_window, max(0, len(sorted_bars) - max(0, min_classifier_bars - 1)))
    rows = list(frame or [])
    per_window = {
        str(window): _aggregate_sample_quality(
            [
                row.get("windows", {}).get(str(window))
                for row in rows
                if isinstance(row.get("windows"), dict)
            ]
        )
        for window in normalized_windows
    }
    return {
        "bars": len(sorted_bars),
        "min_classifier_bars": int(min_classifier_bars),
        "max_window": max_window,
        "eligible_anchor_count": usable_anchor_count,
        "classified_anchor_count": len(rows),
        "tail_lookahead_excluded": tail_lookahead_excluded,
        "per_window": per_window,
        "notes": [
            "Quality flags mark suspect samples; the first implementation does not drop them automatically.",
            "tail_lookahead_excluded anchors are omitted because their forward window is incomplete.",
        ],
    }


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


def _atr_pct_at(bars: Sequence[MarketBar], idx: int, *, window: int = 14) -> Optional[float]:
    if idx <= 0:
        return None
    start = max(1, idx - window + 1)
    ranges: List[float] = []
    for pos in range(start, idx + 1):
        current = bars[pos]
        previous = bars[pos - 1]
        if current.close <= 0 or previous.close <= 0:
            continue
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        ranges.append(true_range)
    if not ranges or bars[idx].close <= 0:
        return None
    return mean(ranges) / bars[idx].close * 100.0


def _significant_move_threshold(atr_pct: Optional[float]) -> float:
    value = _finite(atr_pct)
    if value is None:
        return 1.0
    return max(1.0, min(8.0, value))


def _attach_forward_regime_persistence(rows: List[Dict[str, Any]], windows: Sequence[int]) -> None:
    if not rows:
        return
    by_anchor = {
        int(row.get("anchor_index")): row
        for row in rows
        if _finite(row.get("anchor_index")) is not None
    }
    normalized_windows = _normalize_windows(windows)
    for row in rows:
        anchor = _finite(row.get("anchor_index"))
        if anchor is None:
            continue
        anchor_idx = int(anchor)
        regime = str(row.get("regime") or "")
        windows_payload = row.get("windows") if isinstance(row.get("windows"), dict) else {}
        for window in normalized_windows:
            same_regime_days = 0
            for offset in range(1, int(window) + 1):
                next_row = by_anchor.get(anchor_idx + offset)
                if not next_row or str(next_row.get("regime") or "") != regime:
                    break
                same_regime_days += 1
            sample = windows_payload.get(str(window))
            if isinstance(sample, dict):
                sample["forward_regime_persist_days"] = same_regime_days


def _sample_quality_flags(anchor: MarketBar, future: Sequence[MarketBar], *, atr_pct: Optional[float]) -> Dict[str, Any]:
    flags: List[str] = []
    zero_volume_days = sum(1 for bar in future if (bar.volume or 0.0) <= 0)
    if zero_volume_days:
        flags.append("zero_volume_or_suspension_suspect")
    invalid_ohlc_days = sum(1 for bar in future if bar.low <= 0 or bar.high < bar.low or bar.close <= 0)
    if invalid_ohlc_days:
        flags.append("invalid_ohlc")

    jump_threshold = max(12.0, (_significant_move_threshold(atr_pct) * 4.0))
    previous_close = anchor.close
    abnormal_jump_days = 0
    limit_like_days = 0
    for bar in future:
        if previous_close > 0 and bar.close > 0:
            change_pct = _pct(previous_close, bar.close)
            if abs(change_pct) >= jump_threshold:
                abnormal_jump_days += 1
            if abs(change_pct) >= 9.5:
                limit_like_days += 1
        previous_close = bar.close
    if abnormal_jump_days:
        flags.append("ex_rights_or_bad_tick_suspect")
    if limit_like_days:
        flags.append("limit_move_suspect")

    return {
        "flags": flags,
        "zero_volume_days": zero_volume_days,
        "invalid_ohlc_days": invalid_ohlc_days,
        "abnormal_jump_days": abnormal_jump_days,
        "limit_like_days": limit_like_days,
    }


def _aggregate_sample_quality(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    flag_counts: Dict[str, int] = {}
    suspect_samples = 0
    total = 0
    zero_volume_days = 0
    abnormal_jump_days = 0
    limit_like_days = 0
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        total += 1
        quality = sample.get("sample_quality") if isinstance(sample.get("sample_quality"), dict) else {}
        flags = [str(flag) for flag in quality.get("flags") or [] if str(flag)]
        if flags:
            suspect_samples += 1
        for flag in flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
        zero_volume_days += int(_finite(quality.get("zero_volume_days")) or 0)
        abnormal_jump_days += int(_finite(quality.get("abnormal_jump_days")) or 0)
        limit_like_days += int(_finite(quality.get("limit_like_days")) or 0)
    return {
        "samples": total,
        "suspect_samples": suspect_samples,
        "flag_counts": flag_counts,
        "zero_volume_days": zero_volume_days,
        "abnormal_jump_days": abnormal_jump_days,
        "limit_like_days": limit_like_days,
    }


def _non_overlapping_sample_count(rows: Sequence[Dict[str, Any]], window: int) -> int:
    anchors: List[int] = []
    for row in rows or []:
        index = _finite(row.get("anchor_index"))
        if index is not None:
            anchors.append(int(index))
    if not anchors:
        return 0
    count = 0
    last_anchor: Optional[int] = None
    for anchor in sorted(anchors):
        if last_anchor is None or anchor - last_anchor >= max(1, int(window)):
            count += 1
            last_anchor = anchor
    return count


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
