# -*- coding: utf-8 -*-
"""Deterministic daily K-line chart pattern detector."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd


DETECTOR_NAME = "deterministic_kline_patterns_v1"

PATTERN_LABELS = {
    "double_bottom": "W底",
    "double_top": "M顶",
    "head_shoulders_top": "头肩顶",
    "inverse_head_shoulders_bottom": "头肩底",
    "range_breakout": "箱体突破",
    "triangle_consolidation": "三角收敛",
}


@dataclass(frozen=True)
class Pivot:
    index: int
    kind: str
    price: float


@dataclass(frozen=True)
class KlinePattern:
    pattern_type: str
    direction: str
    confidence: float
    start_index: int
    end_index: int
    price_low: float
    price_high: float
    summary: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, frame: pd.DataFrame, *, code: str = "") -> Dict[str, Any]:
        start_time = _date_text(frame.iloc[self.start_index]["timestamp"])
        end_time = _date_text(frame.iloc[self.end_index]["timestamp"])
        price_low = round(float(self.price_low), 4)
        price_high = round(float(self.price_high), 4)
        pattern_id = _pattern_id(code, self.pattern_type, start_time, end_time, price_low, price_high)
        evidence = {"detector": DETECTOR_NAME, **self.evidence}
        return {
            "pattern_id": pattern_id,
            "pattern_type": self.pattern_type,
            "label": PATTERN_LABELS.get(self.pattern_type, self.pattern_type),
            "direction": self.direction,
            "confidence": round(max(0.0, min(float(self.confidence), 1.0)), 4),
            "start_time": start_time,
            "end_time": end_time,
            "price_low": price_low,
            "price_high": price_high,
            "red_box": {"x0": start_time, "x1": end_time, "y0": price_low, "y1": price_high},
            "summary": self.summary,
            "evidence": evidence,
        }


def detect_kline_patterns(
    bars: pd.DataFrame,
    *,
    code: str = "",
    lookback: int = 120,
    max_patterns: int = 5,
) -> List[Dict[str, Any]]:
    """Detect chart-pattern instances from historical OHLCV bars only."""

    frame = _normalize_frame(bars).tail(max(int(lookback), 30)).reset_index(drop=True)
    if len(frame) < 30:
        return []

    pivots = _find_pivots(frame, order=3)
    detected: List[KlinePattern] = []
    for detector in (
        _detect_double_bottom,
        _detect_double_top,
        _detect_inverse_head_shoulders,
        _detect_head_shoulders,
        _detect_range_breakout,
        _detect_triangle_consolidation,
    ):
        pattern = detector(frame, pivots)
        if pattern is not None:
            detected.append(pattern)

    detected = _dedupe_patterns(detected)
    detected.sort(key=lambda item: (item.confidence, item.end_index), reverse=True)
    return [item.to_dict(frame, code=code) for item in detected[: max(int(max_patterns), 0)]]


def _normalize_frame(bars: pd.DataFrame) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])
    frame = bars.copy()
    timestamp_col = _first_existing_column(frame, ["timestamp", "date", "datetime", "trade_date", "time"])
    if timestamp_col is None:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])
    frame["timestamp"] = pd.to_datetime(frame[timestamp_col], errors="coerce")
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return frame[["timestamp", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)


def _find_pivots(frame: pd.DataFrame, *, order: int = 3) -> Dict[str, List[Pivot]]:
    lows = frame["low"].astype(float).tolist()
    highs = frame["high"].astype(float).tolist()
    pivots: Dict[str, List[Pivot]] = {"low": [], "high": []}
    if len(frame) < order * 2 + 1:
        return pivots
    for idx in range(order, len(frame) - order):
        low_window = lows[idx - order : idx + order + 1]
        high_window = highs[idx - order : idx + order + 1]
        if lows[idx] <= min(low_window):
            pivots["low"].append(Pivot(idx, "low", lows[idx]))
        if highs[idx] >= max(high_window):
            pivots["high"].append(Pivot(idx, "high", highs[idx]))
    pivots["low"] = _compact_adjacent_pivots(pivots["low"], prefer="low")
    pivots["high"] = _compact_adjacent_pivots(pivots["high"], prefer="high")
    return pivots


def _detect_double_bottom(frame: pd.DataFrame, pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    lows = pivots.get("low", [])
    highs = pivots.get("high", [])
    best: Optional[KlinePattern] = None
    latest_close = _last_close(frame)
    for first in lows:
        for second in lows:
            if second.index <= first.index:
                continue
            separation = second.index - first.index
            if separation < 5 or separation > 55 or len(frame) - second.index > 35:
                continue
            avg_low = (first.price + second.price) / 2.0
            similarity = 1.0 - abs(first.price - second.price) / avg_low if avg_low > 0 else 0.0
            if similarity < 0.94:
                continue
            neckline_pivot = _highest_between(highs, first.index, second.index)
            neckline = neckline_pivot.price if neckline_pivot else _slice_high(frame, first.index, second.index)
            if neckline <= avg_low * 1.045:
                continue
            recovery = (latest_close / avg_low - 1.0) if avg_low > 0 else 0.0
            breakout_progress = latest_close / neckline if neckline > 0 else 0.0
            if recovery < 0.035 or breakout_progress < 0.90:
                continue
            confidence = 0.48 + (similarity - 0.94) * 2.4 + min(max(recovery, 0.0), 0.16) * 1.1
            if breakout_progress >= 0.995:
                confidence += 0.08
            start = first.index
            end = len(frame) - 1
            pattern = _make_pattern(
                frame,
                pattern_type="double_bottom",
                direction="bullish",
                confidence=confidence,
                start=start,
                end=end,
                summary="Detected a W-bottom structure with two similar swing lows and a rebound toward the neckline.",
                evidence={
                    "first_low_time": _date_text(frame.iloc[first.index]["timestamp"]),
                    "second_low_time": _date_text(frame.iloc[second.index]["timestamp"]),
                    "neckline": round(neckline, 4),
                    "low_similarity_pct": round(similarity * 100.0, 2),
                    "breakout_progress_pct": round(breakout_progress * 100.0, 2),
                },
            )
            best = _better(pattern, best)
    return best


def _detect_double_top(frame: pd.DataFrame, pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    highs = pivots.get("high", [])
    lows = pivots.get("low", [])
    best: Optional[KlinePattern] = None
    latest_close = _last_close(frame)
    for first in highs:
        for second in highs:
            if second.index <= first.index:
                continue
            separation = second.index - first.index
            if separation < 5 or separation > 55 or len(frame) - second.index > 35:
                continue
            avg_high = (first.price + second.price) / 2.0
            similarity = 1.0 - abs(first.price - second.price) / avg_high if avg_high > 0 else 0.0
            if similarity < 0.94:
                continue
            neckline_pivot = _lowest_between(lows, first.index, second.index)
            neckline = neckline_pivot.price if neckline_pivot else _slice_low(frame, first.index, second.index)
            if neckline >= avg_high * 0.955:
                continue
            rejection = (avg_high / latest_close - 1.0) if latest_close > 0 else 0.0
            breakdown_progress = neckline / latest_close if latest_close > 0 else 0.0
            if rejection < 0.035 or breakdown_progress < 0.90:
                continue
            confidence = 0.48 + (similarity - 0.94) * 2.4 + min(max(rejection, 0.0), 0.16) * 1.1
            if latest_close <= neckline * 1.005:
                confidence += 0.08
            pattern = _make_pattern(
                frame,
                pattern_type="double_top",
                direction="bearish",
                confidence=confidence,
                start=first.index,
                end=len(frame) - 1,
                summary="Detected an M-top structure with two similar swing highs and a rejection toward the neckline.",
                evidence={
                    "first_high_time": _date_text(frame.iloc[first.index]["timestamp"]),
                    "second_high_time": _date_text(frame.iloc[second.index]["timestamp"]),
                    "neckline": round(neckline, 4),
                    "high_similarity_pct": round(similarity * 100.0, 2),
                    "breakdown_progress_pct": round(breakdown_progress * 100.0, 2),
                },
            )
            best = _better(pattern, best)
    return best


def _detect_head_shoulders(frame: pd.DataFrame, pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    highs = pivots.get("high", [])
    lows = pivots.get("low", [])
    best: Optional[KlinePattern] = None
    latest_close = _last_close(frame)
    for left_idx in range(len(highs) - 2):
        left, head, right = highs[left_idx], highs[left_idx + 1], highs[left_idx + 2]
        if right.index - left.index > 70 or head.index - left.index < 4 or right.index - head.index < 4:
            continue
        shoulder_avg = (left.price + right.price) / 2.0
        shoulder_similarity = 1.0 - abs(left.price - right.price) / shoulder_avg if shoulder_avg > 0 else 0.0
        if shoulder_similarity < 0.90:
            continue
        if head.price < max(left.price, right.price) * 1.03:
            continue
        neckline_left = _lowest_between(lows, left.index, head.index)
        neckline_right = _lowest_between(lows, head.index, right.index)
        neckline = min(
            neckline_left.price if neckline_left else _slice_low(frame, left.index, head.index),
            neckline_right.price if neckline_right else _slice_low(frame, head.index, right.index),
        )
        if latest_close > neckline * 1.04:
            continue
        confidence = 0.54 + (shoulder_similarity - 0.90) * 1.0 + min((head.price / shoulder_avg - 1.0), 0.16)
        pattern = _make_pattern(
            frame,
            pattern_type="head_shoulders_top",
            direction="bearish",
            confidence=confidence,
            start=left.index,
            end=len(frame) - 1,
            summary="Detected a head-and-shoulders top with a higher middle peak and weakening right shoulder.",
            evidence={
                "left_shoulder_time": _date_text(frame.iloc[left.index]["timestamp"]),
                "head_time": _date_text(frame.iloc[head.index]["timestamp"]),
                "right_shoulder_time": _date_text(frame.iloc[right.index]["timestamp"]),
                "neckline": round(neckline, 4),
                "shoulder_similarity_pct": round(shoulder_similarity * 100.0, 2),
            },
        )
        best = _better(pattern, best)
    return best


def _detect_inverse_head_shoulders(frame: pd.DataFrame, pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    lows = pivots.get("low", [])
    highs = pivots.get("high", [])
    best: Optional[KlinePattern] = None
    latest_close = _last_close(frame)
    for left_idx in range(len(lows) - 2):
        left, head, right = lows[left_idx], lows[left_idx + 1], lows[left_idx + 2]
        if right.index - left.index > 70 or head.index - left.index < 4 or right.index - head.index < 4:
            continue
        shoulder_avg = (left.price + right.price) / 2.0
        shoulder_similarity = 1.0 - abs(left.price - right.price) / shoulder_avg if shoulder_avg > 0 else 0.0
        if shoulder_similarity < 0.90:
            continue
        if head.price > min(left.price, right.price) * 0.97:
            continue
        neckline_left = _highest_between(highs, left.index, head.index)
        neckline_right = _highest_between(highs, head.index, right.index)
        neckline = max(
            neckline_left.price if neckline_left else _slice_high(frame, left.index, head.index),
            neckline_right.price if neckline_right else _slice_high(frame, head.index, right.index),
        )
        if latest_close < neckline * 0.96:
            continue
        confidence = 0.54 + (shoulder_similarity - 0.90) * 1.0 + min((shoulder_avg / head.price - 1.0), 0.16)
        pattern = _make_pattern(
            frame,
            pattern_type="inverse_head_shoulders_bottom",
            direction="bullish",
            confidence=confidence,
            start=left.index,
            end=len(frame) - 1,
            summary="Detected an inverse head-and-shoulders bottom with a lower middle trough and neckline recovery.",
            evidence={
                "left_shoulder_time": _date_text(frame.iloc[left.index]["timestamp"]),
                "head_time": _date_text(frame.iloc[head.index]["timestamp"]),
                "right_shoulder_time": _date_text(frame.iloc[right.index]["timestamp"]),
                "neckline": round(neckline, 4),
                "shoulder_similarity_pct": round(shoulder_similarity * 100.0, 2),
            },
        )
        best = _better(pattern, best)
    return best


def _detect_range_breakout(frame: pd.DataFrame, _pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    if len(frame) < 45:
        return None
    base = frame.iloc[-45:-5]
    latest = frame.iloc[-1]
    range_high = float(base["high"].max())
    range_low = float(base["low"].min())
    median_close = float(base["close"].median())
    if median_close <= 0:
        return None
    width_pct = (range_high - range_low) / median_close
    latest_close = float(latest["close"])
    if width_pct > 0.18 or latest_close < range_high * 1.005:
        return None
    volume_ma = float(base["volume"].mean()) if float(base["volume"].mean()) > 0 else 0.0
    latest_volume = float(latest.get("volume") or 0.0)
    volume_ratio = latest_volume / volume_ma if volume_ma > 0 else 1.0
    confidence = 0.55 + min((latest_close / range_high - 1.0) * 4.0, 0.16)
    if volume_ratio >= 1.08:
        confidence += 0.07
    return _make_pattern(
        frame,
        pattern_type="range_breakout",
        direction="bullish",
        confidence=confidence,
        start=len(frame) - len(base) - 5,
        end=len(frame) - 1,
        summary="Detected a range breakout after a compact consolidation window.",
        evidence={
            "range_high": round(range_high, 4),
            "range_low": round(range_low, 4),
            "range_width_pct": round(width_pct * 100.0, 2),
            "volume_ratio": round(volume_ratio, 3),
        },
    )


def _detect_triangle_consolidation(frame: pd.DataFrame, pivots: Dict[str, List[Pivot]]) -> Optional[KlinePattern]:
    if len(frame) < 55:
        return None
    start_floor = len(frame) - 60
    highs = [item for item in pivots.get("high", []) if item.index >= start_floor]
    lows = [item for item in pivots.get("low", []) if item.index >= start_floor]
    if len(highs) < 2 or len(lows) < 2:
        return None
    first_high, last_high = highs[0], highs[-1]
    first_low, last_low = lows[0], lows[-1]
    if first_high.price <= last_high.price or first_low.price >= last_low.price:
        return None
    start_width = first_high.price - first_low.price
    end_width = last_high.price - last_low.price
    if start_width <= 0 or end_width <= 0 or start_width < end_width * 1.25:
        return None
    latest_close = _last_close(frame)
    if latest_close > last_high.price * 1.02 or latest_close < last_low.price * 0.98:
        return None
    return _make_pattern(
        frame,
        pattern_type="triangle_consolidation",
        direction="neutral",
        confidence=0.56 + min((start_width / end_width - 1.0) * 0.08, 0.16),
        start=min(first_high.index, first_low.index),
        end=len(frame) - 1,
        summary="Detected converging lower highs and higher lows consistent with triangle consolidation.",
        evidence={
            "first_high": round(first_high.price, 4),
            "last_high": round(last_high.price, 4),
            "first_low": round(first_low.price, 4),
            "last_low": round(last_low.price, 4),
            "width_contraction_pct": round((1.0 - end_width / start_width) * 100.0, 2),
        },
    )


def _make_pattern(
    frame: pd.DataFrame,
    *,
    pattern_type: str,
    direction: str,
    confidence: float,
    start: int,
    end: int,
    summary: str,
    evidence: Dict[str, Any],
) -> KlinePattern:
    window = frame.iloc[max(start, 0) : min(end + 1, len(frame))]
    return KlinePattern(
        pattern_type=pattern_type,
        direction=direction,
        confidence=confidence,
        start_index=max(start, 0),
        end_index=min(end, len(frame) - 1),
        price_low=float(window["low"].min()),
        price_high=float(window["high"].max()),
        summary=summary,
        evidence=evidence,
    )


def _dedupe_patterns(patterns: Sequence[KlinePattern]) -> List[KlinePattern]:
    selected: Dict[str, KlinePattern] = {}
    for pattern in patterns:
        key = f"{pattern.pattern_type}:{pattern.start_index}:{pattern.end_index}"
        current = selected.get(key)
        if current is None or pattern.confidence > current.confidence:
            selected[key] = pattern
    return list(selected.values())


def _better(candidate: KlinePattern, current: Optional[KlinePattern]) -> KlinePattern:
    if current is None:
        return candidate
    if (candidate.confidence, candidate.end_index) > (current.confidence, current.end_index):
        return candidate
    return current


def _compact_adjacent_pivots(pivots: Sequence[Pivot], *, prefer: str) -> List[Pivot]:
    if not pivots:
        return []
    compacted: List[Pivot] = []
    for pivot in pivots:
        if not compacted or pivot.index - compacted[-1].index > 2:
            compacted.append(pivot)
            continue
        previous = compacted[-1]
        if prefer == "low" and pivot.price < previous.price:
            compacted[-1] = pivot
        elif prefer == "high" and pivot.price > previous.price:
            compacted[-1] = pivot
    return compacted


def _highest_between(pivots: Sequence[Pivot], start: int, end: int) -> Optional[Pivot]:
    candidates = [item for item in pivots if start < item.index < end]
    return max(candidates, key=lambda item: item.price) if candidates else None


def _lowest_between(pivots: Sequence[Pivot], start: int, end: int) -> Optional[Pivot]:
    candidates = [item for item in pivots if start < item.index < end]
    return min(candidates, key=lambda item: item.price) if candidates else None


def _slice_high(frame: pd.DataFrame, start: int, end: int) -> float:
    return float(frame.iloc[start : end + 1]["high"].max())


def _slice_low(frame: pd.DataFrame, start: int, end: int) -> float:
    return float(frame.iloc[start : end + 1]["low"].min())


def _last_close(frame: pd.DataFrame) -> float:
    return float(frame.iloc[-1]["close"])


def _date_text(value: Any) -> str:
    return pd.Timestamp(value).date().isoformat()


def _first_existing_column(frame: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lower_map = {str(col).lower(): str(col) for col in frame.columns}
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
        lowered = candidate.lower()
        if lowered in lower_map:
            return lower_map[lowered]
    return None


def _pattern_id(code: str, pattern_type: str, start_time: str, end_time: str, price_low: float, price_high: float) -> str:
    raw = f"{code}|{pattern_type}|{start_time}|{end_time}|{price_low}|{price_high}"
    return "kline-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
