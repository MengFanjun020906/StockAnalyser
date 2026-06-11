# -*- coding: utf-8 -*-

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from src.agent.tools.analysis_tools import _handle_analyze_trend


def test_analyze_trend_includes_bollinger_metrics() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-03-01", periods=60, freq="D"),
            "open": [10 + i * 0.1 for i in range(60)],
            "high": [10.2 + i * 0.1 for i in range(60)],
            "low": [9.8 + i * 0.1 for i in range(60)],
            "close": [10 + i * 0.1 for i in range(60)],
            "volume": [100000 + i * 1000 for i in range(60)],
        }
    )

    with patch("src.services.history_loader.load_history_df", return_value=(df, "test")):
        result = _handle_analyze_trend("600519")

    assert "error" not in result
    assert result["bollinger"]["status"] == "ok"
    assert result["bollinger"]["period"] == 20
    assert result["bollinger"]["upper"] is not None
    assert result["bollinger"]["mid"] is not None
    assert result["bollinger"]["lower"] is not None
    assert result["bollinger"]["position_label"]
