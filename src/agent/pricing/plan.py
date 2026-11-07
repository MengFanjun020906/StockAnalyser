from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from src.core.trading_calendar import is_market_open

from .anchors import AnchorBuildResult, build_anchors
from .clustering import (
    AnchorCluster,
    build_support_clusters,
    cluster_sort_key,
    floor_to_tick,
)
from .fingerprint import fingerprint, stable_id
from .market_rules import is_supported_cn_common_stock
from .params import PricePlanParams
from .schemas import (
    AnchorEvidence,
    AnchorRole,
    AnchorStatus,
    GenerationStatus,
    PricePlanArtifact,
    PricePlanSnapshot,
    PriceRelation,
    ReachabilityBand,
    RuleSpec,
    ScenarioRole,
    ZoneScenario,
)


def evaluate_price_plan(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    *,
    disabled_families: frozenset[str] = frozenset(),
    trace_ref: str | None = None,
) -> PricePlanArtifact:
    """Pure price-plan evaluation over an immutable, as-of-truncated snapshot."""
    data_fingerprint = fingerprint(_fingerprint_snapshot(snapshot))
    calculation_fingerprint = fingerprint(
        {
            "data_fingerprint": data_fingerprint,
            "algorithm_version": params.algorithm_version,
            "params": params,
            "disabled_families": sorted(disabled_families),
            "price_basis": snapshot.price_basis,
            "daily_count": len(snapshot.daily_bars),
            "weekly_count": len(snapshot.weekly_bars),
        }
    )
    contextual_reasons = (
        ["AS_OF_ROLLED_BACK"]
        if snapshot.actual_as_of_session < snapshot.requested_as_of
        else []
    )
    critical_reasons = _critical_reasons(snapshot, params)
    if critical_reasons:
        return _no_plan(
            snapshot,
            params,
            data_fingerprint=data_fingerprint,
            calculation_fingerprint=calculation_fingerprint,
            reasons=critical_reasons + contextual_reasons,
            trace_ref=trace_ref,
        )

    anchor_result = build_anchors(
        snapshot,
        params,
        disabled_families=disabled_families,
    )
    clusters = build_support_clusters(
        anchor_result.anchors,
        atr=anchor_result.atr,
        tick_size=snapshot.market_rules.tick_size,
        params=params,
    )
    clusters.sort(
        key=lambda item: cluster_sort_key(
            item,
            last_cash_price=snapshot.last_cash_price,
            atr=anchor_result.atr,
        )
    )
    if not clusters:
        reasons = ["NO_ELIGIBLE_ANCHORS", *contextual_reasons]
        if disabled_families:
            reasons.append("ANCHOR_FAMILY_DISABLED")
        if snapshot.chip is None:
            reasons.append("MISSING_HISTORICAL_CHIP_SNAPSHOT")
        return _no_plan(
            snapshot,
            params,
            data_fingerprint=data_fingerprint,
            calculation_fingerprint=calculation_fingerprint,
            reasons=reasons,
            trace_ref=trace_ref,
            anchors=anchor_result.anchors,
        )

    weekly_filter = _weekly_filter(snapshot.weekly_bars)
    scenarios = _build_scenarios(
        snapshot,
        params,
        anchor_result,
        clusters[:3],
        weekly_filter=weekly_filter,
    )
    scenarios = _assign_roles(scenarios)
    formal = [item for item in scenarios if item.role is not ScenarioRole.OBSERVATION]
    if formal:
        generation_status = GenerationStatus.GENERATED
    elif scenarios:
        generation_status = GenerationStatus.OBSERVATION
    else:
        return _no_plan(
            snapshot,
            params,
            data_fingerprint=data_fingerprint,
            calculation_fingerprint=calculation_fingerprint,
            reasons=["NO_ELIGIBLE_ANCHORS"],
            trace_ref=trace_ref,
            anchors=anchor_result.anchors,
        )

    reason_codes = sorted(
        {
            reason
            for scenario in scenarios
            for reason in scenario.scenario_reason_codes
        }.union(contextual_reasons)
    )
    if snapshot.chip is None:
        reason_codes.append("MISSING_HISTORICAL_CHIP_SNAPSHOT")
    if disabled_families:
        reason_codes.append("ANCHOR_FAMILY_DISABLED")
    payload = {
        "calculation_fingerprint": calculation_fingerprint,
        "generation_status": generation_status,
        "scenarios": [item.model_dump(mode="python") for item in scenarios],
        "reason_codes": sorted(set(reason_codes)),
    }
    plan_fingerprint = fingerprint(payload)
    plan_id = stable_id("plan", payload)
    return PricePlanArtifact(
        plan_id=plan_id,
        revision=1,
        symbol=snapshot.symbol,
        requested_as_of=snapshot.requested_as_of,
        actual_as_of_session=snapshot.actual_as_of_session,
        generation_status=generation_status,
        scenarios=scenarios,
        observation_anchors=anchor_result.anchors,
        market_rules_snapshot_ref=snapshot.market_rules.snapshot_id,
        data_quality="sufficient" if formal else "limited",
        reason_codes=sorted(set(reason_codes)),
        diagnostics={
            "atr14": str(anchor_result.atr),
            "weekly_filter": weekly_filter,
            "daily_bar_count": len(snapshot.daily_bars),
            "weekly_bar_count": len(snapshot.weekly_bars),
            "anchor_counts": _anchor_counts(anchor_result.anchors),
            "disabled_anchor_families": sorted(disabled_families),
        },
        algorithm_version=params.algorithm_version,
        params_version=params.version,
        data_fingerprint=data_fingerprint,
        calculation_fingerprint=calculation_fingerprint,
        plan_fingerprint=plan_fingerprint,
        created_at=_deterministic_created_at(snapshot),
        trace_ref=trace_ref,
    )


