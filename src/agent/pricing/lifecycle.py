from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .confirmation import evaluate_confirmation_bar
from .params import PRICE_PLAN_PARAMS_V1, PricePlanParams
from .schemas import ZoneScenario


@dataclass(frozen=True)
class ScenarioLifecycleEvent:
    event_type: str
    state: str
    occurred_on: date
    payload: dict[str, Any]


def evaluate_scenario_lifecycle(
    scenario: ZoneScenario,
    bars: Iterable[Any],
    *,
    params: PricePlanParams = PRICE_PLAN_PARAMS_V1,
) -> list[ScenarioLifecycleEvent]:
    """Return the deterministic event sequence for one scenario."""
    normalized = _normalize_bars(bars)
    eligible = [
        item
        for item in normalized
        if item["date"] >= scenario.valid_from and item["volume"] > 0
    ]
    events: list[ScenarioLifecycleEvent] = []
    touched_index: int | None = None
    confirmed = False
    confirmation_bars = eligible[: scenario.confirmation_valid_trading_days]
    confirmed_index: int | None = None
    for index, bar in enumerate(confirmation_bars):
        evaluation = evaluate_confirmation_bar(scenario, eligible, index, params=params)
        if evaluation.touched and evaluation.invalidated and not confirmed:
            events.append(
                ScenarioLifecycleEvent(
                    event_type="ambiguous",
                    state="ambiguous",
                    occurred_on=bar["date"],
                    payload={"reason": "zone_touch_and_close_stop_same_daily_bar"},
                )
            )
            return events
        if evaluation.invalidated:
            events.append(
                ScenarioLifecycleEvent(
                    event_type="invalidated",
                    state="invalidated",
                    occurred_on=bar["date"],
                    payload={"reason": "close_at_or_below_stop"},
                )
            )
            return events
        if confirmed:
            continue
        if touched_index is None and evaluation.touched:
            touched_index = index
            events.append(
                ScenarioLifecycleEvent(
                    event_type="touched",
                    state="touched",
                    occurred_on=bar["date"],
                    payload={
                        "entry_low": str(scenario.entry_low),
                        "entry_high": str(scenario.entry_high),
                    },
                )
            )
        if (
            touched_index is not None
            and index - touched_index < params.confirmation_after_touch_bars
            and evaluation.pattern_confirmed
        ):
            events.append(
                ScenarioLifecycleEvent(
                    event_type="technical_confirmed",
                    state="confirmed",
                    occurred_on=bar["date"],
                    payload={"policy_version": "price-confirmation.v1"},
                )
            )
            confirmed = True
            confirmed_index = index
            break
    if not confirmed and len(confirmation_bars) >= scenario.confirmation_valid_trading_days:
        expired_on = confirmation_bars[-1]["date"]
        events.append(
            ScenarioLifecycleEvent(
                event_type="expired",
                state="expired",
                occurred_on=expired_on,
                payload={
                    "planned_confirmation_deadline": scenario.confirmation_deadline.isoformat(),
                    "effective_confirmation_deadline": expired_on.isoformat(),
                    "valid_trading_days": scenario.confirmation_valid_trading_days,
                },
            )
        )
        return events
    if confirmed_index is not None:
        for index in range(confirmed_index + 1, len(eligible)):
            bar = eligible[index]
            evaluation = evaluate_confirmation_bar(scenario, eligible, index, params=params)
            if evaluation.invalidated:
                events.append(
                    ScenarioLifecycleEvent(
                        event_type="invalidated",
                        state="invalidated",
                        occurred_on=bar["date"],
                        payload={"reason": "close_at_or_below_stop"},
                    )
                )
                break
    return events


def _normalize_bars(bars: Iterable[Any]) -> list[dict[str, Any]]:
    output: dict[date, dict[str, Any]] = {}
    for raw in bars or []:
        try:
            raw_date = _value(raw, "date") or _value(raw, "bar_date")
            bar_date = (
                raw_date.date() if isinstance(raw_date, datetime)
                else raw_date if isinstance(raw_date, date)
                else date.fromisoformat(str(raw_date)[:10])
            )
            item = {
                "date": bar_date,
                "open": Decimal(str(_value(raw, "open"))),
                "high": Decimal(str(_value(raw, "high"))),
                "low": Decimal(str(_value(raw, "low"))),
                "close": Decimal(str(_value(raw, "close"))),
                "volume": Decimal(str(_value(raw, "volume") or 0)),
            }
        except (TypeError, ValueError, ArithmeticError):
            continue
        if item["high"] < max(item["open"], item["close"], item["low"]):
            continue
        if item["low"] > min(item["open"], item["close"], item["high"]):
            continue
        output[bar_date] = item
    return [output[key] for key in sorted(output)]


def _value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


__all__ = ["ScenarioLifecycleEvent", "evaluate_scenario_lifecycle"]
