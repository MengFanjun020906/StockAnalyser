from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any, Iterable, Sequence

from src.agent.structure.price_structure import analyze_price_structure, coerce_price_bars

from .fingerprint import fingerprint, stable_id
from .params import PricePlanParams
from .schemas import (
    AnchorEvidence,
    AnchorRole,
    AnchorStatus,
    PriceBarSnapshot,
    PricePlanSnapshot,
)


COUNTING_FAMILIES = {"trend", "cost_volume", "price_structure"}


@dataclass(frozen=True)
class AnchorBuildResult:
    anchors: list[AnchorEvidence]
    atr: Decimal
    structure_evidence: dict[str, Any]


def build_anchors(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    *,
    disabled_families: frozenset[str] = frozenset(),
) -> AnchorBuildResult:
    bars = snapshot.daily_bars
    atr = calculate_atr(bars, params.atr_period)
    structure = snapshot.structure_evidence or analyze_price_structure(
        coerce_price_bars([bar.model_dump(mode="python") for bar in bars]),
        include_raw=False,
    )
    anchors: list[AnchorEvidence] = []
    if "trend" not in disabled_families:
        anchors.extend(_trend_anchors(snapshot, params, atr))
    if "cost_volume" not in disabled_families:
        anchors.extend(_cost_volume_anchors(snapshot, params, atr, structure))
    if "price_structure" not in disabled_families:
        anchors.extend(_structure_anchors(snapshot, params, atr, structure))
    if "volatility_boundary" not in disabled_families:
        anchors.extend(_volatility_anchors(snapshot, params, atr))
    return AnchorBuildResult(anchors=anchors, atr=atr, structure_evidence=structure)


def calculate_atr(bars: Sequence[PriceBarSnapshot], period: int) -> Decimal:
    if len(bars) < period + 1:
        raise ValueError("insufficient bars for ATR")
    true_ranges: list[Decimal] = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges[-period:], Decimal("0")) / Decimal(period)


def _trend_anchors(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    atr: Decimal,
) -> list[AnchorEvidence]:
    closes = [bar.close for bar in snapshot.daily_bars]
    anchors: list[AnchorEvidence] = []
    for period in (5, 10, 20, 30, 57, 120):
        values = _rolling_mean(closes, period)
        anchors.append(
            _series_anchor(snapshot, params, atr, f"ma{period}", values, source_priority=period)
        )
    for span in (13, 34):
        values = _ema(closes, span)
        anchors.append(
            _series_anchor(snapshot, params, atr, f"ema{span}", values, source_priority=span + 1)
        )
    boll_mid = _rolling_mean(closes, 20)
    anchors.append(
        _series_anchor(snapshot, params, atr, "boll_mid", boll_mid, source_priority=80)
    )
    return anchors


def _series_anchor(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    atr: Decimal,
    anchor_type: str,
    values: Sequence[Decimal],
    *,
    source_priority: int,
) -> AnchorEvidence:
    price = values[-1]
    lookback = min(3, len(values) - 1)
    delta = price - values[-1 - lookback]
    threshold = atr * Decimal("0.02")
    direction = "rising" if delta > threshold else "falling" if delta < -threshold else "flat"
    role = AnchorRole.SUPPORT if price <= snapshot.last_cash_price else AnchorRole.RESISTANCE
    direction_ok = (
        direction in {"rising", "flat"}
        if role is AnchorRole.SUPPORT
        else direction in {"falling", "flat"}
    )
    reactions, reaction_moves = _series_reactions(
        snapshot.daily_bars,
        values,
        role,
        atr,
        params,
    )
    broken = _is_broken(snapshot.daily_bars, price, role, atr, params)
    status = AnchorStatus.ELIGIBLE
    reasons: list[str] = []
    if not direction_ok:
        status = AnchorStatus.OBSERVATION
        reasons.append("DIRECTION_NOT_ELIGIBLE")
    elif broken:
        status = AnchorStatus.REJECTED
        reasons.append("ANCHOR_BROKEN")
    elif reactions < 1:
        status = AnchorStatus.OBSERVATION
        reasons.append("INSUFFICIENT_REACTIONS")
    return _anchor(
        snapshot,
        anchor_type=anchor_type,
        source_family="trend",
        price=price,
        role=role,
        direction=direction,
        status=status,
        confirmation_date=snapshot.actual_as_of_session,
        source_priority=source_priority,
        age_bars=0,
        reaction_count=reactions,
        median_reaction_move_atr=_median_decimal(reaction_moves),
        source_ref=f"indicator:{anchor_type}:priority:{source_priority}",
        atr=atr,
        params=params,
        reason_codes=reasons,
    )


