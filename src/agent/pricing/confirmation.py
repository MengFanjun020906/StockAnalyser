from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from .params import PRICE_PLAN_PARAMS_V1, PricePlanParams
from .schemas import ZoneScenario


@dataclass(frozen=True)
class ConfirmationEvaluation:
    touched: bool
    hard_break: bool
    volume_break: bool
    hammer: bool
    bullish_engulfing: bool
    confirmed_bottom_fractal: bool
    contraction_confirmed: bool
    expansion_confirmed: bool

    @property
    def invalidated(self) -> bool:
        return self.hard_break or self.volume_break

    @property
    def pattern_confirmed(self) -> bool:
        return (
            (self.hammer or self.confirmed_bottom_fractal)
            and self.contraction_confirmed
        ) or (self.bullish_engulfing and self.expansion_confirmed)


def evaluate_confirmation_bar(
    scenario: ZoneScenario,
    bars: Sequence[dict[str, Any]],
    index: int,
    *,
    params: PricePlanParams = PRICE_PLAN_PARAMS_V1,
) -> ConfirmationEvaluation:
    bar = bars[index]
    history = [
        {
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
        }
        for item in scenario.confirmation_seed_bars
        if item.volume > 0
    ]
    combined = [*history, *bars[: index + 1]]
    current_index = len(combined) - 1
    current = combined[current_index]
    tick_size = _rule_decimal(scenario, "closed_bar_reversal_pattern", "tick_size") or Decimal("0.01")
    body = abs(current["close"] - current["open"])
    bar_range = max(current["high"] - current["low"], tick_size)
    lower_shadow = min(current["open"], current["close"]) - current["low"]
    upper_shadow = current["high"] - max(current["open"], current["close"])
    body_floor = max(body, tick_size)
    hammer = (
        current["close"] > current["open"]
        and lower_shadow >= params.hammer_lower_body_ratio * body_floor
        and upper_shadow <= params.hammer_upper_body_ratio * body_floor
        and (current["close"] - current["low"]) / bar_range
        >= params.hammer_close_location_min
    )
    bullish_engulfing = False
    confirmed_bottom_fractal = False
    if current_index >= 1:
        previous = combined[current_index - 1]
        bullish_engulfing = (
            previous["close"] < previous["open"]
            and current["close"] > current["open"]
            and current["open"] <= previous["close"]
            and current["close"] >= previous["open"]
        )
    if current_index >= 2:
        middle = combined[current_index - 1]
        confirmed_bottom_fractal = (
            middle["low"] < combined[current_index - 2]["low"]
            and middle["low"] < current["low"]
        )
    prior = [
        item["volume"]
        for item in combined[max(0, current_index - params.confirmation_volume_ma_bars) : current_index]
        if item["volume"] > 0
    ]
    average = (
        sum(prior, Decimal("0")) / Decimal(len(prior))
        if len(prior) == params.confirmation_volume_ma_bars
        else None
    )
    contraction_confirmed = bool(
        average is not None
        and current["volume"] <= average * params.contraction_volume_ratio
    )
    expansion_confirmed = bool(
        average is not None
        and current["volume"] >= average * params.expansion_volume_ratio
    )
    breakdown_price = _rule_decimal(scenario, "volume_breakdown", "breakdown_price")
    volume_break = bool(
        breakdown_price is not None
        and average is not None
        and current["close"] < breakdown_price
        and current["volume"] >= average * params.breakdown_volume_ratio
    )
    return ConfirmationEvaluation(
        touched=current["low"] <= scenario.entry_high and current["high"] >= scenario.entry_low,
        hard_break=scenario.stop_loss is not None and current["close"] <= scenario.stop_loss,
        volume_break=volume_break,
        hammer=hammer,
        bullish_engulfing=bullish_engulfing,
        confirmed_bottom_fractal=confirmed_bottom_fractal,
        contraction_confirmed=contraction_confirmed,
        expansion_confirmed=expansion_confirmed,
    )


def _rule_decimal(
    scenario: ZoneScenario,
    rule_type: str,
    key: str,
) -> Decimal | None:
    rule = next((item for item in scenario.confirmation_rules + scenario.invalidation_rules if item.rule_type == rule_type), None)
    if rule is None or key not in rule.params:
        return None
    try:
        return Decimal(str(rule.params[key]))
    except (TypeError, ValueError, ArithmeticError):
        return None


__all__ = ["ConfirmationEvaluation", "evaluate_confirmation_bar"]
