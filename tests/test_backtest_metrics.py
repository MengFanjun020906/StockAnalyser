# -*- coding: utf-8 -*-
"""Portfolio metric null/edge-case and arithmetic tests."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest.entities import EquityPoint
from src.backtest.metrics import compute_metrics
from src.backtest.schemas import DataQuality, RiskProfile, SourcePipeline


def _points(values: list[str], *, start: date = date(2026, 1, 5)) -> list[EquityPoint]:
    result = []
    peak = Decimal("100")
    for index, value in enumerate(values):
        equity = Decimal(value)
        if equity > peak:
            peak = equity
        result.append(
            EquityPoint(
                session=start + timedelta(days=index),
                timestamp=None,
                cash=equity,
                reserved_cash=Decimal("0"),
                market_value=Decimal("0"),
                equity=equity,
                peak_equity=peak,
                drawdown=equity / peak - Decimal("1"),
                stale=False,
                benchmark_value=None,
                granularity="1d",
                quality="complete",
            )
        )
    return result


def _metrics(points, **kwargs):
    return compute_metrics(
        equity_points=points,
        initial_cash=Decimal("100"),
        fills=[],
        orders=[],
        closed_trades=[],
        plans_count=0,
        diagnostics={},
        benchmark=kwargs.pop("benchmark", None),
        annualization_days=252,
        risk_free_rate=Decimal("0"),
        fees_paid=Decimal("0"),
        slippage_paid=Decimal("0"),
        source_pipeline=SourcePipeline.SINGLE_STOCK,
        risk_profile=RiskProfile.BALANCED,
        **kwargs,
    )


def test_hand_calculated_nav_100_110_99():
    metrics = _metrics(_points(["110", "99"]))
    assert metrics.total_return_pct == -1.0
    assert metrics.max_drawdown_pct == -10.0
    assert metrics.trading_days == 2
    assert metrics.daily_return_count == 2
    assert metrics.annualized_return_pct is None
    assert metrics.not_applicable["annualized_return_pct"] == "short_sample"
    assert metrics.sharpe is None
    assert metrics.not_applicable["sharpe"] == "short_sample"


def test_short_sample_and_zero_variance_are_null_not_infinity():
    metrics = _metrics(_points(["100"] * 25))
    assert metrics.total_return_pct == 0.0
    assert metrics.volatility_pct is None
    assert metrics.not_applicable["volatility_pct"] == "zero_variance"
    assert metrics.sharpe is None
    assert metrics.not_applicable["sharpe"] == "zero_variance"
    assert metrics.max_drawdown_pct == 0.0


def test_empty_equity_is_unavailable_with_reason():
    metrics = _metrics([])
    assert metrics.quality is DataQuality.UNAVAILABLE
    assert metrics.not_applicable["all"] == "no_equity_points"
    assert metrics.total_return_pct is None


def test_benchmark_excess_uses_same_window():
    points = _points(["110", "121"])
    benchmark = {points[0].session: Decimal("100"), points[1].session: Decimal("105")}
    metrics = _metrics(points, benchmark=benchmark)
    assert metrics.benchmark_return_pct == 5.0
    assert round(metrics.excess_return_pct or 0, 6) == 16.0


def test_drawdown_recovery_dates_are_tracked():
    points = _points(["90", "95", "110", "99", "111"])
    metrics = _metrics(points)
    assert metrics.max_drawdown_pct == -10.0
    assert metrics.max_drawdown_start == points[2].session
    assert metrics.max_drawdown_trough == points[3].session
    assert metrics.max_drawdown_recovery == points[4].session
