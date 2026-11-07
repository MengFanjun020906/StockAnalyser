# -*- coding: utf-8 -*-
"""Tests for deterministic Chan and SMC price-structure analysis."""

from unittest.mock import patch

import pandas as pd

from src.agent import factory
from src.agent.structure import PriceBar, analyze_price_structure, classify_chan_signals, coerce_price_bars
from src.agent.tools.analysis_tools import _handle_analyze_price_structure, analyze_price_structure_tool


def _bars(count: int = 90) -> list[PriceBar]:
    bars: list[PriceBar] = []
    price = 10.0
    for i in range(count):
        wave = ((i % 12) - 6) * 0.18
        drift = i * 0.035
        close = price + drift + wave
        open_ = close - (0.12 if i % 2 == 0 else -0.1)
        high = max(open_, close) + 0.35 + (0.08 if i % 7 == 0 else 0)
        low = min(open_, close) - 0.35 - (0.08 if i % 9 == 0 else 0)
        volume = 1000 + i * 10
        bars.append(PriceBar(index=i, time=f"2026-01-{(i % 28) + 1:02d}", open=open_, high=high, low=low, close=close, volume=volume))
    return bars


def test_price_structure_outputs_chan_and_smc_sections():
    result = analyze_price_structure(_bars(100))

    assert result["status"] == "ok"
    assert result["chan"]["fractal_count"] > 0
    assert "latest_pens" in result["chan"]
    if result["chan"]["latest_pens"]:
        assert "macd_area" in result["chan"]["latest_pens"][-1]["power"]
    assert "unfinished_pen" in result["chan"]
    assert result["smc"]["swing_count"] > 0
    assert "bos" in result["smc"]
    assert "choch" in result["smc"]
    assert "order_blocks" in result["smc"]
    assert "fair_value_gaps" in result["smc"]


def test_price_structure_detects_order_block_and_fvg():
    bars = _bars(35)
    bars.extend([
        PriceBar(index=35, time="2026-02-05", open=13.8, high=14.0, low=13.1, close=13.2, volume=1100),
        PriceBar(index=36, time="2026-02-06", open=13.3, high=15.2, low=13.2, close=15.1, volume=3200),
        PriceBar(index=37, time="2026-02-07", open=15.2, high=15.6, low=15.05, close=15.4, volume=2600),
        PriceBar(index=38, time="2026-02-08", open=16.0, high=16.4, low=15.8, close=16.2, volume=2800),
    ])

    result = analyze_price_structure(bars)

    assert any(item["type"] == "bullish" for item in result["smc"]["order_blocks"])
    assert any(item["type"] == "bullish" for item in result["smc"]["fair_value_gaps"])


def test_price_structure_insufficient_data():
    result = analyze_price_structure(_bars(10))

    assert result["status"] == "insufficient_data"
    assert result["data_quality"] == "insufficient"


