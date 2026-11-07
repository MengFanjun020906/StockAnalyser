from datetime import date

import pandas as pd

from src.agent.scoring.composite_snapshot import CompositeSnapshotBuilder
from src.agent.scoring.runtime import score_symbol_composite


def test_snapshot_builder_enforces_as_of_and_fixed_daily_lookback():
    calls = []
    dates = pd.bdate_range(end="2026-08-10", periods=600)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [100 + index * 0.1 for index in range(len(dates))],
            "high": [101 + index * 0.1 for index in range(len(dates))],
            "low": [99 + index * 0.1 for index in range(len(dates))],
            "close": [100.5 + index * 0.1 for index in range(len(dates))],
            "volume": [1000 + index for index in range(len(dates))],
        }
    )

    def fake_loader(stock_code, days, target_date, fallback_to_network):
        calls.append((stock_code, days, target_date, fallback_to_network))
        return frame.copy(), "unit_test"

    snapshot = CompositeSnapshotBuilder(history_loader=fake_loader).build(
        "600519",
        as_of="2026-08-05",
        quote_basis="eod_current",
    )

    assert calls == [("600519", 520, date(2026, 8, 5), True)]
    assert snapshot.as_of == "2026-08-05"
    assert snapshot.metadata["history_end"] == "2026-08-05"
    assert snapshot.metadata["daily_bar_count"] == 520
    assert snapshot.metadata["daily_lookback_bars"] == 520
    assert snapshot.metadata["ema_warmup_bars"] == 200
    assert snapshot.metadata["source"] == "unit_test"
    assert snapshot.metadata["weekly_last_bar_complete"] is False
    assert snapshot.chan.status == "partial"
    assert snapshot.s4.volume_ratio_5d is not None
    assert snapshot.s4.volatility_pct is not None
    assert snapshot.s4.gain_10d_pct is not None
    assert snapshot.s4.status == "partial"
    assert snapshot.s4.guide_line is not None
    assert snapshot.s4.ma57 is not None
    assert snapshot.s4.weekly_provisional is True


def test_shared_runtime_returns_snapshot_and_shadow_verdict():
    snapshot = CompositeSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (pd.DataFrame(), "empty_test")
    )

    payload = score_symbol_composite(
        "600519",
        as_of="2026-08-05",
        snapshot_builder=snapshot,
    )

    assert payload["schema_version"] == "stock-composite-shadow.v1"
    assert payload["snapshot"]["symbol"] == "600519"
    assert payload["composite_verdict"]["score"] is None
    assert payload["composite_verdict"]["verdict_status"] == "not_evaluable"


def test_default_snapshot_loader_requests_full_history_cache_contract(monkeypatch):
    calls = []

    def fake_default_loader(*args, **kwargs):
        calls.append((args, kwargs))
        return pd.DataFrame(), "empty_test"

    monkeypatch.setattr("src.services.history_loader.load_history_df", fake_default_loader)

    CompositeSnapshotBuilder(fallback_to_network=True).build("600519", as_of="2026-08-09")

    assert calls[0][0] == ("600519",)
    assert calls[0][1]["days"] == 520
    assert calls[0][1]["minimum_records"] == 520
    assert calls[0][1]["fallback_to_network"] is True