def _build_scenarios(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    anchor_result: AnchorBuildResult,
    clusters: Sequence[AnchorCluster],
    *,
    weekly_filter: str,
) -> list[ZoneScenario]:
    scenarios: list[ZoneScenario] = []
    supports = _structure_supports(anchor_result.anchors)
    resistances = _deduplicated_resistances(anchor_result.anchors)
    for base_rank, cluster in enumerate(clusters, start=1):
        reasons = list(cluster.reason_codes)
        protective_candidates = [price for price in supports if price <= cluster.entry_low]
        protective_level = max(protective_candidates) if protective_candidates else None
        stop_loss = None
        if protective_level is None:
            reasons.append("NO_VALID_STOP")
        else:
            stop_loss = floor_to_tick(
                min(cluster.entry_low, protective_level)
                - params.stop_buffer_atr * anchor_result.atr,
                snapshot.market_rules.tick_size,
            )
            stop_distance = (cluster.entry_high - stop_loss) / anchor_result.atr
            if stop_loss >= cluster.entry_low or stop_distance > params.max_stop_distance_atr:
                reasons.append("NO_VALID_STOP")
                stop_loss = None

        target_1 = None
        target_2 = None
        target_candidates = [
            item.price for item in resistances if item.price > cluster.entry_high
        ]
        if not target_candidates:
            reasons.append("NO_VALID_TARGET")
        else:
            nearest = target_candidates[0]
            if nearest - cluster.entry_high < params.min_target_gap_atr * anchor_result.atr:
                reasons.append("TARGET_TOO_CLOSE")
            else:
                target_1 = floor_to_tick(nearest, snapshot.market_rules.tick_size)
                target_2 = next(
                    (
                        floor_to_tick(value, snapshot.market_rules.tick_size)
                        for value in target_candidates[1:]
                        if value - target_1
                        >= params.second_target_gap_atr * anchor_result.atr
                    ),
                    None,
                )

        net_risk = None
        net_reward = None
        net_rr = None
        if stop_loss is not None and target_1 is not None:
            net_risk, net_reward, net_rr = _reference_net_rr(
                entry_reference=cluster.entry_high,
                stop_loss=stop_loss,
                target_1=target_1,
                params=params,
            )
            if net_rr is None or net_rr < params.min_reference_net_rr_1:
                reasons.append("RISK_REWARD_BELOW_MINIMUM")

        if weekly_filter == "opposing":
            reasons.append("WEEKLY_TREND_OPPOSING")
        formal = cluster.is_formal_candidate and not reasons
        relation, reachability = _reachability(
            last_cash_price=snapshot.last_cash_price,
            entry_low=cluster.entry_low,
            entry_high=cluster.entry_high,
            stop_loss=stop_loss,
            atr=anchor_result.atr,
            params=params,
        )
        scenario_seed = {
            "calculation": fingerprint(
                {
                    "snapshot": snapshot.symbol,
                    "as_of": snapshot.actual_as_of_session,
                    "params": params.version,
                }
            ),
            "rank": base_rank,
            "anchors": [item.anchor_id for item in cluster.representatives],
            "entry_low": cluster.entry_low,
            "entry_high": cluster.entry_high,
            "stop_loss": stop_loss,
            "target_1": target_1,
            "target_2": target_2,
            "reasons": sorted(set(reasons)),
            "confirmation_seed_bars": list(
                snapshot.daily_bars[-params.confirmation_volume_ma_bars :]
            ),
        }
        scenario_id = stable_id("scenario", scenario_seed, length=20)
        confirmation_rules = _confirmation_rules(
            params,
            tick_size=snapshot.market_rules.tick_size,
        )
        invalidation_rules = _invalidation_rules(
            params,
            stop_loss,
            breakdown_price=floor_to_tick(
                cluster.entry_low - params.breakdown_buffer_atr * anchor_result.atr,
                snapshot.market_rules.tick_size,
            ),
        )
        scenario_fingerprint = fingerprint(
            {
                **scenario_seed,
                "confirmation_rules": confirmation_rules,
                "invalidation_rules": invalidation_rules,
            }
        )
        scenarios.append(
            ZoneScenario(
                scenario_id=scenario_id,
                role=ScenarioRole.PRIMARY if formal else ScenarioRole.OBSERVATION,
                base_rank=base_rank,
                source_families=list(cluster.source_families),
                anchor_refs=[item.anchor_id for item in cluster.representatives],
                entry_low=cluster.entry_low,
                entry_high=cluster.entry_high,
                entry_reference=cluster.entry_high,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                target_1_exit_ratio=(
                    params.target_1_exit_ratio if target_2 is not None else Decimal("1")
                ),
                reference_net_rr_1=net_rr,
                reference_net_risk=net_risk,
                reference_net_reward_1=net_reward,
                reachability_band=reachability,
                price_relation=relation,
                confirmation_rules=confirmation_rules,
                invalidation_rules=invalidation_rules,
                confirmation_seed_bars=list(
                    snapshot.daily_bars[-params.confirmation_volume_ma_bars :]
                ),
                valid_from=_add_weekday_sessions(snapshot.actual_as_of_session, 1),
                confirmation_deadline=_add_weekday_sessions(
                    snapshot.actual_as_of_session,
                    params.confirmation_valid_trading_days,
                ),
                confirmation_valid_trading_days=params.confirmation_valid_trading_days,
                order_valid_trading_days=params.order_valid_trading_days,
                scenario_reason_codes=sorted(set(reasons)),
                scenario_fingerprint=scenario_fingerprint,
            )
        )
    return scenarios


