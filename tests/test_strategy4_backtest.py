from unittest.mock import patch

import pandas as pd

from src.agent.scoring.strategy4_backtest import evaluate_strategy4_backtest
from src.schemas.composite_verdict import S4FeatureSnapshot


def _bars(count=1000):
    dates = pd.bdate_range(end="2026-08-07", periods=count)
    close = [100.0 + index * 0.2 for index in range(count)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [value + 1.0 for value in close],
            "low": [value - 1.0 for value in close],
            "close": [value + 0.5 for value in close],
            "volume": [1000.0] * count,
        }
    )


def test_backtest_requires_listing_and_limit_metadata():
    result = evaluate_strategy4_backtest(_bars(), as_of="2026-08-07")

    assert result.status == "insufficient_data"
    assert result.reason == "listing_or_limit_metadata_missing"


def test_backtest_uses_next_open_and_ten_day_close_with_explicit_metadata():
    frame = _bars()
    with patch(
        "src.agent.scoring.strategy4_backtest.compute_strategy4_features",
        return_value=S4FeatureSnapshot(status="ok", golden_cross_today=True, all_ok=True),
    ):
        result = evaluate_strategy4_backtest(
            frame,
            as_of="2026-08-07",
            listing_date="2020-01-01",
            is_st=False,
            limit_pct=0.10,
        )

    assert result.status == "ok"
    assert result.sample_count >= 20
    assert result.win_count == result.sample_count
    assert result.win_rate_pct == 100.0
