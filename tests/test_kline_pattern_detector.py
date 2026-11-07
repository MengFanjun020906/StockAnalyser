# -*- coding: utf-8 -*-
"""Tests for deterministic K-line chart pattern detection."""

from __future__ import annotations

import pandas as pd

from src.services.kline_pattern_detector import detect_kline_patterns


def _frame_from_closes(closes: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-07-24", periods=len(closes))
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            {
                "timestamp": dates[idx],
                "open": close * (0.998 if idx % 2 == 0 else 1.002),
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000 + idx * 10_000,
                "amount": close * (1_000_000 + idx * 10_000),
            }
        )
    return pd.DataFrame(rows)


def test_detects_double_bottom_w_pattern() -> None:
    closes = (
        [112, 110, 108, 106, 104, 101, 98, 95, 92, 90]
        + [93, 96, 100, 103, 105, 104, 101, 98, 95, 92, 91]
        + [93, 96, 99, 102, 104, 105, 106, 107, 108]
    )

    patterns = detect_kline_patterns(_frame_from_closes(closes), max_patterns=5)

    double_bottom = next(item for item in patterns if item["pattern_type"] == "double_bottom")
    assert double_bottom["label"] == "W底"
    assert double_bottom["direction"] == "bullish"
    assert double_bottom["red_box"]["y0"] < double_bottom["red_box"]["y1"]
    assert double_bottom["evidence"]["detector"] == "deterministic_kline_patterns_v1"
    assert double_bottom["evidence"]["neckline"] > double_bottom["price_low"]


def test_detects_double_top_m_pattern() -> None:
    closes = (
        [78, 79, 80, 83, 86, 90, 94, 98, 102, 106, 110]
        + [106, 102, 98, 95, 94, 96, 100, 104, 108, 109]
        + [106, 102, 98, 94, 90, 88, 86, 84, 82, 80]
    )

    patterns = detect_kline_patterns(_frame_from_closes(closes), max_patterns=5)

    double_top = next(item for item in patterns if item["pattern_type"] == "double_top")
    assert double_top["label"] == "M顶"
    assert double_top["direction"] == "bearish"
    assert double_top["evidence"]["neckline"] < double_top["price_high"]


def test_monotonic_trend_does_not_emit_reversal_patterns() -> None:
    closes = [50 + idx * 0.5 for idx in range(80)]

    patterns = detect_kline_patterns(_frame_from_closes(closes), max_patterns=5)

    pattern_types = {item["pattern_type"] for item in patterns}
    assert "double_bottom" not in pattern_types
    assert "double_top" not in pattern_types
    assert "head_shoulders_top" not in pattern_types
    assert "inverse_head_shoulders_bottom" not in pattern_types
