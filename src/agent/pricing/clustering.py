from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from statistics import median
from typing import Sequence

from .anchors import COUNTING_FAMILIES
from .params import PricePlanParams
from .schemas import AnchorEvidence, AnchorRole, AnchorStatus


@dataclass(frozen=True)
class AnchorCluster:
    representatives: tuple[AnchorEvidence, ...]
    entry_low: Decimal
    entry_high: Decimal
    source_families: tuple[str, ...]
    is_formal_candidate: bool
    reason_codes: tuple[str, ...]


def build_support_clusters(
    anchors: Sequence[AnchorEvidence],
    *,
    atr: Decimal,
    tick_size: Decimal,
    params: PricePlanParams,
) -> list[AnchorCluster]:
    candidates = sorted(
        (
            item
            for item in anchors
            if item.anchor_status is AnchorStatus.ELIGIBLE
            and item.role is AnchorRole.SUPPORT
            and item.source_family in COUNTING_FAMILIES
        ),
        key=lambda item: (item.price, item.anchor_id),
    )
    raw_clusters: list[list[AnchorEvidence]] = []
    for anchor in candidates:
        if not raw_clusters:
            raw_clusters.append([anchor])
            continue
        current = raw_clusters[-1]
        gap_ok = anchor.price - current[-1].price <= params.cluster_gap_atr * atr
        width_ok = anchor.price - current[0].price <= params.max_cluster_width_atr * atr
        if gap_ok and width_ok:
            current.append(anchor)
        else:
            raw_clusters.append([anchor])

    output: list[AnchorCluster] = []
    for group in raw_clusters:
        representatives = _family_representatives(group)
        prices = [item.price for item in representatives]
        padding = max(tick_size, params.zone_padding_atr * atr)
        low = floor_to_tick(min(prices) - padding, tick_size)
        high = ceil_to_tick(max(prices) + padding, tick_size)
        families = tuple(sorted({item.source_family for item in representatives}))
        reasons: list[str] = []
        if len(families) < 2:
            reasons.append("INSUFFICIENT_SOURCE_FAMILIES")
        if high - low > params.max_zone_width_atr * atr:
            reasons.append("ZONE_WIDTH_EXCEEDED")
        output.append(
            AnchorCluster(
                representatives=tuple(representatives),
                entry_low=low,
                entry_high=high,
                source_families=families,
                is_formal_candidate=not reasons,
                reason_codes=tuple(reasons),
            )
        )
    return output


def cluster_sort_key(
    cluster: AnchorCluster, *, last_cash_price: Decimal, atr: Decimal
) -> tuple[object, ...]:
    contains_structure = "price_structure" in cluster.source_families
    reaction_median = median(
        [float(item.median_reaction_move_atr) for item in cluster.representatives]
    )
    distance = max(Decimal("0"), last_cash_price - cluster.entry_high) / atr
    oldest = max(item.age_bars for item in cluster.representatives)
    return (
        -len(cluster.source_families),
        -int(contains_structure),
        -reaction_median,
        distance,
        cluster.entry_high - cluster.entry_low,
        oldest,
        tuple(item.anchor_id for item in cluster.representatives),
    )


def floor_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    units = (value / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    return units * tick_size


def ceil_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    units = (value / tick_size).to_integral_value(rounding=ROUND_CEILING)
    return units * tick_size


def _family_representatives(group: Sequence[AnchorEvidence]) -> list[AnchorEvidence]:
    by_family: dict[str, list[AnchorEvidence]] = {}
    for item in group:
        by_family.setdefault(item.source_family, []).append(item)
    representatives = []
    for family in sorted(by_family):
        representatives.append(
            sorted(
                by_family[family],
                key=lambda item: (
                    -min(item.reaction_count, 5),
                    -item.median_reaction_move_atr,
                    item.age_bars,
                    item.source_priority,
                    item.anchor_id,
                ),
            )[0]
        )
    return representatives