def _cost_volume_anchors(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    atr: Decimal,
    structure: dict[str, Any],
) -> list[AnchorEvidence]:
    anchors: list[AnchorEvidence] = []
    chip = snapshot.chip
    if chip is not None and chip.trade_date <= snapshot.actual_as_of_session:
        values = {
            "chip_avg_cost": chip.avg_cost,
            "chip_cost_70_low": chip.cost_70_low,
            "chip_cost_70_high": chip.cost_70_high,
            "chip_cost_90_low": chip.cost_90_low,
            "chip_cost_90_high": chip.cost_90_high,
        }
        age = _age_bars(snapshot.daily_bars, chip.trade_date)
        for source_priority, (anchor_type, price) in enumerate(values.items(), start=10):
            if price is None:
                continue
            role = AnchorRole.SUPPORT if price <= snapshot.last_cash_price else AnchorRole.RESISTANCE
            broken = _is_broken(snapshot.daily_bars, price, role, atr, params)
            anchors.append(
                _anchor(
                    snapshot,
                    anchor_type=anchor_type,
                    source_family="cost_volume",
                    price=price,
                    role=role,
                    direction="event",
                    status=AnchorStatus.REJECTED if broken else AnchorStatus.ELIGIBLE,
                    confirmation_date=chip.trade_date,
                    source_priority=source_priority,
                    age_bars=age,
                    reaction_count=0,
                    median_reaction_move_atr=Decimal("0"),
                    source_ref=chip.source_ref,
                    atr=atr,
                    params=params,
                    reason_codes=["ANCHOR_BROKEN"] if broken else [],
                )
            )
    event = _latest_structure_event(structure, kind="low")
    if event is not None:
        event_index, event_date, _ = event
        avwap = _anchored_vwap(snapshot.daily_bars[event_index:])
        if avwap is not None:
            structure_confirmed = event_index + 3 < len(snapshot.daily_bars)
            confirmation_date = (
                snapshot.daily_bars[event_index + 3].trade_date
                if structure_confirmed
                else snapshot.actual_as_of_session
            )
            role = AnchorRole.SUPPORT if avwap <= snapshot.last_cash_price else AnchorRole.RESISTANCE
            anchors.append(
                _anchor(
                    snapshot,
                    anchor_type="anchored_vwap_swing_low",
                    source_family="cost_volume",
                    price=avwap,
                    role=role,
                    direction="event",
                    status=(
                        AnchorStatus.ELIGIBLE
                        if structure_confirmed
                        else AnchorStatus.OBSERVATION
                    ),
                    confirmation_date=confirmation_date,
                    source_priority=50,
                    age_bars=max(0, len(snapshot.daily_bars) - event_index - 4),
                    reaction_count=0,
                    median_reaction_move_atr=Decimal("0"),
                    source_ref=f"price_structure:swing_low:{event_date.isoformat()}",
                    atr=atr,
                    params=params,
                    reason_codes=[] if structure_confirmed else ["STRUCTURE_UNCONFIRMED"],
                )
            )
    return anchors


