"""Deterministic Strategy-4 position sizing policy."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.schemas.composite_verdict import S4FeatureSnapshot


SINGLE_NAME_CAP_RATIO = 0.20
REGIME_COEFFICIENTS = {
    "range_bound": 0.60,
    "event_driven": 0.50,
    "high_volatility": 0.40,
    "trending_down": 0.35,
    "risk_off": 0.30,
    "panic": 0.25,
    "unknown": 0.25,
}
LOW_VOLATILITY_BUCKETS = {"very_low", "low", "normal"}


def calculate_position_sizing(
    features: S4FeatureSnapshot,
    market_regime: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return ratio-based sizing plus a percent boundary for Risk Gate."""
    result: Dict[str, Any] = {
        "position_tier": None,
        "base_position_ratio": None,
        "market_coefficient": None,
        "target_position_cap_ratio": None,
        "target_position_cap_pct": None,
        "status": "not_evaluable",
    }
    if features.all_ok is not True or features.resonance_count is None or features.volume_ratio_5d is None:
        result["reason"] = "s4_position_inputs_missing_or_not_passed"
        return result

    if features.resonance_count == 4 and features.volume_ratio_5d >= 1.5:
        tier, base_ratio = "S", 0.20
    elif features.resonance_count == 4 and features.volume_ratio_5d >= 1.2:
        tier, base_ratio = "A", 0.15
    else:
        tier, base_ratio = "B", 0.10
    coefficient = market_coefficient(market_regime)
    result.update({"position_tier": tier, "base_position_ratio": base_ratio})
    if coefficient is None:
        result["reason"] = "market_coefficient_missing"
        return result

    cap_ratio = min(base_ratio * coefficient, SINGLE_NAME_CAP_RATIO)
    result.update(
        {
            "market_coefficient": coefficient,
            "target_position_cap_ratio": round(cap_ratio, 6),
            "target_position_cap_pct": round(cap_ratio * 100.0, 6),
            "status": "ok",
        }
    )
    return result


def market_coefficient(market_regime: Optional[Any]) -> Optional[float]:
    """Map a RegimeState-like object or dict to the frozen coefficient."""
    if market_regime is None:
        return None
    regime = _value(market_regime, "regime")
    bucket = _value(market_regime, "volatility_bucket")
    if regime == "trending_up":
        return 1.0 if bucket in LOW_VOLATILITY_BUCKETS else 0.8
    return REGIME_COEFFICIENTS.get(regime or "unknown")


def _value(payload: Any, key: str) -> Optional[str]:
    value = payload.get(key) if isinstance(payload, dict) else getattr(payload, key, None)
    if hasattr(value, "value"):
        value = value.value
    return str(value) if value is not None else None


__all__ = ["REGIME_COEFFICIENTS", "SINGLE_NAME_CAP_RATIO", "calculate_position_sizing", "market_coefficient"]
