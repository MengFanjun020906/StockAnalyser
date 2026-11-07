"""Pure composite-verdict evaluation over a pre-built feature snapshot."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.agent.scoring.position_sizing import calculate_position_sizing
from src.schemas.composite_verdict import (
    CompositeScoreComponent,
    CompositeScoreComponents,
    CompositeScoreSnapshot,
    CompositeVerdict,
    ExitWarning,
    S4FeatureSnapshot,
)


CHAN_SIGNAL_POINTS: Dict[str, int] = {
    "first_buy": 5,
    "second_buy": 4,
    "third_buy": 2,
    "bottom_exhaustion": 2,
    "weekly_confirmation": 1,
    "top_divergence": -3,
    "top_exhaustion": -2,
}
EXIT_WARNING_SIGNALS = {
    "top_fractal",
    "top_divergence",
    "top_exhaustion",
    "volume_stall",
    "line_convergence",
    "overbought",
    "chan_top_divergence_confirmed",
    "first_sell",
    "second_sell",
    "third_sell",
}


def evaluate_composite_verdict(
    snapshot: CompositeScoreSnapshot,
    *,
    market_regime: Optional[object] = None,
) -> CompositeVerdict:
    """Return a deterministic, side-effect-free verdict for one snapshot."""
    chan_score, chan_breakdown, chan_missing = _score_chan(snapshot)
    s4_score, s4_breakdown, s4_missing = _score_s4(snapshot)
    provisional_s4_score, provisional_s4_breakdown = _score_s4_provisional(snapshot)
    complete = s4_score is not None and chan_score is not None
    score = s4_score + chan_score if complete and s4_score is not None and chan_score is not None else None
    provisional_score = (
        provisional_s4_score + chan_score
        if score is None and provisional_s4_score is not None and chan_score is not None
        else None
    )
    signal_level = _signal_level(score, s4_score, snapshot)
    exit_warning = _exit_warning(snapshot)
    missing_features = list(dict.fromkeys([*s4_missing, *chan_missing]))
    required_holding_action = "reduce" if snapshot.holding and exit_warning.level == 3 else "none"
    reduce_to_ratio = 0.50 if required_holding_action == "reduce" else None
    position = calculate_position_sizing(snapshot.s4, market_regime)
    adjustments = _backtest_adjustments(snapshot)
    formalization_reason_codes = _formalization_reason_codes(
        snapshot,
        s4_score=s4_score,
        chan_score=chan_score,
    )
    quality_warning_codes = _quality_warning_codes(snapshot)

    s4_status = (
        "ok"
        if s4_score is not None
        else "not_implemented"
        if snapshot.s4.status == "not_implemented"
        else "partial"
        if snapshot.s4.status == "partial"
        else "not_evaluable"
    )

    return CompositeVerdict(
        symbol=snapshot.symbol,
        as_of=snapshot.as_of,
        verdict_status="evaluable" if complete else "not_evaluable",
        score=score,
        provisional_score=provisional_score,
        components=CompositeScoreComponents(
            s4=CompositeScoreComponent(
                status=s4_status,
                score=s4_score,
                provisional_score=provisional_s4_score if s4_score is None else None,
                scale_max=10,
                breakdown=s4_breakdown,
                provisional_breakdown=provisional_s4_breakdown if s4_score is None else {},
                missing_features=s4_missing,
            ),
            chan=CompositeScoreComponent(
                status=(
                    "not_evaluable"
                    if chan_score is None
                    else "partial"
                    if snapshot.chan.status == "partial"
                    else "ok"
                ),
                score=chan_score,
                scale_max=10,
                breakdown=chan_breakdown,
                missing_features=chan_missing,
            ),
        ),
        signal_level=signal_level,
        entry_permission=_entry_permission(signal_level),
        required_holding_action=required_holding_action,
        reduce_to_ratio=reduce_to_ratio,
        target_position_cap_pct=position.get("target_position_cap_pct"),
        target_position_cap_ratio=position.get("target_position_cap_ratio"),
        position_tier=position.get("position_tier"),
        market_coefficient=position.get("market_coefficient"),
        backtest=snapshot.backtest.model_dump(mode="json"),
        adjustments=adjustments,
        exit_warning=exit_warning,
        missing_features=missing_features,
        formalization_reason_codes=formalization_reason_codes,
        quality_warning_codes=quality_warning_codes,
        strategy4_rule_version="strategy4.v1",
    )


def _formalization_reason_codes(
    snapshot: CompositeScoreSnapshot,
    *,
    s4_score: Optional[int],
    chan_score: Optional[int],
) -> List[str]:
    """Explain only conditions that actually block a formal composite score."""
    reasons: List[str] = []
    if s4_score is None:
        if snapshot.s4.weekly_provisional:
            reasons.append("S4_WEEKLY_BAR_UNCONFIRMED")
        elif snapshot.s4.status == "not_implemented":
            reasons.append("S4_NOT_IMPLEMENTED")
        else:
            reasons.append("S4_FEATURES_INCOMPLETE")
    if chan_score is None:
        reasons.append("CHAN_EVIDENCE_INSUFFICIENT")
    return reasons


def _quality_warning_codes(snapshot: CompositeScoreSnapshot) -> List[str]:
    """Expose non-blocking caveats without confusing them with score blockers."""
    warnings: List[str] = []
    if snapshot.data_quality in {"limited", "insufficient", "unknown"}:
        warnings.append(f"DATA_QUALITY_{snapshot.data_quality.upper()}")
    if snapshot.backtest.status != "ok":
        warnings.append(f"BACKTEST_{snapshot.backtest.status.upper()}")
    return warnings


def _score_chan(snapshot: CompositeScoreSnapshot) -> Tuple[Optional[int], Dict[str, int], List[str]]:
    if snapshot.chan.status == "insufficient":
        return None, {}, ["chan_signals"]
    breakdown: Dict[str, int] = {}
    seen = set()
    for signal in snapshot.chan.signals:
        key = signal.name
        if not signal.triggered or signal.metrics.get("provisional") or key in seen:
            continue
        seen.add(key)
        points = CHAN_SIGNAL_POINTS.get(signal.name)
        if points is not None:
            breakdown[signal.name] = points
    return max(0, min(10, sum(breakdown.values()))), breakdown, []


def _exit_warning(snapshot: CompositeScoreSnapshot) -> ExitWarning:
    items = []
    for signal in snapshot.chan.signals:
        if signal.triggered and signal.name in EXIT_WARNING_SIGNALS and signal.name not in items:
            items.append(signal.name)
    sell_levels = {
        "first_sell": 2,
        "second_sell": 1,
        "third_sell": 2,
        "top_divergence": 2,
        "top_exhaustion": 1,
    }
    level = max((sell_levels.get(item, 0) for item in items), default=0)
    if "top_divergence" in items and len(items) >= 2:
        level = 3
    elif "first_sell" in items and any(item in {"top_divergence", "top_exhaustion"} for item in items):
        level = 3
    elif not level:
        level = 1 if items else 0
    return ExitWarning(level=level, items=items)


def _score_s4(snapshot: CompositeScoreSnapshot) -> Tuple[Optional[int], Dict[str, int], List[str]]:
    features = snapshot.s4
    required = {
        "region_pass": features.region_pass,
        "resonance_count": features.resonance_count,
        "volume_ratio_5d": features.volume_ratio_5d,
        "volatility_pct": features.volatility_pct,
        "gain_10d_pct": features.gain_10d_pct,
        "golden_cross_today": features.golden_cross_today,
        "all_ok": features.all_ok,
    }
    missing = list(dict.fromkeys([
        *features.missing_features,
        *(name for name, value in required.items() if value is None),
    ]))
    if features.status != "ok" or missing:
        return None, {}, missing

    breakdown = _strategy4_breakdown(features)
    return sum(breakdown.values()), breakdown, []


def _score_s4_provisional(snapshot: CompositeScoreSnapshot) -> Tuple[Optional[int], Dict[str, int]]:
    """Score complete feature values without promoting an unfinished week."""
    features = snapshot.s4
    required = (
        features.region_pass,
        features.resonance_count,
        features.volume_ratio_5d,
        features.volatility_pct,
        features.gain_10d_pct,
        features.golden_cross_today,
        features.all_ok,
    )
    if features.status != "partial" or not features.weekly_provisional or any(value is None for value in required):
        return None, {}
    breakdown = _strategy4_breakdown(features)
    return sum(breakdown.values()), breakdown


def _strategy4_breakdown(features: S4FeatureSnapshot) -> Dict[str, int]:
    return {
        "region": 2 if features.region_pass else 0,
        "resonance": 2 if int(features.resonance_count or 0) >= 3 else 0,
        "volume_ratio": 2 if float(features.volume_ratio_5d or 0.0) >= 1.2 else 0,
        "volatility": 2 if float(features.volatility_pct or 0.0) <= 6.0 else 0,
        "gain_10d": 1 if float(features.gain_10d_pct or 0.0) <= 9.0 else 0,
        "golden_cross": 1 if features.golden_cross_today and features.all_ok else 0,
    }


def _backtest_adjustments(snapshot: CompositeScoreSnapshot) -> List[Dict[str, object]]:
    backtest = snapshot.backtest
    if backtest.status == "ok" and backtest.win_rate_pct is not None and backtest.win_rate_pct < 40.0:
        return [
            {
                "type": "backtest_win_rate",
                "reason": "win_rate_below_40_pct",
                "win_rate_pct": backtest.win_rate_pct,
                "sample_count": backtest.sample_count,
                "applied": False,
                "shadow_mode": True,
            }
        ]
    if backtest.status == "insufficient_sample":
        return [
            {
                "type": "backtest_win_rate",
                "reason": "backtest_sample_insufficient",
                "sample_count": backtest.sample_count,
                "applied": False,
                "shadow_mode": True,
            }
        ]
    return []


def _signal_level(
    score: Optional[int],
    s4_score: Optional[int],
    snapshot: CompositeScoreSnapshot,
) -> str:
    if score is None or s4_score is None:
        return "not_evaluable"
    first_buy = any(
        signal.triggered and signal.name == "first_buy"
        for signal in snapshot.chan.signals
    )
    if score >= 18 and first_buy and s4_score >= 8:
        return "strong_confirmed"
    if score >= 15:
        return "buy_valid"
    if score >= 10:
        return "buy_watch"
    if score >= 6:
        return "buy_transition"
    return "observe_or_exit"


def _entry_permission(signal_level: str) -> str:
    if signal_level in {"strong_confirmed", "buy_valid"}:
        return "allowed"
    if signal_level == "buy_watch":
        return "conditional"
    if signal_level == "not_evaluable":
        return "not_evaluable"
    return "blocked"


__all__ = ["evaluate_composite_verdict"]
