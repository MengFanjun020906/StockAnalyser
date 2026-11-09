# -*- coding: utf-8 -*-
"""Display-only multi-index benchmark normalization tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.backtest.benchmarks import build_benchmark_payload


def test_benchmark_payload_normalizes_to_initial_equity_and_caches(tmp_path):
    sessions = [date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8)]

    def loader(symbol: str, _start: date, _end: date):
        assert symbol == "000001.SH"
        return {
            sessions[0]: Decimal("100"),
            sessions[1]: Decimal("110"),
            sessions[2]: Decimal("121"),
        }

    cache = tmp_path / "benchmarks.json"
    payload = build_benchmark_payload(
        run_id="bt-test",
        sessions=sessions,
        initial_equity=Decimal("100000"),
        loader=loader,
        cache_path=cache,
        strategy_return_pct=-5.0,
        specs=[("000001.SH", "上证指数")],
    )
    points = payload["series"][0]["points"]
    assert payload["benchmark_returns"]["000001.SH"] == pytest.approx(21.0)
    assert payload["excess_vs_shanghai_pct"] == pytest.approx(-26.0)
    assert payload["shanghai_return_pct"] == pytest.approx(21.0)
    assert [point["value"] for point in points] == [100000.0, 110000.0, 121000.0]
    assert cache.exists()

    def broken_loader(_symbol: str, _start: date, _end: date):
        raise AssertionError("cache should be used")

    cached = build_benchmark_payload(
        run_id="bt-test",
        sessions=sessions,
        initial_equity=Decimal("100000"),
        loader=broken_loader,
        cache_path=cache,
        specs=[("000001.SH", "上证指数")],
    )
    assert cached["series"][0]["points"][2]["value"] == 121000.0