def _assign_roles(scenarios: Sequence[ZoneScenario]) -> list[ZoneScenario]:
    formal_index = 0
    output = []
    for item in scenarios:
        if item.scenario_reason_codes:
            output.append(item.model_copy(update={"role": ScenarioRole.OBSERVATION}))
            continue
        role = ScenarioRole.PRIMARY if formal_index == 0 else ScenarioRole.SECONDARY
        formal_index += 1
        output.append(item.model_copy(update={"role": role}))
    return output


def _critical_reasons(
    snapshot: PricePlanSnapshot, params: PricePlanParams
) -> list[str]:
    reasons: list[str] = []
    if not is_supported_cn_common_stock(snapshot.symbol):
        reasons.append("UNSUPPORTED_INSTRUMENT")
    required_daily = params.daily_evaluation_bars + params.daily_warmup_bars
    required_weekly = params.weekly_evaluation_bars + params.weekly_warmup_bars
    if len(snapshot.daily_bars) < required_daily:
        reasons.append("INSUFFICIENT_DAILY_HISTORY")
    if len(snapshot.weekly_bars) < required_weekly:
        reasons.append("INSUFFICIENT_WEEKLY_HISTORY")
    if snapshot.last_cash_price is None:
        reasons.append("MISSING_CASH_PRICE")
    if not snapshot.price_scale_verified:
        reasons.append("PRICE_SCALE_UNVERIFIED")
    if (
        snapshot.market_rules.tick_size <= 0
        or snapshot.market_rules.previous_close is None
        or snapshot.market_rules.limit_up is None
        or snapshot.market_rules.limit_down is None
        or not snapshot.market_rules.instrument_type_verified
        or snapshot.market_rules.instrument_type != "common_stock"
        or not snapshot.market_rules.st_status_verified
        or not snapshot.market_rules.listing_status_verified
        or snapshot.market_rules.listing_status != "listed"
        or snapshot.market_rules.suspended
    ):
        reasons.append("MARKET_RULES_UNAVAILABLE")
    if snapshot.actual_as_of_session > snapshot.requested_as_of:
        reasons.append("AS_OF_INCONSISTENT")
    return reasons


