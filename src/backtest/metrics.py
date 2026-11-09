# -*- coding: utf-8 -*-
"""Portfolio metrics with explicit null/not-applicable semantics."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from statistics import mean, stdev
from typing import Any, Iterable

from .entities import ClosedTrade, EquityPoint, Fill, Order
from .schemas import BacktestMetrics, DataQuality, RiskProfile, SourcePipeline


def compute_metrics(
    *,
    equity_points: list[EquityPoint],
    initial_cash: Decimal,
    fills: list[Fill],
    orders: list[Order],
    closed_trades: list[ClosedTrade],
    plans_count: int,
    diagnostics: dict[str, Any],
    benchmark: dict[date, Decimal] | None,
    annualization_days: int,
    risk_free_rate: Decimal,
    fees_paid: Decimal,
    slippage_paid: Decimal,
    quality_flags: Iterable[str] = (),
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK,
    risk_profile: RiskProfile = RiskProfile.BALANCED,
    benchmark_status: str = "not_requested",
) -> BacktestMetrics:
    flags = set(quality_flags)
    na: dict[str, str] = {}
    if not equity_points:
        return BacktestMetrics(
            quality=DataQuality.UNAVAILABLE,
            source_pipeline=source_pipeline,
            risk_profile=risk_profile,
            not_applicable={"all": "no_equity_points"},
            single_trade=_single_trade_stats(closed_trades, plans_count, diagnostics),
        )

    daily_returns = _daily_returns(equity_points, initial_cash)
    n = len(daily_returns)
    final_equity = equity_points[-1].equity
    total_return = _ratio(final_equity, initial_cash)

    annualized = None
    if n < 20:
        na["annualized_return_pct"] = "short_sample"
        na["sharpe"] = "short_sample"
        na["volatility_pct"] = "short_sample"
    elif total_return <= -1:
        na["annualized_return_pct"] = "non_positive_equity"
    else:
        annualized = (1.0 + total_return) ** (annualization_days / n) - 1.0

    volatility = None
    sharpe = None
    if n >= 20:
        variance = stdev(daily_returns) if n > 1 else 0.0
        if variance > 1e-12:
            volatility = variance * math.sqrt(annualization_days)
            rf_daily = (1.0 + float(risk_free_rate)) ** (1.0 / annualization_days) - 1.0
            excess = [item - rf_daily for item in daily_returns]
            std_excess = stdev(excess) if len(excess) > 1 else 0.0
            if std_excess > 1e-12:
                sharpe = mean(excess) / std_excess * math.sqrt(annualization_days)
            else:
                na["sharpe"] = "zero_variance"
        else:
            na["volatility_pct"] = "zero_variance"
            na["sharpe"] = "zero_variance"

    max_dd, dd_start, dd_trough, dd_recovery = _drawdown_details(equity_points)
    benchmark_return = None
    excess_return = None
    if benchmark:
        bench_values = [benchmark.get(point.session) for point in equity_points]
        valid = [(point.session, value) for point, value in zip(equity_points, bench_values) if value]
        if len(valid) >= 2 and valid[0][1] > 0:
            benchmark_return = _ratio(valid[-1][1], valid[0][1])
            excess_return = total_return - benchmark_return
        else:
            na["benchmark_return_pct"] = "benchmark_unavailable"
            na["excess_return_pct"] = "benchmark_unavailable"
    else:
        reason = "benchmark_unavailable" if benchmark_status == "unavailable" else "benchmark_not_requested"
        na["benchmark_return_pct"] = reason
        na["excess_return_pct"] = reason

    cash_ratios = [
        float(point.cash / point.equity)
        for point in equity_points
        if point.equity > 0
    ]
    position_ratios = [
        float(point.market_value / point.equity)
        for point in equity_points
        if point.equity > 0
    ]
    avg_equity = sum((point.equity for point in equity_points), Decimal("0")) / Decimal(len(equity_points))
    total_traded = sum((fill.gross_amount for fill in fills), Decimal("0"))
    turnover = float(total_traded / avg_equity) if avg_equity > 0 else None
    if turnover is None:
        na["turnover_pct"] = "zero_average_equity"

    stale_sessions = sum(1 for point in equity_points if point.stale)
    if stale_sessions:
        flags.add("stale_equity")
    quality = DataQuality.PARTIAL if flags or diagnostics.get("ambiguous_fills", 0) else DataQuality.COMPLETE
    if stale_sessions and stale_sessions == len(equity_points):
        quality = DataQuality.UNAVAILABLE

    return BacktestMetrics(
        quality=quality,
        source_pipeline=source_pipeline,
        risk_profile=risk_profile,
        total_return_pct=total_return * 100.0,
        annualized_return_pct=annualized * 100.0 if annualized is not None else None,
        volatility_pct=volatility * 100.0 if volatility is not None else None,
        sharpe=sharpe,
        max_drawdown_pct=max_dd * 100.0,
        max_drawdown_start=dd_start,
        max_drawdown_trough=dd_trough,
        max_drawdown_recovery=dd_recovery,
        benchmark_return_pct=benchmark_return * 100.0 if benchmark_return is not None else None,
        excess_return_pct=excess_return * 100.0 if excess_return is not None else None,
        cash_ratio_pct=mean(cash_ratios) * 100.0 if cash_ratios else None,
        average_position_pct=mean(position_ratios) * 100.0 if position_ratios else None,
        turnover_pct=turnover * 100.0 if turnover is not None else None,
        fee_drag_pct=float(fees_paid / initial_cash * Decimal("100")) if initial_cash > 0 else None,
        slippage_drag_pct=float(slippage_paid / initial_cash * Decimal("100")) if initial_cash > 0 else None,
        trading_days=len(equity_points),
        daily_return_count=n,
        not_applicable=na,
        single_trade=_single_trade_stats(closed_trades, plans_count, diagnostics),
    )


def _daily_returns(points: list[EquityPoint], initial_cash: Decimal) -> list[float]:
    returns: list[float] = []
    previous = initial_cash
    for point in points:
        if previous > 0:
            returns.append(float(point.equity / previous - Decimal("1")))
        previous = point.equity
    return returns


def _ratio(value: Decimal, base: Decimal) -> float:
    if base <= 0:
        return 0.0
    return float(value / base - Decimal("1"))


def _drawdown_details(
    points: list[EquityPoint],
) -> tuple[float, date | None, date | None, date | None]:
    peak = points[0].equity
    peak_session = points[0].session
    max_dd = 0.0
    dd_start: date | None = None
    dd_trough: date | None = None
    recovery: date | None = None
    for point in points:
        if point.equity > peak:
            peak = point.equity
            peak_session = point.session
        if peak > 0:
            drawdown = float(point.equity / peak - Decimal("1"))
            if drawdown < max_dd:
                max_dd = drawdown
                dd_start = peak_session
                dd_trough = point.session
                recovery = None
        if max_dd < 0 and dd_trough is not None and point.session > dd_trough and point.equity >= peak:
            recovery = point.session
    return max_dd, dd_start, dd_trough, recovery


def _single_trade_stats(
    trades: list[ClosedTrade],
    plans_count: int,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.quantity > 0]
    wins = [trade for trade in closed if trade.realized_pnl > 0]
    losses = [trade for trade in closed if trade.realized_pnl < 0]
    flats = [trade for trade in closed if trade.realized_pnl == 0]
    avg_win = mean([trade.return_pct for trade in wins]) if wins else None
    avg_loss = mean([trade.return_pct for trade in losses]) if losses else None
    payoff = (
        abs(avg_win / avg_loss)
        if avg_win is not None and avg_loss not in (None, 0)
        else None
    )
    expectancy = (
        (len(wins) / len(closed)) * avg_win + (len(losses) / len(closed)) * avg_loss
        if closed and avg_win is not None and avg_loss is not None
        else None
    )
    stats: dict[str, Any] = {
        "plans_included": plans_count,
        "closed_trades": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else None,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "payoff_ratio": payoff,
        "expectancy_pct": expectancy,
        "avg_holding_days": mean([trade.holding_days for trade in closed]) if closed else None,
        "avg_mfe_pct": mean([trade.mfe_pct for trade in closed if trade.mfe_pct is not None])
        if any(trade.mfe_pct is not None for trade in closed)
        else None,
        "avg_mae_pct": mean([trade.mae_pct for trade in closed if trade.mae_pct is not None])
        if any(trade.mae_pct is not None for trade in closed)
        else None,
        "unclosed_trades": diagnostics.get("open_positions", 0),
        "exit_reasons": _exit_reason_counts(closed),
        "fill_rate_pct": diagnostics.get("fill_rate_pct"),
        "not_triggered": diagnostics.get("not_triggered", 0),
        "gate_blocked": diagnostics.get("gate_blocked", 0),
        "insufficient_cash": diagnostics.get("insufficient_cash", 0),
        "partial_fills": diagnostics.get("partial_fills", 0),
        "ambiguous_fills": diagnostics.get("ambiguous_fills", 0),
    }
    return stats


def _exit_reason_counts(trades: list[ClosedTrade]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    return dict(sorted(counts.items()))


__all__ = ["compute_metrics"]