def _structure_anchors(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    atr: Decimal,
    structure: dict[str, Any],
) -> list[AnchorEvidence]:
    anchors: list[AnchorEvidence] = []
    smc = structure.get("smc") if isinstance(structure.get("smc"), dict) else {}
    swings = smc.get("latest_swings") if isinstance(smc.get("latest_swings"), list) else []
    for item in swings[-8:]:
        try:
            index = int(item["index"])
            price = Decimal(str(item["price"]))
            role = AnchorRole.SUPPORT if item.get("type") == "low" else AnchorRole.RESISTANCE
            confirmation_index = index + 3
            confirmed = confirmation_index < len(snapshot.daily_bars)
            if not confirmed:
                confirmation_index = len(snapshot.daily_bars) - 1
            confirmation_date = snapshot.daily_bars[confirmation_index].trade_date
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        broken = _is_broken(snapshot.daily_bars, price, role, atr, params)
        reason_codes = []
        if not confirmed:
            reason_codes.append("STRUCTURE_UNCONFIRMED")
        if broken:
            reason_codes.append("ANCHOR_BROKEN")
        anchors.append(
            _anchor(
                snapshot,
                anchor_type=f"confirmed_swing_{item.get('type')}",
                source_family="price_structure",
                price=price,
                role=role,
                direction="event",
                status=(
                    AnchorStatus.REJECTED
                    if broken
                    else AnchorStatus.OBSERVATION
                    if not confirmed
                    else AnchorStatus.ELIGIBLE
                ),
                confirmation_date=confirmation_date,
                source_priority=10,
                age_bars=len(snapshot.daily_bars) - confirmation_index - 1,
                reaction_count=1,
                median_reaction_move_atr=Decimal("1"),
                source_ref=f"price_structure:smc:{item.get('label') or item.get('type')}:{index}",
                atr=atr,
                params=params,
                reason_codes=reason_codes,
            )
        )
    chan = structure.get("chan") if isinstance(structure.get("chan"), dict) else {}
    centers = chan.get("latest_centers") if isinstance(chan.get("latest_centers"), list) else []
    for center in centers[-3:]:
        for key, role in (("ZD", AnchorRole.SUPPORT), ("ZG", AnchorRole.RESISTANCE)):
            try:
                price = Decimal(str(center[key]))
                index = int(center.get("end_index", len(snapshot.daily_bars) - 1))
                confirmation_date = snapshot.daily_bars[min(index, len(snapshot.daily_bars) - 1)].trade_date
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            broken = _is_broken(snapshot.daily_bars, price, role, atr, params)
            anchors.append(
                _anchor(
                    snapshot,
                    anchor_type=f"chan_center_{key.lower()}",
                    source_family="price_structure",
                    price=price,
                    role=role,
                    direction="event",
                    status=AnchorStatus.REJECTED if broken else AnchorStatus.ELIGIBLE,
                    confirmation_date=confirmation_date,
                    source_priority=20,
                    age_bars=max(0, len(snapshot.daily_bars) - index - 1),
                    reaction_count=1,
                    median_reaction_move_atr=Decimal("1"),
                    source_ref=f"price_structure:chan:center:{center.get('id')}:{key}",
                    atr=atr,
                    params=params,
                    reason_codes=["ANCHOR_BROKEN"] if broken else [],
                )
            )
    return anchors


def _volatility_anchors(
    snapshot: PricePlanSnapshot,
    params: PricePlanParams,
    atr: Decimal,
) -> list[AnchorEvidence]:
    closes = [bar.close for bar in snapshot.daily_bars]
    mid = _rolling_mean(closes, 20)[-1]
    variance = sum((item - mid) ** 2 for item in closes[-20:]) / Decimal(20)
    std = variance.sqrt()
    result = []
    for anchor_type, price, role in (
        ("boll_lower", mid - Decimal(2) * std, AnchorRole.SUPPORT),
        ("boll_upper", mid + Decimal(2) * std, AnchorRole.RESISTANCE),
    ):
        result.append(
            _anchor(
                snapshot,
                anchor_type=anchor_type,
                source_family="volatility_boundary",
                price=price,
                role=role,
                direction="event",
                status=AnchorStatus.ELIGIBLE,
                confirmation_date=snapshot.actual_as_of_session,
                source_priority=100,
                age_bars=0,
                reaction_count=0,
                median_reaction_move_atr=Decimal("0"),
                source_ref=f"indicator:{anchor_type}",
                atr=atr,
                params=params,
                reason_codes=[],
            )
        )
    return result


def _anchor(
    snapshot: PricePlanSnapshot,
    *,
    anchor_type: str,
    source_family: str,
    price: Decimal,
    role: AnchorRole,
    direction: str,
    status: AnchorStatus,
    confirmation_date: Any,
    source_priority: int,
    age_bars: int,
    reaction_count: int,
    median_reaction_move_atr: Decimal,
    source_ref: str,
    atr: Decimal,
    params: PricePlanParams,
    reason_codes: list[str],
) -> AnchorEvidence:
    max_age = _max_anchor_age(anchor_type, source_family, params)
    if age_bars > max_age:
        status = AnchorStatus.REJECTED
        reason_codes = [*reason_codes, "ANCHOR_EXPIRED"]
    seed = {
        "symbol": snapshot.symbol,
        "as_of": snapshot.actual_as_of_session,
        "type": anchor_type,
        "family": source_family,
        "price": price,
        "source_ref": source_ref,
        "source_priority": source_priority,
        "algorithm": params.algorithm_version,
    }
    return AnchorEvidence(
        anchor_id=stable_id("anchor", seed, length=20),
        symbol=snapshot.symbol,
        anchor_type=anchor_type,
        source_family=source_family,
        price=price,
        role=role,
        direction=direction,
        anchor_status=status,
        as_of=snapshot.actual_as_of_session,
        confirmation_date=confirmation_date,
        source_ref=source_ref,
        source_priority=source_priority,
        distance_atr=(snapshot.last_cash_price - price) / atr,
        age_bars=age_bars,
        reaction_count=reaction_count,
        median_reaction_move_atr=median_reaction_move_atr,
        calculation_fingerprint=fingerprint(seed),
        reason_codes=reason_codes,
    )


