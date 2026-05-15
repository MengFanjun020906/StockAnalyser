# -*- coding: utf-8 -*-
"""Tests for deterministic Chan and SMC price-structure analysis."""

from unittest.mock import patch

import pandas as pd

from src.agent import factory
from src.agent.structure import PriceBar, analyze_price_structure, coerce_price_bars
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