def _no_plan(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    *,
    data_fingerprint: str,
    calculation_fingerprint: str,
    reasons: Sequence[str],
    trace_ref: str | None,
    anchors: Sequence[AnchorEvidence] = (),
) -> PricePlanArtifact:
    unique_reasons = sorted(set(reasons))
    plan_payload = {
        "calculation_fingerprint": calculation_fingerprint,
        "generation_status": "no_plan",
        "reasons": unique_reasons,
    }
    return PricePlanArtifact(
        plan_id=stable_id("plan", plan_payload),
        revision=1,
        symbol=snapshot.symbol,
        requested_as_of=snapshot.requested_as_of,
        actual_as_of_session=snapshot.actual_as_of_session,
        generation_status=GenerationStatus.NO_PLAN,
        scenarios=[],
        observation_anchors=list(anchors),
        market_rules_snapshot_ref=snapshot.market_rules.snapshot_id,
        data_quality="invalid",
        reason_codes=unique_reasons,
        diagnostics={
            "daily_bar_count": len(snapshot.daily_bars),
            "weekly_bar_count": len(snapshot.weekly_bars),
        },
        algorithm_version=params.algorithm_version,
        params_version=params.version,
        data_fingerprint=data_fingerprint,
        calculation_fingerprint=calculation_fingerprint,
        plan_fingerprint=fingerprint(plan_payload),
        created_at=_deterministic_created_at(snapshot),
        trace_ref=trace_ref,
    )


def _structure_supports(anchors: Iterable[AnchorEvidence]) -> list[Decimal]:
    return sorted(
        item.price
        for item in anchors
        if item.source_family == "price_structure"
        and item.role is AnchorRole.SUPPORT
        and item.anchor_status is AnchorStatus.ELIGIBLE
    )


def _deduplicated_resistances(
    anchors: Iterable[AnchorEvidence],
) -> list[AnchorEvidence]:
    candidates = [
        item
        for item in anchors
        if item.role is AnchorRole.RESISTANCE
        and item.anchor_status is AnchorStatus.ELIGIBLE
    ]
    by_family: dict[str, list[AnchorEvidence]] = {}
    for item in candidates:
        by_family.setdefault(item.source_family, []).append(item)
    output = []
    for family in sorted(by_family):
        output.extend(sorted(by_family[family], key=lambda item: (item.price, item.anchor_id)))
    return sorted(output, key=lambda item: (item.price, item.source_family, item.anchor_id))


def _reference_net_rr(
    *,
    entry_reference: Decimal,
    stop_loss: Decimal,
    target_1: Decimal,
    params: PricePlanParams,
) -> tuple[Decimal, Decimal, Decimal | None]:
    divisor = Decimal("10000")
    entry_net = entry_reference * (
        Decimal("1")
        + (params.reference_buy_cost_bps + params.reference_buy_slippage_bps)
        / divisor
    )
    stop_net = stop_loss * (
        Decimal("1")
        - (params.reference_sell_cost_bps + params.reference_sell_slippage_bps)
        / divisor
    )
    target_net = target_1 * (
        Decimal("1")
        - (params.reference_sell_cost_bps + params.reference_sell_slippage_bps)
        / divisor
    )
    risk = entry_net - stop_net
    reward = target_net - entry_net
    return risk, reward, reward / risk if risk > 0 else None


def _reachability(
    *,
    last_cash_price: Decimal,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal | None,
    atr: Decimal,
    params: PricePlanParams,
) -> tuple[PriceRelation, ReachabilityBand]:
    if last_cash_price > entry_high:
        relation = PriceRelation.ABOVE
        distance = (last_cash_price - entry_high) / atr
    elif entry_low <= last_cash_price <= entry_high:
        relation = PriceRelation.INSIDE
        distance = Decimal("0")
    elif stop_loss is None or last_cash_price > stop_loss:
        return PriceRelation.BELOW, ReachabilityBand.UNKNOWN
    else:
        return PriceRelation.INVALIDATED, ReachabilityBand.UNKNOWN
    if distance <= params.near_distance_atr:
        return relation, ReachabilityBand.NEAR
    if distance <= params.medium_distance_atr:
        return relation, ReachabilityBand.MEDIUM
    return relation, ReachabilityBand.FAR


