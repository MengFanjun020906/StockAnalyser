# -*- coding: utf-8 -*-
"""Chan and SMC price-structure analysis.

The module deliberately returns structural evidence instead of trading
judgements. Downstream LLM and risk-gate layers decide how to use the evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class PriceBar:
    """One normalized OHLCV bar."""

    index: int
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def coerce_price_bars(rows: Iterable[Any]) -> List[PriceBar]:
    """Coerce DataFrame rows, dicts, tuples or dataclass-like rows into bars."""
    bars: List[PriceBar] = []
    for idx, row in enumerate(rows):
        item = _row_to_mapping(row)
        try:
            open_ = _as_float(_get_any(item, "open", "Open"))
            high = _as_float(_get_any(item, "high", "High"))
            low = _as_float(_get_any(item, "low", "Low"))
            close = _as_float(_get_any(item, "close", "Close"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (open_, high, low, close)):
            continue
        if high < low:
            high, low = low, high
        volume = _as_float(_get_any(item, "volume", "vol", "Volume"), default=0.0)
        time_value = _get_any(item, "date", "datetime", "time", "trade_date", "timestamp")
        bars.append(
            PriceBar(
                index=idx,
                time=str(time_value) if time_value not in (None, "") else str(idx),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume if math.isfinite(volume) else 0.0,
            )
        )
    return bars


def analyze_price_structure(
    bars: Sequence[PriceBar],
    *,
    min_bars: int = 30,
    chan_min_pen_span: int = 4,
    swing_window: int = 3,
    include_raw: bool = False,
) -> Dict[str, Any]:
    """Analyze Chan and SMC structure from normalized OHLCV bars."""
    normalized = list(bars)
    if len(normalized) < min_bars:
        return {
            "status": "insufficient_data",
            "data_quality": "insufficient",
            "bar_count": len(normalized),
            "min_bars": min_bars,
            "error": f"历史K线不足：需要至少 {min_bars} 根，实际 {len(normalized)} 根。",
        }

    merged = _merge_inclusion_bars(normalized)
    fractals = _detect_chan_fractals(merged)
    macd_bars = _calculate_macd_bars(normalized)
    pens = _build_chan_pens(fractals, min_span=chan_min_pen_span, macd_bars=macd_bars)
    centers = _detect_chan_centers(pens)
    unfinished_pen = _detect_unfinished_pen(merged, pens)
    chan = {
        "status": "ok" if len(pens) >= 2 else "limited",
        "merged_bar_count": len(merged),
        "fractal_count": len(fractals),
        "pen_count": len(pens),
        "center_count": len(centers),
        "latest_fractals": fractals[-8:],
        "latest_pens": pens[-8:],
        "latest_centers": centers[-5:],
        "unfinished_pen": unfinished_pen,
        "structure_summary": _summarize_chan(pens, centers, unfinished_pen),
    }

    swings = _detect_swings(normalized, window=swing_window)
    smc = {
        "status": "ok" if swings else "limited",
        "swing_window": swing_window,
        "swing_count": len(swings),
        "latest_swings": swings[-12:],
        "bos": _detect_bos(normalized, swings),
        "choch": _detect_choch(normalized, swings),
        "order_blocks": _detect_order_blocks(normalized),
        "fair_value_gaps": _detect_fair_value_gaps(normalized),
        "structure_summary": _summarize_smc(swings),
    }

    result = {
        "status": "ok",
        "data_quality": "sufficient" if len(normalized) >= 80 else "limited",
        "bar_count": len(normalized),
        "latest_bar": normalized[-1].to_dict(),
        "chan": chan,
        "smc": smc,
        "notes": [
            "结构分析只输出笔、中枢、力度、BOS/CHoCH/OB/FVG 等证据，不直接给买卖结论。",
            "若日线样本少于 80 根，缠论中枢和力度对比可靠性会下降。",
        ],
    }
    if include_raw:
        result["raw"] = {"merged_bars": [bar.to_dict() for bar in merged]}
    return result


def _row_to_mapping(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "_asdict"):
        return row._asdict()
    if hasattr(row, "to_dict"):
        try:
            return row.to_dict()
        except Exception:
            pass
    if hasattr(row, "__dict__"):
        return {k: v for k, v in row.__dict__.items() if not k.startswith("_")}
    return {}


def _get_any(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    lower = {str(k).lower(): v for k, v in item.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _as_float(value: Any, default: Optional[float] = None) -> float:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("empty numeric value")
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise


def _merge_inclusion_bars(bars: Sequence[PriceBar]) -> List[PriceBar]:
    """Merge Chan inclusion relationships while preserving broad direction."""
    if not bars:
        return []
    merged = [bars[0]]
    direction = 0
    for bar in bars[1:]:
        last = merged[-1]
        contains = (last.high >= bar.high and last.low <= bar.low) or (bar.high >= last.high and bar.low <= last.low)
        if not contains:
            if bar.high > last.high and bar.low > last.low:
                direction = 1
            elif bar.high < last.high and bar.low < last.low:
                direction = -1
            merged.append(bar)
            continue
        if direction >= 0:
            high = max(last.high, bar.high)
            low = max(last.low, bar.low)
        else:
            high = min(last.high, bar.high)
            low = min(last.low, bar.low)
        merged[-1] = PriceBar(
            index=bar.index,
            time=bar.time,
            open=last.open,
            high=high,
            low=low,
            close=bar.close,
            volume=last.volume + bar.volume,
        )
    return merged


def _detect_chan_fractals(bars: Sequence[PriceBar]) -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    for i in range(1, len(bars) - 1):
        prev_, cur, nxt = bars[i - 1], bars[i], bars[i + 1]
        if cur.high > prev_.high and cur.high > nxt.high and cur.low > prev_.low and cur.low > nxt.low:
            raw.append(_fractal("top", cur, cur.high))
        elif cur.low < prev_.low and cur.low < nxt.low and cur.high < prev_.high and cur.high < nxt.high:
            raw.append(_fractal("bottom", cur, cur.low))

    alternating: List[Dict[str, Any]] = []
    for item in raw:
        if not alternating:
            alternating.append(item)
            continue
        last = alternating[-1]
        if item["type"] != last["type"]:
            alternating.append(item)
            continue
        if item["type"] == "top" and item["price"] >= last["price"]:
            alternating[-1] = item
        elif item["type"] == "bottom" and item["price"] <= last["price"]:
            alternating[-1] = item
    return alternating


def _fractal(kind: str, bar: PriceBar, price: float) -> Dict[str, Any]:
    return {
        "type": kind,
        "index": bar.index,
        "time": bar.time,
        "price": round(float(price), 4),
    }


def _calculate_macd_bars(bars: Sequence[PriceBar]) -> List[float]:
    closes = [bar.close for bar in bars]
    if not closes:
        return []
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    dif = [fast - slow for fast, slow in zip(ema_fast, ema_slow)]
    dea = _ema(dif, 9)
    return [(d - e) * 2 for d, e in zip(dif, dea)]


def _ema(values: Sequence[float], span: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def _macd_area(macd_bars: Sequence[float], start_index: int, end_index: int) -> float:
    if not macd_bars:
        return 0.0
    lo, hi = sorted((max(start_index, 0), max(end_index, 0)))
    hi = min(hi, len(macd_bars) - 1)
    if lo > hi:
        return 0.0
    return float(sum(abs(value) for value in macd_bars[lo:hi + 1]))


def _build_chan_pens(
    fractals: Sequence[Dict[str, Any]],
    *,
    min_span: int,
    macd_bars: Sequence[float],
) -> List[Dict[str, Any]]:
    pens: List[Dict[str, Any]] = []
    for start, end in zip(fractals, fractals[1:]):
        span = int(end["index"]) - int(start["index"])
        if span < min_span or start["type"] == end["type"]:
            continue
        direction = "up" if start["type"] == "bottom" and end["type"] == "top" else "down"
        start_price = float(start["price"])
        end_price = float(end["price"])
        amplitude_pct = (end_price - start_price) / start_price * 100 if start_price else 0.0
        macd_area = _macd_area(macd_bars, int(start["index"]), int(end["index"]))
        pens.append({
            "id": len(pens) + 1,
            "direction": direction,
            "start": start,
            "end": end,
            "span": span,
            "amplitude_pct": round(amplitude_pct, 3),
            "power": {
                "price_move": round(abs(end_price - start_price), 4),
                "amplitude_pct": round(abs(amplitude_pct), 3),
                "macd_area": round(macd_area, 6),
                "macd_area_ratio_vs_prev": None,
                "amplitude_ratio_vs_prev": None,
            },
        })
        if len(pens) >= 2:
            prev = pens[-2]
            prev_amp = float(prev["power"].get("amplitude_pct") or 0.0)
            prev_macd_area = float(prev["power"].get("macd_area") or 0.0)
            amp = float(pens[-1]["power"]["amplitude_pct"])
            curr_macd_area = float(pens[-1]["power"].get("macd_area") or 0.0)
            pens[-1]["power"]["amplitude_ratio_vs_prev"] = round(amp / prev_amp, 3) if prev_amp else None
            pens[-1]["power"]["macd_area_ratio_vs_prev"] = round(curr_macd_area / prev_macd_area, 3) if prev_macd_area else None
    return pens


def _detect_chan_centers(pens: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    centers: List[Dict[str, Any]] = []
    for i in range(0, len(pens) - 2):
        trio = pens[i:i + 3]
        highs = [max(float(p["start"]["price"]), float(p["end"]["price"])) for p in trio]
        lows = [min(float(p["start"]["price"]), float(p["end"]["price"])) for p in trio]
        zg = min(highs)
        zd = max(lows)
        if zg < zd:
            continue
        start_index = int(trio[0]["start"]["index"])
        end_index = int(trio[-1]["end"]["index"])
        centers.append({
            "id": len(centers) + 1,
            "pen_ids": [p["id"] for p in trio],
            "ZG": round(zg, 4),
            "ZD": round(zd, 4),
            "GG": round(max(highs), 4),
            "DD": round(min(lows), 4),
            "start_index": start_index,
            "end_index": end_index,
            "width_pct": round((zg - zd) / zd * 100, 3) if zd else None,
        })
    return _merge_overlapping_centers(centers)


def _merge_overlapping_centers(centers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for center in centers:
        if not merged:
            merged.append(dict(center))
            continue
        last = merged[-1]
        if center["ZD"] <= last["ZG"] and center["ZG"] >= last["ZD"] and center["start_index"] <= last["end_index"] + 20:
            last["ZG"] = round(min(float(last["ZG"]), float(center["ZG"])), 4)
            last["ZD"] = round(max(float(last["ZD"]), float(center["ZD"])), 4)
            last["GG"] = round(max(float(last["GG"]), float(center["GG"])), 4)
            last["DD"] = round(min(float(last["DD"]), float(center["DD"])), 4)
            last["end_index"] = center["end_index"]
            last["pen_ids"] = sorted(set(list(last["pen_ids"]) + list(center["pen_ids"])))
            last["width_pct"] = round((last["ZG"] - last["ZD"]) / last["ZD"] * 100, 3) if last["ZD"] else None
        else:
            merged.append(dict(center))
    for idx, center in enumerate(merged, start=1):
        center["id"] = idx
    return merged


def _detect_unfinished_pen(bars: Sequence[PriceBar], pens: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not bars:
        return {"status": "unknown"}
    latest = bars[-1]
    if not pens:
        return {"status": "insufficient_pens", "latest_price": latest.close}
    last = pens[-1]
    anchor = last["end"]
    anchor_price = float(anchor["price"])
    if last["direction"] == "up":
        direction = "down_candidate"
        progress = (anchor_price - latest.low) / anchor_price * 100 if anchor_price else 0.0
        invalidation = max(bar.high for bar in bars[-5:])
    else:
        direction = "up_candidate"
        progress = (latest.high - anchor_price) / anchor_price * 100 if anchor_price else 0.0
        invalidation = min(bar.low for bar in bars[-5:])
    return {
        "status": "forming",
        "direction": direction,
        "anchor_fractal": anchor,
        "latest_index": latest.index,
        "latest_time": latest.time,
        "progress_pct": round(max(progress, 0.0), 3),
        "invalidation_level": round(float(invalidation), 4),
    }


def _detect_swings(bars: Sequence[PriceBar], *, window: int) -> List[Dict[str, Any]]:
    swings: List[Dict[str, Any]] = []
    if len(bars) < window * 2 + 1:
        return swings
    for i in range(window, len(bars) - window):
        cur = bars[i]
        left = bars[i - window:i]
        right = bars[i + 1:i + 1 + window]
        if cur.high >= max(bar.high for bar in left + right):
            swings.append(_swing("high", cur, cur.high))
        if cur.low <= min(bar.low for bar in left + right):
            swings.append(_swing("low", cur, cur.low))
    return _label_swing_sequence(sorted(swings, key=lambda item: (item["index"], item["type"])))


def _swing(kind: str, bar: PriceBar, price: float) -> Dict[str, Any]:
    return {
        "type": kind,
        "label": None,
        "index": bar.index,
        "time": bar.time,
        "price": round(float(price), 4),
    }


def _label_swing_sequence(swings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    last_high: Optional[float] = None
    last_low: Optional[float] = None
    for swing in swings:
        item = dict(swing)
        price = float(item["price"])
        if item["type"] == "high":
            item["label"] = "HH" if last_high is not None and price > last_high else "LH" if last_high is not None else "H"
            last_high = price
        else:
            item["label"] = "HL" if last_low is not None and price > last_low else "LL" if last_low is not None else "L"
            last_low = price
        result.append(item)
    return result


def _recent_swing(swings: Sequence[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    for swing in reversed(swings):
        if swing["type"] == kind:
            return swing
    return None


def _infer_smc_bias(swings: Sequence[Dict[str, Any]]) -> str:
    labels = [str(item.get("label")) for item in swings[-6:]]
    if labels.count("HH") + labels.count("HL") >= 3:
        return "up"
    if labels.count("LH") + labels.count("LL") >= 3:
        return "down"
    return "range"


def _detect_bos(bars: Sequence[PriceBar], swings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not bars or not swings:
        return {"status": "none"}
    latest = bars[-1]
    bias = _infer_smc_bias(swings)
    last_high = _recent_swing(swings, "high")
    last_low = _recent_swing(swings, "low")
    if bias in {"up", "range"} and last_high and latest.close > float(last_high["price"]):
        return {"status": "bullish", "level": last_high, "break_index": latest.index, "break_price": round(latest.close, 4)}
    if bias in {"down", "range"} and last_low and latest.close < float(last_low["price"]):
        return {"status": "bearish", "level": last_low, "break_index": latest.index, "break_price": round(latest.close, 4)}
    return {"status": "none", "bias": bias}


def _detect_choch(bars: Sequence[PriceBar], swings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not bars or not swings:
        return {"status": "none"}
    latest = bars[-1]
    bias = _infer_smc_bias(swings)
    last_high = _recent_swing(swings, "high")
    last_low = _recent_swing(swings, "low")
    if bias == "up" and last_low and latest.close < float(last_low["price"]):
        return {"status": "bearish", "level": last_low, "break_index": latest.index, "break_price": round(latest.close, 4)}
    if bias == "down" and last_high and latest.close > float(last_high["price"]):
        return {"status": "bullish", "level": last_high, "break_index": latest.index, "break_price": round(latest.close, 4)}
    return {"status": "none", "bias": bias}


def _detect_order_blocks(bars: Sequence[PriceBar]) -> List[Dict[str, Any]]:
    if len(bars) < 25:
        return []
    volumes = [bar.volume for bar in bars if bar.volume > 0]
    avg_volume = sum(volumes[-20:]) / len(volumes[-20:]) if volumes else 0.0
    blocks: List[Dict[str, Any]] = []
    for i in range(1, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        spread = max(bar.high - bar.low, 1e-9)
        body_ratio = abs(bar.close - bar.open) / spread
        volume_ok = avg_volume <= 0 or bar.volume > avg_volume * 1.3
        if body_ratio <= 0.6 or not volume_ok:
            continue
        bullish_impulse = bar.close > bar.open and prev.close < prev.open
        bearish_impulse = bar.close < bar.open and prev.close > prev.open
        if not bullish_impulse and not bearish_impulse:
            continue
        blocks.append({
            "type": "bullish" if bullish_impulse else "bearish",
            "source_index": prev.index,
            "source_time": prev.time,
            "impulse_index": bar.index,
            "zone_low": round(prev.low, 4),
            "zone_high": round(prev.high, 4),
            "body_ratio": round(body_ratio, 3),
            "volume_ratio": round(bar.volume / avg_volume, 3) if avg_volume else None,
        })
    return blocks[-8:]


def _detect_fair_value_gaps(bars: Sequence[PriceBar]) -> List[Dict[str, Any]]:
    gaps: List[Dict[str, Any]] = []
    for i in range(2, len(bars)):
        a, mid, c = bars[i - 2], bars[i - 1], bars[i]
        if c.low > a.high:
            gaps.append({
                "type": "bullish",
                "start_index": a.index,
                "middle_index": mid.index,
                "end_index": c.index,
                "zone_low": round(a.high, 4),
                "zone_high": round(c.low, 4),
                "gap_pct": round((c.low - a.high) / a.high * 100, 3) if a.high else None,
            })
        elif c.high < a.low:
            gaps.append({
                "type": "bearish",
                "start_index": a.index,
                "middle_index": mid.index,
                "end_index": c.index,
                "zone_low": round(c.high, 4),
                "zone_high": round(a.low, 4),
                "gap_pct": round((a.low - c.high) / a.low * 100, 3) if a.low else None,
            })
    return gaps[-10:]


def _summarize_chan(
    pens: Sequence[Dict[str, Any]],
    centers: Sequence[Dict[str, Any]],
    unfinished_pen: Dict[str, Any],
) -> Dict[str, Any]:
    latest_pen = pens[-1] if pens else None
    latest_center = centers[-1] if centers else None
    return {
        "latest_pen_direction": latest_pen.get("direction") if latest_pen else "unknown",
        "latest_pen_amplitude_pct": latest_pen.get("amplitude_pct") if latest_pen else None,
        "latest_center": {
            "ZG": latest_center.get("ZG"),
            "ZD": latest_center.get("ZD"),
            "width_pct": latest_center.get("width_pct"),
        } if latest_center else None,
        "unfinished_direction": unfinished_pen.get("direction"),
    }


def _summarize_smc(swings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    labels = [item.get("label") for item in swings[-6:]]
    return {
        "bias": _infer_smc_bias(swings),
        "latest_labels": labels,
    }