def _max_anchor_age(
    anchor_type: str,
    source_family: str,
    params: PricePlanParams,
) -> int:
    if anchor_type.startswith("chip_"):
        return params.chip_anchor_max_age_bars
    if anchor_type.startswith("anchored_vwap_"):
        return params.event_anchor_max_age_bars
    if source_family == "price_structure":
        return params.structure_anchor_max_age_bars
    return params.daily_evaluation_bars


def _rolling_mean(values: Sequence[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        raise ValueError(f"insufficient values for MA{period}")
    output: list[Decimal] = []
    running = sum(values[:period], Decimal("0"))
    output.extend([running / Decimal(period)] * period)
    for index in range(period, len(values)):
        running += values[index] - values[index - period]
        output.append(running / Decimal(period))
    return output


def _ema(values: Sequence[Decimal], span: int) -> list[Decimal]:
    alpha = Decimal(2) / Decimal(span + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (Decimal(1) - alpha) * output[-1])
    return output


def _series_reactions(
    bars: Sequence[PriceBarSnapshot],
    values: Sequence[Decimal],
    role: AnchorRole,
    atr: Decimal,
    params: PricePlanParams,
) -> tuple[int, list[Decimal]]:
    moves: list[Decimal] = []
    next_allowed = 0
    start = max(0, len(bars) - params.daily_evaluation_bars)
    end = len(bars) - params.reaction_horizon_bars
    for index in range(start, end):
        if index < next_allowed:
            continue
        bar = bars[index]
        price = values[index]
        touch_atr = _atr_at_index(bars, index, params.atr_period) or atr
        if bar.low > price + params.reaction_touch_atr * touch_atr:
            continue
        if bar.high < price - params.reaction_touch_atr * touch_atr:
            continue
        future = bars[index + 1:index + 1 + params.reaction_horizon_bars]
        if role is AnchorRole.SUPPORT:
            move = (max(item.high for item in future) - price) / touch_atr
        else:
            move = (price - min(item.low for item in future)) / touch_atr
        if move >= params.reaction_move_atr:
            moves.append(move)
            next_allowed = index + params.reaction_min_gap_bars
    return len(moves), moves


def _atr_at_index(
    bars: Sequence[PriceBarSnapshot],
    index: int,
    period: int,
) -> Decimal | None:
    if index < period:
        return None
    true_ranges = []
    for offset in range(index - period + 1, index + 1):
        previous = bars[offset - 1]
        current = bars[offset]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    value = sum(true_ranges, Decimal("0")) / Decimal(period)
    return value if value > 0 else None


def _is_broken(
    bars: Sequence[PriceBarSnapshot],
    price: Decimal,
    role: AnchorRole,
    atr: Decimal,
    params: PricePlanParams,
) -> bool:
    tail = bars[-params.break_confirm_bars:]
    if role is AnchorRole.SUPPORT:
        boundary = price - params.break_buffer_atr * atr
        return all(bar.close < boundary for bar in tail)
    boundary = price + params.break_buffer_atr * atr
    return all(bar.close > boundary for bar in tail)


def _latest_structure_event(
    structure: dict[str, Any], *, kind: str
) -> tuple[int, Any, Decimal] | None:
    smc = structure.get("smc") if isinstance(structure.get("smc"), dict) else {}
    swings = smc.get("latest_swings") if isinstance(smc.get("latest_swings"), list) else []
    for item in reversed(swings):
        if item.get("type") != kind:
            continue
        try:
            return int(item["index"]), _as_date(item["time"]), Decimal(str(item["price"]))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _anchored_vwap(bars: Iterable[PriceBarSnapshot]) -> Decimal | None:
    numerator = Decimal("0")
    denominator = Decimal("0")
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / Decimal(3)
        numerator += typical * bar.volume
        denominator += bar.volume
    return numerator / denominator if denominator > 0 else None


def _age_bars(bars: Sequence[PriceBarSnapshot], event_date: Any) -> int:
    return sum(1 for bar in bars if bar.trade_date > event_date)


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else Decimal("0")


def _as_date(value: Any) -> Any:
    from datetime import date

    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