def _weekly_filter(bars: Sequence[object]) -> str:
    closes = [bar.close for bar in bars]
    ema = _ema(closes, 13)
    weekly_above = closes[-1] >= ema[-1]
    weekly_rising = ema[-1] > ema[-4]
    if weekly_above and weekly_rising:
        return "supportive"
    if weekly_above or weekly_rising:
        return "neutral"
    return "opposing"


def _ema(values: Sequence[Decimal], span: int) -> list[Decimal]:
    alpha = Decimal(2) / Decimal(span + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (Decimal("1") - alpha) * output[-1])
    return output


def _confirmation_rules(
    params: PricePlanParams,
    *,
    tick_size: Decimal,
) -> list[RuleSpec]:
    return [
        RuleSpec(
            rule_type="recent_zone_touch",
            params={"bars": params.confirmation_after_touch_bars},
            rule_version="price-confirmation.v1",
        ),
        RuleSpec(
            rule_type="closed_bar_reversal_pattern",
            params={
                "patterns": "hammer|bullish_engulfing|confirmed_bottom_fractal",
                "tick_size": tick_size,
                "hammer_lower_body_ratio": params.hammer_lower_body_ratio,
                "hammer_upper_body_ratio": params.hammer_upper_body_ratio,
                "hammer_close_location_min": params.hammer_close_location_min,
            },
            rule_version="price-confirmation.v1",
        ),
        RuleSpec(
            rule_type="pattern_volume_confirmation",
            params={
                "contraction_ratio": params.contraction_volume_ratio,
                "expansion_ratio": params.expansion_volume_ratio,
                "volume_ma_bars": params.confirmation_volume_ma_bars,
            },
            rule_version="price-confirmation.v1",
        ),
    ]


def _add_weekday_sessions(start: date, count: int) -> date:
    current = start
    remaining = max(0, count)
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5 and is_market_open("cn", current):
            remaining -= 1
    return current


def _invalidation_rules(
    params: PricePlanParams,
    stop_loss: Decimal | None,
    *,
    breakdown_price: Decimal,
) -> list[RuleSpec]:
    rules = [
        RuleSpec(
            rule_type="volume_breakdown",
            params={
                "buffer_atr": params.breakdown_buffer_atr,
                "breakdown_price": breakdown_price,
                "volume_ratio": params.breakdown_volume_ratio,
                "volume_ma_bars": params.confirmation_volume_ma_bars,
            },
            rule_version="price-invalidation.v1",
        )
    ]
    if stop_loss is not None:
        rules.insert(
            0,
            RuleSpec(
                rule_type="close_at_or_below_stop",
                params={"stop_loss": stop_loss},
                rule_version="price-invalidation.v1",
            ),
        )
    return rules


def _fingerprint_snapshot(snapshot: PricePlanSnapshot) -> dict[str, object]:
    return {
        "schema_version": snapshot.schema_version,
        "symbol": snapshot.symbol,
        "market": snapshot.market,
        "timeframe": snapshot.timeframe,
        "requested_as_of": snapshot.requested_as_of,
        "actual_as_of_session": snapshot.actual_as_of_session,
        "daily_bars": snapshot.daily_bars,
        "weekly_bars": snapshot.weekly_bars,
        "last_cash_price": snapshot.last_cash_price,
        "price_basis": snapshot.price_basis,
        "price_scale_verified": snapshot.price_scale_verified,
        "adjustment_factor_semantics": snapshot.adjustment_factor_semantics,
        "adjustment_factor_version": snapshot.adjustment_factor_version,
        "history_source": snapshot.history_source,
        "history_source_version": snapshot.history_source_version,
        "market_rules": snapshot.market_rules,
        "chip": snapshot.chip,
        "structure_evidence": snapshot.structure_evidence,
        "missing_fields": sorted(snapshot.missing_fields),
    }


def _anchor_counts(anchors: Sequence[AnchorEvidence]) -> dict[str, int]:
    output: dict[str, int] = {}
    for item in anchors:
        key = f"{item.source_family}:{item.anchor_status.value}"
        output[key] = output.get(key, 0) + 1
    return output


def _deterministic_created_at(snapshot: PricePlanSnapshot) -> datetime:
    return datetime.combine(
        snapshot.actual_as_of_session,
        time(hour=23, minute=59, second=59),
        tzinfo=timezone.utc,
    )
