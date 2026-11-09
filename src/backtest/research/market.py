# -*- coding: utf-8 -*-
"""As-of market snapshots and deterministic research plans for P3.

The P3 Agent may select and confirm, but executable prices are always produced
by deterministic code.  Free-form LLM text never becomes an order price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from src.backtest.calendar import CnTradingCalendar
from src.backtest.data_view import InMemoryDataView
from src.backtest.entities import DailyBar
from src.backtest.rules import MarketRule
from src.backtest.schemas import (
    ExperimentConfig,
    ReplayPlan,
    SourcePipeline,
    fingerprint_value,
)


@dataclass
class TechnicalSnapshot:
    symbol: str
    as_of: date
    close: Decimal
    prev_close: Decimal
    return_5d: float
    return_20d: float
    return_60d: float
    ma20: Decimal
    ma60: Decimal
    atr14: Decimal
    atr_pct: float
    high20: Decimal
    low20: Decimal
    volume_ratio: float
    score: float
    bars_count: int
    one_lot_cost: Decimal | None = None
    one_lot_risk_pct: float | None = None
    flags: list[str] = field(default_factory=list)

    def to_prompt_row(self) -> dict[str, Any]:
        row = {
            "symbol": self.symbol,
            "close": float(self.close),
            "ret_5d_pct": round(self.return_5d * 100, 2),
            "ret_20d_pct": round(self.return_20d * 100, 2),
            "ret_60d_pct": round(self.return_60d * 100, 2),
            "ma20": float(self.ma20),
            "ma60": float(self.ma60),
            "atr_pct": round(self.atr_pct * 100, 2),
            "high20": float(self.high20),
            "low20": float(self.low20),
            "volume_ratio": round(self.volume_ratio, 2),
            "score": round(self.score, 4),
            "bars": self.bars_count,
        }
        if self.one_lot_cost is not None:
            row["one_lot_cost"] = float(self.one_lot_cost)
        if self.one_lot_risk_pct is not None:
            row["one_lot_risk_pct"] = round(self.one_lot_risk_pct, 3)
        return row


@dataclass
class ResearchPlanDraft:
    plan: ReplayPlan
    snapshot: TechnicalSnapshot
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan.plan_key,
            "symbol": self.plan.symbol,
            "entry_low": str(self.plan.entry_low),
            "entry_high": str(self.plan.entry_high),
            "stop_loss": str(self.plan.stop_loss),
            "target_1": str(self.plan.target_1),
            "target_2": str(self.plan.target_2),
            "target_position_pct": str(self.plan.target_position_pct),
            "reason": self.reason,
        }


class AsOfMarketView:
    """Only bars with session <= as_of are exposed."""

    def __init__(
        self,
        data: InMemoryDataView,
        calendar: CnTradingCalendar,
        *,
        min_history_bars: int = 60,
    ) -> None:
        self.data = data
        self.calendar = calendar
        self.min_history_bars = min_history_bars

    def snapshot(self, symbol: str, as_of: date) -> tuple[TechnicalSnapshot | None, str | None]:
        bars = self.data.daily_bars(symbol, as_of)
        if len(bars) < self.min_history_bars:
            return None, "insufficient_history"
        if bars[-1].session != as_of and bars[-1].session < as_of:
            # Use the last completed bar, but never a future bar.
            as_of = bars[-1].session
        closes = [bar.close for bar in bars]
        close = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else close
        if close <= 0:
            return None, "invalid_close"
        ret_5 = _return(closes, 5)
        ret_20 = _return(closes, 20)
        ret_60 = _return(closes, 60)
        ma20 = _mean(closes[-20:])
        ma60 = _mean(closes[-60:]) if len(closes) >= 60 else _mean(closes)
        atr = _atr14(bars)
        atr_pct = float(atr / close) if close > 0 else 0.0
        window20 = bars[-20:]
        high20 = max(bar.high for bar in window20)
        low20 = min(bar.low for bar in window20)
        volume_ratio = _volume_ratio(bars)
        score = (
            0.35 * ret_20
            + 0.20 * ret_60
            + 0.20 * float(close / ma20 - Decimal("1"))
            + 0.10 * float(close / ma60 - Decimal("1"))
            + 0.10 * min(volume_ratio, 3.0)
            + 0.05 * (1.0 if close >= ma20 else -1.0)
        )
        return (
            TechnicalSnapshot(
                symbol=symbol,
                as_of=as_of,
                close=close,
                prev_close=prev_close,
                return_5d=ret_5,
                return_20d=ret_20,
                return_60d=ret_60,
                ma20=ma20,
                ma60=ma60,
                atr14=atr,
                atr_pct=atr_pct,
                high20=high20,
                low20=low20,
                volume_ratio=volume_ratio,
                score=score,
                bars_count=len(bars),
            ),
            None,
        )

    def universe_snapshots(
        self,
        symbols: list[str],
        as_of: date,
    ) -> tuple[dict[str, TechnicalSnapshot], dict[str, str]]:
        valid: dict[str, TechnicalSnapshot] = {}
        gaps: dict[str, str] = {}
        for symbol in symbols:
            snapshot, reason = self.snapshot(symbol, as_of)
            if snapshot is None:
                gaps[symbol] = reason or "unavailable"
            else:
                valid[symbol] = snapshot
        return valid, gaps

    def rule(self, symbol: str, session: date) -> MarketRule:
        return self.data.rule(symbol, session)

    def tool_status(self) -> dict[str, str]:
        # Critical tools are served by the frozen snapshot. Non-critical
        # research tools are intentionally declared as gaps in the first slice.
        return {
            "daily_bars": "available",
            "trading_calendar": "available",
            "market_rules": "available",
            "corporate_actions": "available",
            "fundamental": "gap",
            "news": "gap",
            "knowledge_graph": "gap",
            "realtime_quote": "blocked",
            "realtime_search": "blocked",
        }


def build_research_plan(
    *,
    config: ExperimentConfig,
    snapshot: TechnicalSnapshot,
    rule: MarketRule,
    available_at: datetime,
    valid_from: date,
    rank: int,
) -> ResearchPlanDraft | None:
    """Build a deterministic, machine-auditable plan from as-of technicals."""
    if rule.tick_size <= 0 or snapshot.close <= 0 or snapshot.atr14 <= 0:
        return None
    plan_key = f"research:{snapshot.symbol}:{snapshot.as_of.isoformat()}:v1"
    research = config.research
    entry_mode = "pullback"
    if research is not None:
        if research.entry_mode.value == "breakout":
            entry_mode = "breakout"
        elif research.entry_mode.value == "adaptive":
            # Near a 20-day high in an uptrend: continuation entry.  Otherwise
            # keep the pullback entry; this avoids waiting for MA20 forever.
            if snapshot.close >= snapshot.ma20 and snapshot.close >= snapshot.high20 * Decimal("0.97"):
                entry_mode = "breakout"
    if entry_mode == "breakout":
        breakout_level = snapshot.high20
        entry_low = _floor_tick(breakout_level * Decimal("0.999"), rule.tick_size)
        entry_high = _ceil_tick(breakout_level * Decimal("1.005"), rule.tick_size)
        stop_loss = _floor_tick(entry_low - snapshot.atr14 * Decimal("1.5"), rule.tick_size)
        reason = "atr_breakout"
    else:
        # Pullback/continuation entry: slightly below the last close and MA20.
        entry_reference = min(snapshot.close, max(snapshot.ma20, snapshot.low20))
        entry_low = _floor_tick(entry_reference * Decimal("0.99"), rule.tick_size)
        entry_high = _ceil_tick(entry_reference * Decimal("1.005"), rule.tick_size)
        stop_loss = _floor_tick(entry_low - snapshot.atr14 * Decimal("1.5"), rule.tick_size)
        reason = "atr_pullback"
    if entry_low <= 0 or entry_high <= entry_low or stop_loss <= 0 or stop_loss >= entry_low:
        return None
    risk = entry_high - stop_loss
    if risk <= 0:
        return None
    target_1 = _ceil_tick(entry_high + risk * Decimal("2"), rule.tick_size)
    target_2 = _ceil_tick(entry_high + risk * Decimal("3"), rule.tick_size)
    policy = config.risk_policy
    target_pct = Decimal("20")
    if policy is not None:
        target_pct = min(policy.max_single_position_pct, config.max_single_position_pct)
    if config.research and config.research.max_position_pct is not None:
        target_pct = min(target_pct, config.research.max_position_pct)
    target_pct = min(target_pct, Decimal("100"))
    order_valid_days = (
        config.research.order_valid_trading_days if config.research is not None else 15
    )
    source_hash = fingerprint_value(
        {
            "symbol": snapshot.symbol,
            "as_of": snapshot.as_of,
            "close": snapshot.close,
            "ma20": snapshot.ma20,
            "atr14": snapshot.atr14,
            "entry_mode": entry_mode,
            "plan_version": config.research.plan_version if config.research else "research-plan.v1",
        }
    )
    plan = ReplayPlan(
        plan_key=plan_key,
        plan_id=plan_key,
        source_type="research_rule",
        source_pipeline=SourcePipeline.RESEARCH,
        source_revision=1,
        source_hash=source_hash,
        symbol=snapshot.symbol,
        scenario_id="research-1",
        scenario_role="primary",
        available_at=available_at,
        valid_from=valid_from,
        valid_until=valid_from,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_reference=entry_high,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        target_1_exit_ratio=Decimal("0.5"),
        order_valid_trading_days=order_valid_days,
        max_hold_trading_days=config.max_hold_trading_days,
        target_position_pct=target_pct,
        rank=rank,
        requires_confirmation=False,
        confirmation_valid_trading_days=1,
        confirmation_deadline=valid_from,
        scenario_json={},
        quality_flags=[
            "research_plan_v1",
            f"research_entry_{entry_mode}",
            "survivor_bias",
            "model_knowledge_leakage_risk",
        ],
    )
    return ResearchPlanDraft(plan=plan, snapshot=snapshot, reason=reason)


def _return(closes: list[Decimal], days: int) -> float:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return 0.0
    return float(closes[-1] / closes[-days - 1] - Decimal("1"))


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def _atr14(bars: list[DailyBar]) -> Decimal:
    window = bars[-15:]
    if len(window) < 2:
        return Decimal("0")
    ranges = []
    for previous, current in zip(window, window[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(ranges, Decimal("0")) / Decimal(len(ranges)) if ranges else Decimal("0")


def _volume_ratio(bars: list[DailyBar]) -> float:
    recent = [bar.volume for bar in bars[-5:] if bar.volume > 0]
    baseline = [bar.volume for bar in bars[-20:] if bar.volume > 0]
    if not recent or not baseline:
        return 1.0
    recent_mean = sum(recent, Decimal("0")) / Decimal(len(recent))
    baseline_mean = sum(baseline, Decimal("0")) / Decimal(len(baseline))
    if baseline_mean <= 0:
        return 1.0
    return float(recent_mean / baseline_mean)


def _floor_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _ceil_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


__all__ = [
    "AsOfMarketView",
    "ResearchPlanDraft",
    "TechnicalSnapshot",
    "build_research_plan",
]