def test_coerce_price_bars_from_dataframe_records():
    df = pd.DataFrame(
        [
            {"date": "2026-01-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
            {"date": "2026-01-02", "open": 10.5, "high": 11.5, "low": 10.1, "close": 11, "volume": 1200},
        ]
    )

    bars = coerce_price_bars(df.to_dict("records"))

    assert len(bars) == 2
    assert bars[0].time == "2026-01-01"
    assert bars[1].close == 11.0


def test_analyze_price_structure_tool_registered_in_factory():
    assert analyze_price_structure_tool.name == "analyze_price_structure"
    factory._TOOL_REGISTRY = None
    registry = factory.get_tool_registry()

    assert "analyze_price_structure" in registry.list_names()


def test_analyze_price_structure_tool_handler_uses_history_loader():
    rows = [bar.to_dict() for bar in _bars(80)]
    df = pd.DataFrame(rows)
    with patch("src.services.history_loader.load_history_df", return_value=(df, "unit_test")):
        result = _handle_analyze_price_structure("600519", days=80)

    assert result["code"] == "600519"
    assert result["source"] == "unit_test"
    assert result["status"] == "ok"
    assert result["chan"]["status"] in {"ok", "limited"}


def test_chan_signal_classifier_labels_bottom_divergence_and_first_buy():
    pens = [
        {
            "id": 1,
            "direction": "down",
            "start": {"type": "top", "index": 2, "price": 12.0},
            "end": {"type": "bottom", "index": 8, "price": 9.0},
            "power": {"macd_area": 10.0, "macd_area_ratio_vs_prev": None},
        },
        {
            "id": 2,
            "direction": "up",
            "start": {"type": "bottom", "index": 8, "price": 9.0},
            "end": {"type": "top", "index": 14, "price": 11.5},
            "power": {"macd_area": 8.0, "macd_area_ratio_vs_prev": 0.8},
        },
        {
            "id": 3,
            "direction": "down",
            "start": {"type": "top", "index": 14, "price": 11.5},
            "end": {"type": "bottom", "index": 20, "price": 8.5},
            "power": {"macd_area": 7.0, "macd_area_ratio_vs_prev": 0.875},
        },
    ]

    signals = classify_chan_signals(
        pens=pens,
        centers=[],
        macd_bars=[0.2, -2.0, -1.5, -1.0, -0.5],
        timeframe="1d",
    )
    by_name = {item["name"]: item for item in signals}

    assert by_name["bottom_divergence"]["metrics"]["macd_area_ratio"] == 0.7
    assert by_name["first_buy"]["triggered"] is True
    assert by_name["bottom_exhaustion"]["triggered"] is True


def test_chan_signal_classifier_labels_top_divergence_and_exhaustion():
    pens = [
        {
            "id": 1,
            "direction": "up",
            "start": {"type": "bottom", "index": 2, "price": 9.0},
            "end": {"type": "top", "index": 8, "price": 12.0},
            "power": {"macd_area": 10.0},
        },
        {
            "id": 2,
            "direction": "down",
            "start": {"type": "top", "index": 8, "price": 12.0},
            "end": {"type": "bottom", "index": 14, "price": 10.0},
            "power": {"macd_area": 8.0},
        },
        {
            "id": 3,
            "direction": "up",
            "start": {"type": "bottom", "index": 14, "price": 10.0},
            "end": {"type": "top", "index": 20, "price": 12.5},
            "power": {"macd_area": 7.0},
        },
    ]

    signals = classify_chan_signals(
        pens=pens,
        centers=[],
        macd_bars=[-0.2, 2.0, 1.5, 1.0, 0.5],
        timeframe="1d",
    )
    by_name = {item["name"]: item for item in signals}

    assert by_name["top_divergence"]["metrics"]["macd_area_ratio"] == 0.7
    assert by_name["first_sell"]["triggered"] is True
    assert by_name["top_exhaustion"]["triggered"] is True


def test_chan_signal_classifier_labels_second_buy_on_higher_low():
    pens = [
        {
            "id": 1,
            "direction": "down",
            "start": {"type": "top", "index": 2, "price": 12.0},
            "end": {"type": "bottom", "index": 8, "price": 9.0},
            "power": {"macd_area": 10.0},
        },
        {
            "id": 2,
            "direction": "up",
            "start": {"type": "bottom", "index": 8, "price": 9.0},
            "end": {"type": "top", "index": 14, "price": 11.5},
            "power": {"macd_area": 8.0},
        },
        {
            "id": 3,
            "direction": "down",
            "start": {"type": "top", "index": 14, "price": 11.5},
            "end": {"type": "bottom", "index": 20, "price": 9.5},
            "power": {"macd_area": 6.0},
        },
    ]

    signals = classify_chan_signals(
        pens=pens,
        centers=[],
        macd_bars=[0.1, -0.5, -0.8, -1.0],
        timeframe="1d",
    )

    second_buy = next(item for item in signals if item["name"] == "second_buy")
    assert second_buy["metrics"]["previous_low"] == 9.0
    assert second_buy["metrics"]["current_low"] == 9.5


def test_chan_signal_classifier_labels_first_breakout_high_above_center():
    pens = [
        {
            "id": 4,
            "direction": "up",
            "start": {"type": "bottom", "index": 16, "price": 10.5},
            "end": {"type": "top", "index": 20, "price": 12.0},
            "power": {"macd_area": 6.0},
        }
    ]
    centers = [{"id": 1, "ZG": 11.0, "ZD": 9.8, "start_index": 4, "end_index": 14}]

    signals = classify_chan_signals(
        pens=pens,
        centers=centers,
        macd_bars=[-0.2, 0.1, 0.3, 0.5],
        timeframe="1d",
    )

    breakout = next(item for item in signals if item["name"] == "center_breakout")
    assert breakout["metrics"]["center_id"] == 1
    assert breakout["metrics"]["breakout_price"] == 12.0


def test_chan_signal_classifier_requires_pullback_for_third_buy():
    pens = [
        {"id": 1, "direction": "up", "start": {"index": 16, "price": 10.5}, "end": {"index": 20, "price": 12.0}, "power": {}},
        {"id": 2, "direction": "down", "start": {"index": 20, "price": 12.0}, "end": {"index": 24, "price": 11.2}, "power": {}},
    ]
    signals = classify_chan_signals(
        pens=pens,
        centers=[{"id": 1, "ZG": 11.0, "ZD": 9.8, "start_index": 4, "end_index": 14}],
        macd_bars=[0.2, 0.1, -0.1, -0.2],
        timeframe="1d",
    )
    third_buy = next(item for item in signals if item["name"] == "third_buy")
    assert third_buy["metrics"]["breakout_pen_id"] == 1
    assert third_buy["metrics"]["pullback_price"] == 11.2


def test_chan_signal_classifier_labels_second_sell_on_lower_high():
    pens = [
        {"id": 1, "direction": "up", "start": {"index": 2, "price": 9.0}, "end": {"index": 8, "price": 12.0}, "power": {"macd_area": 10.0}},
        {"id": 2, "direction": "down", "start": {"index": 8, "price": 12.0}, "end": {"index": 14, "price": 10.0}, "power": {"macd_area": 8.0}},
        {"id": 3, "direction": "up", "start": {"index": 14, "price": 10.0}, "end": {"index": 20, "price": 11.5}, "power": {"macd_area": 6.0}},
    ]
    signals = classify_chan_signals(pens=pens, centers=[], macd_bars=[], timeframe="1d")
    second_sell = next(item for item in signals if item["name"] == "second_sell")
    assert second_sell["metrics"]["previous_high"] == 12.0
    assert second_sell["metrics"]["current_high"] == 11.5


def test_chan_signal_classifier_requires_pullback_for_third_sell():
    pens = [
        {"id": 1, "direction": "down", "start": {"index": 16, "price": 10.5}, "end": {"index": 20, "price": 9.0}, "power": {}},
        {"id": 2, "direction": "up", "start": {"index": 20, "price": 9.0}, "end": {"index": 24, "price": 9.5}, "power": {}},
    ]
    signals = classify_chan_signals(
        pens=pens,
        centers=[{"id": 1, "ZG": 11.0, "ZD": 9.8, "start_index": 4, "end_index": 14}],
        macd_bars=[],
        timeframe="1d",
    )
    third_sell = next(item for item in signals if item["name"] == "third_sell")
    assert third_sell["metrics"]["breakdown_pen_id"] == 1
    assert third_sell["metrics"]["pullback_price"] == 9.5
