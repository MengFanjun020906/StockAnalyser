from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable, Sequence

from .confirmation import evaluate_confirmation_bar
from .params import PricePlanParams, get_price_plan_params
from .schemas import MarketRuleSnapshot, PricePlanArtifact, ScenarioRole, ZoneScenario


def evaluate_price_plan_backtest(
    artifact: PricePlanArtifact,
    bars: Sequence[Any],
    *,
    market_rules: MarketRuleSnapshot | None,
    params: PricePlanParams | None = None,
    max_hold_trading_days: int = 30,
    selected_scenario_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate every formal scenario without reading narrative report text."""
    params = params or get_price_plan_params(artifact.params_version)
    if params is None:
        return _strategy_result(
            status="unevaluable",
            outcome=None,
            reason="unknown_params_version",
            scenario_evaluations=[],
        )
    formal = [item for item in artifact.scenarios if item.role is not ScenarioRole.OBSERVATION]
    if artifact.generation_status.value == "no_plan" or not formal:
        return _strategy_result(
            status="skipped",
            outcome=None,
            reason="no_formal_scenario",
            scenario_evaluations=[],
        )
    if (
        market_rules is None
        or market_rules.board == "unknown"
        or not market_rules.instrument_type_verified
        or not market_rules.st_status_verified
        or not market_rules.listing_status_verified
    ):
        return _strategy_result(
            status="unevaluable",
            outcome=None,
            reason="missing_market_rule_snapshot",
            scenario_evaluations=[],
        )
    normalized = _normalize_bars(bars)
    if not normalized:
        return _strategy_result(
            status="unevaluable",
            outcome=None,
            reason="missing_forward_bars",
            scenario_evaluations=[],
        )
    evaluations = [
        _evaluate_scenario(
            scenario,
            normalized,
            market_rules=market_rules,
            params=params,
            max_hold_trading_days=max_hold_trading_days,
        )
        for scenario in sorted(formal, key=lambda item: (item.base_rank, item.scenario_id))
    ]
    selected = (
        next(
            (item for item in evaluations if item["scenario_id"] == selected_scenario_id),
            None,
        )
        if selected_scenario_id
        else evaluations[0]
    )
    first_touched = min(
        (item for item in evaluations if item.get("touched_at")),
        key=lambda item: (str(item["touched_at"]), int(item["base_rank"])),
        default=None,
    )
    policy = {
        "policy_version": "backtest-policy.v1",
        "selected_scenario_id": selected.get("scenario_id") if selected else None,
        "selection_reason": (
            "account_decision"
            if selected_scenario_id and selected
            else "selected_scenario_unavailable"
            if selected_scenario_id
            else "neutral_rank_order"
        ),
        "other_scenario_touched_first": bool(
            selected
            and first_touched
            and first_touched.get("scenario_id") != selected.get("scenario_id")
        ),
        "first_touched_scenario_id": first_touched.get("scenario_id") if first_touched else None,
    }
    if selected is None:
        return _strategy_result(
            status="unevaluable",
            outcome=None,
            reason="selected_scenario_unavailable",
            scenario_evaluations=evaluations,
            policy=policy,
        )
    return _strategy_result(
        status=selected["evaluation_status"],
        outcome=selected["outcome"],
        reason=selected.get("unevaluable_reason"),
        scenario_evaluations=evaluations,
        policy=policy,
    )


def _evaluate_scenario(
    scenario: ZoneScenario,
    bars: list[dict[str, Any]],
    *,
    market_rules: MarketRuleSnapshot,
    params: PricePlanParams,
    max_hold_trading_days: int,
) -> dict[str, Any]:
    result = {
        "scenario_id": scenario.scenario_id,
        "role": scenario.role.value,
        "base_rank": scenario.base_rank,
        "evaluation_status": "evaluated",
        "outcome": None,
        "touched_at": None,
        "confirmed_at": None,
        "filled_at": None,
        "entry_price": None,
        "exit_at": None,
        "exit_price": None,
        "exit_reason": None,
        "wait_trading_days": None,
        "holding_trading_days": None,
        "buy_fee": None,
        "sell_fee": None,
        "net_return_pct": None,
        "mae_pct": None,
        "mfe_pct": None,
        "ambiguous_bar": False,
        "unevaluable_reason": None,
    }
    eligible = [
        item
        for item in bars
        if item["date"] >= scenario.valid_from and not _is_suspended_bar(item)
    ]
    if not eligible:
        return _unevaluable(result, "missing_forward_bars")

    touch_index: int | None = None
    confirmation_index: int | None = None
    confirmation_bars = eligible[: scenario.confirmation_valid_trading_days]
    for index, bar in enumerate(confirmation_bars):
        confirmation = evaluate_confirmation_bar(scenario, eligible, index, params=params)
        if confirmation.touched and touch_index is None:
            touch_index = index
            result["touched_at"] = bar["date"].isoformat()
        confirmed = (
            touch_index is not None
            and index - touch_index < params.confirmation_after_touch_bars
            and confirmation.pattern_confirmed
        )
        if confirmation.invalidated and confirmed:
            result["outcome"] = "UNRESOLVED"
            result["ambiguous_bar"] = True
            return result
        if confirmation.invalidated:
            result["outcome"] = "INVALIDATED"
            result["exit_at"] = bar["date"].isoformat()
            result["exit_reason"] = "pre_entry_close_at_or_below_stop"
            return result
        if confirmed:
            confirmation_index = index
            result["confirmed_at"] = bar["date"].isoformat()
            break
    if confirmation_index is None:
        if len(confirmation_bars) < scenario.confirmation_valid_trading_days:
            return _unevaluable(result, "insufficient_confirmation_window_bars")
        expired_on = confirmation_bars[-1]["date"]
        result["outcome"] = "EXPIRED"
        result["exit_at"] = expired_on.isoformat()
        result["exit_reason"] = "confirmation_expired"
        return result

    order_bars = list(
        enumerate(eligible[confirmation_index + 1 :], start=confirmation_index + 1)
    )[: scenario.order_valid_trading_days]
    fill_index: int | None = None
    entry_price: Decimal | None = None
    entry_day_invalidation = False
    for wait_days, (offset, bar) in enumerate(order_bars, start=1):
        previous_close = eligible[offset - 1]["close"] if offset > 0 else market_rules.previous_close
        if _is_one_price_limit_up(bar, previous_close, market_rules):
            continue
        if scenario.stop_loss is not None and bar["open"] <= scenario.stop_loss:
            result["outcome"] = "INVALIDATED"
            result["exit_at"] = bar["date"].isoformat()
            result["exit_reason"] = "opened_at_or_below_stop"
            return result
        raw_fill = None
        if bar["open"] <= scenario.entry_high:
            raw_fill = bar["open"]
        elif bar["low"] <= scenario.entry_high <= bar["high"]:
            raw_fill = scenario.entry_high
        invalidated = evaluate_confirmation_bar(
            scenario,
            eligible,
            offset,
            params=params,
        ).invalidated
        if invalidated and raw_fill is None:
            result["outcome"] = "INVALIDATED"
            result["exit_at"] = bar["date"].isoformat()
            result["exit_reason"] = "invalidated_before_fill"
            return result
        if invalidated and raw_fill is not None and bar["open"] > scenario.entry_high:
            result["outcome"] = "UNRESOLVED"
            result["ambiguous_bar"] = True
            result["exit_at"] = bar["date"].isoformat()
            result["exit_reason"] = "intraday_fill_and_invalidation_order_unknown"
            return result
        if raw_fill is None:
            continue
        entry_price = _ceil_tick(
            raw_fill * (Decimal("1") + params.reference_buy_slippage_bps / Decimal("10000")),
            market_rules.tick_size,
        )
        fill_index = offset
        result["filled_at"] = bar["date"].isoformat()
        result["entry_price"] = float(entry_price)
        result["wait_trading_days"] = wait_days
        entry_day_invalidation = invalidated
        break
    if fill_index is None or entry_price is None:
        if len(order_bars) < scenario.order_valid_trading_days:
            return _unevaluable(result, "insufficient_order_window_bars")
        result["outcome"] = "NOT_FILLED"
        result["exit_reason"] = "order_window_expired"
        return result

    exit_bars = eligible[fill_index + 1 :][:max_hold_trading_days]
    if not exit_bars:
        return _unevaluable(result, "missing_t_plus_1_exit_bars")
    return _evaluate_exit(
        result,
        scenario=scenario,
        entry_price=entry_price,
        exit_bars=exit_bars,
        previous_close=eligible[fill_index]["close"],
        market_rules=market_rules,
        params=params,
        max_hold_trading_days=max_hold_trading_days,
        pending_t_plus_one_stop=entry_day_invalidation,
    )


def _evaluate_exit(
    result: dict[str, Any],
    *,
    scenario: ZoneScenario,
    entry_price: Decimal,
    exit_bars: list[dict[str, Any]],
    previous_close: Decimal,
    market_rules: MarketRuleSnapshot,
    params: PricePlanParams,
    max_hold_trading_days: int,
    pending_t_plus_one_stop: bool = False,
) -> dict[str, Any]:
    entry_day_close = previous_close
    remaining = Decimal("1")
    gross_proceeds = Decimal("0")
    sell_fees = Decimal("0")
    target_1_done = False
    min_low = entry_price
    max_high = entry_price
    final_price: Decimal | None = None
    final_reason: str | None = None
    final_index = 0
    for index, bar in enumerate(exit_bars[:max_hold_trading_days], start=1):
        min_low = min(min_low, bar["low"])
        max_high = max(max_high, bar["high"])
        if pending_t_plus_one_stop:
            if _is_one_price_limit_down(bar, previous_close, market_rules):
                previous_close = bar["close"]
                continue
            final_price = _floor_tick(
                bar["open"]
                * (Decimal("1") - params.reference_sell_slippage_bps / Decimal("10000")),
                market_rules.tick_size,
            )
            final_reason = "t_plus_one_stop_after_entry_day_invalidation"
        else:
            stop_hit = scenario.stop_loss is not None and bar["low"] <= scenario.stop_loss
            target_1_hit = not target_1_done and scenario.target_1 is not None and bar["high"] >= scenario.target_1
            target_2_hit = target_1_done and scenario.target_2 is not None and bar["high"] >= scenario.target_2
        if not pending_t_plus_one_stop and stop_hit:
            if _is_one_price_limit_down(bar, previous_close, market_rules):
                previous_close = bar["close"]
                continue
            result["ambiguous_bar"] = bool(target_1_hit or target_2_hit)
            final_price = _floor_tick(
                scenario.stop_loss
                * (Decimal("1") - params.reference_sell_slippage_bps / Decimal("10000")),
                market_rules.tick_size,
            )
            final_reason = "stop_loss"
        elif not pending_t_plus_one_stop and target_1_hit:
            ratio = min(remaining, scenario.target_1_exit_ratio)
            price = _floor_tick(
                scenario.target_1
                * (Decimal("1") - params.reference_sell_slippage_bps / Decimal("10000")),
                market_rules.tick_size,
            )
            proceeds = price * ratio
            gross_proceeds += proceeds
            sell_fees += proceeds * params.reference_sell_cost_bps / Decimal("10000")
            remaining -= ratio
            target_1_done = True
            if remaining <= 0:
                final_price = price
                final_reason = "target_1"
        elif not pending_t_plus_one_stop and target_2_hit:
            final_price = _floor_tick(
                scenario.target_2
                * (Decimal("1") - params.reference_sell_slippage_bps / Decimal("10000")),
                market_rules.tick_size,
            )
            final_reason = "target_2"
        if final_price is not None:
            proceeds = final_price * remaining
            gross_proceeds += proceeds
            sell_fees += proceeds * params.reference_sell_cost_bps / Decimal("10000")
            remaining = Decimal("0")
            result["exit_at"] = bar["date"].isoformat()
            final_index = index
            break
        previous_close = bar["close"]
    if remaining > 0:
        if len(exit_bars) < max_hold_trading_days:
            return _unevaluable(result, "insufficient_holding_window_bars")
        bar = exit_bars[max_hold_trading_days - 1]
        previous_for_timeout = (
            exit_bars[max_hold_trading_days - 2]["close"]
            if max_hold_trading_days >= 2
            else entry_day_close
        )
        if _is_one_price_limit_down(bar, previous_for_timeout, market_rules):
            return _unevaluable(result, "timeout_exit_blocked_by_limit_down")
        final_price = _floor_tick(
            bar["close"] * (Decimal("1") - params.reference_sell_slippage_bps / Decimal("10000")),
            market_rules.tick_size,
        )
        proceeds = final_price * remaining
        gross_proceeds += proceeds
        sell_fees += proceeds * params.reference_sell_cost_bps / Decimal("10000")
        result["exit_at"] = bar["date"].isoformat()
        final_reason = "timeout_exit"
        final_index = max_hold_trading_days

    buy_fee = entry_price * params.reference_buy_cost_bps / Decimal("10000")
    net_cost = entry_price + buy_fee
    net_pnl = gross_proceeds - sell_fees - net_cost
    net_return = net_pnl / net_cost * Decimal("100")
    result.update(
        {
            "outcome": "WIN" if net_return > 0 else "LOSS",
            "exit_price": float(final_price) if final_price is not None else None,
            "exit_reason": final_reason,
            "holding_trading_days": final_index,
            "buy_fee": float(buy_fee),
            "sell_fee": float(sell_fees),
            "net_return_pct": float(net_return.quantize(Decimal("0.0001"))),
            "mae_pct": float(((min_low / entry_price - 1) * 100).quantize(Decimal("0.0001"))),
            "mfe_pct": float(((max_high / entry_price - 1) * 100).quantize(Decimal("0.0001"))),
        }
    )
    return result


def _normalize_bars(bars: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: dict[date, dict[str, Any]] = {}
    for raw in bars:
        try:
            bar_date = _date_value(_value(raw, "date") or _value(raw, "bar_date"))
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
        if min(item["open"], item["high"], item["low"], item["close"]) <= 0:
            continue
        if item["high"] < max(item["open"], item["close"], item["low"]):
            continue
        if item["low"] > min(item["open"], item["close"], item["high"]):
            continue
        normalized[bar_date] = item
    return [normalized[key] for key in sorted(normalized)]


def _strategy_result(
    *,
    status: str,
    outcome: str | None,
    reason: str | None,
    scenario_evaluations: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "strategy": "deterministic_price_plan",
        "schema_version": "price-plan-backtest.v1",
        "evaluation_status": status,
        "outcome": outcome,
        "scenario_evaluations": scenario_evaluations,
        "policy_evaluation": policy or {},
        "unevaluable_reason": reason if status == "unevaluable" else None,
    }


def _unevaluable(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["evaluation_status"] = "unevaluable"
    result["outcome"] = None
    result["unevaluable_reason"] = reason
    return result


def _ceil_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _floor_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _is_suspended_bar(bar: dict[str, Any]) -> bool:
    return bar["volume"] <= 0


def _is_one_price_limit_up(
    bar: dict[str, Any],
    previous_close: Decimal | None,
    rules: MarketRuleSnapshot,
) -> bool:
    limit = _daily_limit(previous_close, rules.price_limit_ratio, rules.tick_size, up=True)
    return limit is not None and _one_price_bar(bar) and bar["close"] >= limit


def _is_one_price_limit_down(
    bar: dict[str, Any],
    previous_close: Decimal | None,
    rules: MarketRuleSnapshot,
) -> bool:
    limit = _daily_limit(previous_close, rules.price_limit_ratio, rules.tick_size, up=False)
    return limit is not None and _one_price_bar(bar) and bar["close"] <= limit


def _daily_limit(
    previous_close: Decimal | None,
    ratio: Decimal | None,
    tick: Decimal,
    *,
    up: bool,
) -> Decimal | None:
    if previous_close is None or ratio is None:
        return None
    multiplier = Decimal("1") + ratio if up else Decimal("1") - ratio
    return (
        previous_close * multiplier / tick
    ).to_integral_value(rounding=ROUND_HALF_UP) * tick


def _one_price_bar(bar: dict[str, Any]) -> bool:
    return bar["open"] == bar["high"] == bar["low"] == bar["close"]


def _value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


__all__ = ["evaluate_price_plan_backtest"]
