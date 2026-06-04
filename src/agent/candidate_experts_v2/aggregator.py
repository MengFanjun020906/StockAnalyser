# -*- coding: utf-8 -*-
"""P4 aggregator: merge desk picks → AggregatedPool, then allocate regime slots.

Two public functions:
    aggregate_desk_picks(packets, rows) -> AggregatedPool
    allocate_slots(pool, regime, *, total, allocation_json, backfill_rules_json,
                   backfill_max, pick_top_n) -> List[AggregatedCandidate]
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from src.agent.candidate_experts_v2.schemas import (
    AggregatedCandidate,
    AggregatedPool,
    EvidenceItem,
    ExpertPacketV2,
    FactSheet,
    FeatureRow,
    RiskNote,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dimension → tool mapping for confidence calculation (per spec §4.4)
# ---------------------------------------------------------------------------

_DESK_DIM_TOOL_MAP: Dict[str, Dict[str, Set[str]]] = {
    "early_turn_desk": {
        "position": {"analyze_price_structure", "calculate_ma"},
        "volume":   {"get_volume_analysis"},
        "trend":    {"analyze_trend"},
        "capital":  {"get_capital_flow"},
    },
    "momentum_desk": {
        "trend":     {"analyze_trend"},
        "volume":    {"get_volume_analysis"},
        "capital":   {"get_tushare_moneyflow_dc", "get_capital_flow"},
        "structure": {"get_tushare_limit_step", "get_tushare_dragon_tiger_list",
                      "get_tushare_dragon_tiger_inst", "get_tushare_limit_list_d"},
    },
    "quality_repair_desk": {
        "earnings":  {"get_tushare_financial_indicators", "get_tushare_forecast",
                      "get_tushare_express", "get_tushare_financial_statements"},
        "valuation": {"get_tushare_daily_basic"},
        "position":  {"analyze_price_structure"},
    },
    "theme_catalyst_desk": {
        "theme":    {"get_eastmoney_cjzc_daily", "search_stock_news", "score_stock_news_sentiment"},
        "business": {"get_stock_business_context"},
        "sector":   {"get_stockapi_hot_sectors", "get_stockapi_hot_sector_leaders"},
        "capital":  {"get_stockapi_hot_money_activity", "get_capital_flow", "get_volume_analysis"},
        "position": {"get_realtime_quote", "analyze_price_structure"},
    },
}

# Conflict detection thresholds
_HIGH_RANGE_PCT_THRESHOLD = 0.70   # thesis_position_mismatch
_HIGH_GAIN_THRESHOLD = 30.0        # quality_price_mismatch (30% gain since base)

# Stance strength ordering
_STANCE_STRENGTH: Dict[str, int] = {
    "support": 3,
    "watch": 2,
    "neutral": 1,
    "oppose": 0,
    "invalid": -1,
}

# Desk priority order for primary_desk resolution (lower = higher priority)
_DESK_PRIORITY: Dict[str, int] = {
    "theme_catalyst_desk": 0,
    "early_turn_desk": 1,
    "quality_repair_desk": 2,
    "momentum_desk": 3,
}

# Default slot allocation (used when no config JSON provided)
_DEFAULT_ALLOCATION: Dict[str, Dict[str, int]] = {
    "trending_up":     {"theme_catalyst_desk": 2, "early_turn_desk": 2, "momentum_desk": 3, "quality_repair_desk": 1},
    "range_bound":     {"theme_catalyst_desk": 2, "early_turn_desk": 3, "momentum_desk": 1, "quality_repair_desk": 2},
    "high_volatility": {"theme_catalyst_desk": 2, "early_turn_desk": 2, "momentum_desk": 1, "quality_repair_desk": 3},
    "risk_off":        {"theme_catalyst_desk": 2, "early_turn_desk": 2, "momentum_desk": 0, "quality_repair_desk": 4},
    "panic":           {"theme_catalyst_desk": 2, "early_turn_desk": 2, "momentum_desk": 0, "quality_repair_desk": 4},
    "trending_down":   {"theme_catalyst_desk": 2, "early_turn_desk": 3, "momentum_desk": 0, "quality_repair_desk": 3},
    "event_driven":    {"theme_catalyst_desk": 4, "early_turn_desk": 1, "momentum_desk": 2, "quality_repair_desk": 1},
    "unknown":         {"theme_catalyst_desk": 2, "early_turn_desk": 2, "momentum_desk": 2, "quality_repair_desk": 2},
}

# Default backfill rules: regime → {donor_desk: [recipient_desks]}
_DEFAULT_BACKFILL_RULES: Dict[str, Dict[str, List[str]]] = {
    "risk_off":       {"momentum_desk": ["quality_repair_desk"],
                       "early_turn_desk": ["quality_repair_desk", "theme_catalyst_desk"]},
    "panic":          {"momentum_desk": ["quality_repair_desk"],
                       "early_turn_desk": ["quality_repair_desk", "theme_catalyst_desk"]},
    "trending_down":  {"momentum_desk": ["quality_repair_desk"],
                       "early_turn_desk": ["quality_repair_desk", "theme_catalyst_desk"]},
    "trending_up":    {"early_turn_desk": ["momentum_desk"],
                       "quality_repair_desk": ["momentum_desk", "theme_catalyst_desk"]},
}


# ---------------------------------------------------------------------------
# aggregate_desk_picks
# ---------------------------------------------------------------------------

def aggregate_desk_picks(
    packets: Sequence[ExpertPacketV2],
    rows: Sequence[FeatureRow],
) -> AggregatedPool:
    """Merge per-desk ExpertPacketV2 outputs into a single AggregatedPool.

    Steps:
      1. Build code → FactSheet index from rows.
      2. For each desk packet, parse candidates with stance support/watch.
      3. Merge candidates with the same code across desks.
      4. Compute tool-coverage confidence for each candidate.
      5. Detect conflict flags.
      6. Mark multi_desk_conviction (same code picked by ≥2 desks, no oppose).
      7. Determine primary_desk per candidate.
      8. Bucket into by_desk lists (sorted) + observe pool.
    """
    diagnostics: List[Dict[str, Any]] = []

    # Build FactSheet index
    fs_index: Dict[str, FactSheet] = {}
    for row in rows:
        if row.fact_sheet is not None:
            fs_index[row.code] = row.fact_sheet

    # Collect active desks (those with ok/partial status and at least one candidate)
    desk_candidates: Dict[str, List[Any]] = {}  # desk_key → ExpertCandidateV2 list
    vetoed: List[Dict[str, Any]] = []

    for packet in packets:
        desk_key = str(packet.expert)  # expert_name = desk key
        picks = [
            c for c in packet.candidates
            if str(c.stance) in {"support", "watch"}
        ]
        desk_candidates[desk_key] = picks
        diag: Dict[str, Any] = {
            "desk": desk_key,
            "status": packet.status,
            "picks": len(picks),
            "rejected": len(packet.rejected),
        }
        # Surface the real failure reason instead of silently dropping it —
        # otherwise a failed/timeout desk degrades the whole pipeline with no
        # visible cause, forcing slow blind re-runs.
        errors = [str(e) for e in (packet.errors or []) if e]
        if errors:
            diag["errors"] = errors
        warnings = []
        dq = getattr(packet, "data_quality", None)
        if dq is not None:
            warnings = [str(w) for w in (getattr(dq, "warnings", None) or []) if w]
        if warnings:
            diag["warnings"] = warnings
        diagnostics.append(diag)

    # Merge by code
    all_codes: Set[str] = {str(c.code) for picks in desk_candidates.values() for c in picks}
    merged: Dict[str, AggregatedCandidate] = {}

    for code in all_codes:
        desks_picking: List[str] = []
        stance_by_desk: Dict[str, str] = {}
        evidence_by_desk: Dict[str, List[EvidenceItem]] = {}
        risks: List[RiskNote] = []
        best_reason = ""

        for desk_key, picks in desk_candidates.items():
            for pick in picks:
                if pick.code != code:
                    continue
                desks_picking.append(desk_key)
                stance_by_desk[desk_key] = str(pick.stance)
                evidence_by_desk[desk_key] = list(pick.evidence or [])
                risks.extend(pick.risks or [])
                if not best_reason or pick.stance == "support":
                    best_reason = pick.reason or ""
                break

        if not desks_picking:
            continue

        # Determine primary desk
        primary_desk = _select_primary_desk(desks_picking, stance_by_desk, {})

        # Get setup_type from primary desk pick
        setup_type = "unknown"
        for pick in desk_candidates.get(primary_desk, []):
            if pick.code == code:
                setup_type = str(pick.setup_type or "unknown")
                break

        # Get name from any pick
        name = ""
        for picks in desk_candidates.values():
            for pick in picks:
                if pick.code == code:
                    name = pick.name or ""
                    break
            if name:
                break

        fs = fs_index.get(code)
        all_evidence = [ev for evs in evidence_by_desk.values() for ev in evs]
        confidence = _compute_confidence(primary_desk, all_evidence)
        conflict_flags = _detect_conflicts(code, primary_desk, stance_by_desk, fs, setup_type)
        multi_conviction = (
            len(desks_picking) >= 2
            and all(str(s) in {"support", "watch"} for s in stance_by_desk.values())
        )

        merged[code] = AggregatedCandidate(
            code=code,
            name=name,
            market="cn",
            setup_type=setup_type,  # type: ignore[arg-type]
            desks=desks_picking,
            primary_desk=primary_desk,
            stance_by_desk=stance_by_desk,
            reason=best_reason,
            confidence=confidence,
            multi_desk_conviction=multi_conviction,
            conflict_flags=conflict_flags,
            fact_sheet=fs,
            evidence_by_desk=evidence_by_desk,
            risks=_dedup_risks(risks),
            observe_only=False,
        )

    # All picked codes → bucket by primary_desk
    by_desk: Dict[str, List[AggregatedCandidate]] = {}
    for cand in merged.values():
        by_desk.setdefault(cand.primary_desk, []).append(cand)

    # Sort each desk: multi_conviction DESC, confidence DESC
    for desk_list in by_desk.values():
        desk_list.sort(key=lambda c: (c.multi_desk_conviction, c.confidence), reverse=True)

    # Observe pool: rows not picked by any desk
    picked_codes = set(merged.keys())
    observe: List[AggregatedCandidate] = [
        AggregatedCandidate(
            code=row.code,
            name=row.name,
            market=row.market,
            confidence=0.0,
            observe_only=True,
        )
        for row in rows
        if row.code not in picked_codes
    ]

    return AggregatedPool(
        by_desk=by_desk,
        vetoed=vetoed,
        observe=observe,
        regime="unknown",
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# allocate_slots
# ---------------------------------------------------------------------------

def allocate_slots(
    pool: AggregatedPool,
    regime: str,
    *,
    total: int = 8,
    allocation_json: Optional[str] = None,
    backfill_rules_json: Optional[str] = None,
    backfill_max: int = 3,
    pick_top_n: int = 5,
) -> List[AggregatedCandidate]:
    """Assign per-desk slot quotas based on regime, then fill and backfill.

    Returns a flat list of up to `total` AggregatedCandidates ordered by
    primary_desk then (multi_desk_conviction DESC, confidence DESC).
    """
    regime = str(regime or "unknown")
    allocation = _parse_allocation(allocation_json, regime)
    backfill_rules = _parse_backfill_rules(backfill_rules_json)

    # Regime guard: defensive regimes must not allow momentum
    momentum_forbidden = regime in {"risk_off", "panic", "trending_down"}
    if momentum_forbidden:
        allocation["momentum_desk"] = 0

    result: List[AggregatedCandidate] = []
    empty_slots: Dict[str, int] = {}

    for desk_key, quota in allocation.items():
        if quota <= 0:
            empty_slots[desk_key] = 0
            continue
        candidates = pool.by_desk.get(desk_key, [])
        # Multi-conviction picks take priority within desk
        mc = [c for c in candidates if c.multi_desk_conviction]
        rest = [c for c in candidates if not c.multi_desk_conviction]
        ordered = mc + rest
        taken = ordered[:min(quota, pick_top_n)]
        result.extend(taken)
        gap = quota - len(taken)
        if gap > 0:
            empty_slots[desk_key] = gap

    # Backfill empty slots
    if empty_slots:
        rules = backfill_rules.get(regime, {})
        for donor, recipients in rules.items():
            gap = empty_slots.get(donor, 0)
            if gap <= 0:
                continue
            fill_count = min(gap, backfill_max)
            per_recipient = max(1, fill_count // max(1, len(recipients)))
            for rec in recipients:
                if momentum_forbidden and rec == "momentum_desk":
                    continue
                # Take candidates already in result to avoid duplication
                already_codes = {str(c.code) for c in result}
                extras = [
                    c for c in pool.by_desk.get(rec, [])
                    if str(c.code) not in already_codes
                ][:per_recipient]
                result.extend(extras)

    # Deduplicate (multi-conviction picked under multiple desks)
    seen: Set[str] = set()
    deduped: List[AggregatedCandidate] = []
    for c in result:
        code = str(c.code)
        if code not in seen:
            seen.add(code)
            deduped.append(c)

    return deduped[:total]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_confidence(desk_key: str, evidence: Sequence[EvidenceItem]) -> float:
    dim_map = _DESK_DIM_TOOL_MAP.get(desk_key, {})
    if not dim_map:
        # fallback: use generic tool count heuristic
        n = len({str(ev.tool) for ev in evidence if ev.tool})
        return round(min(0.9, 0.4 + n * 0.1), 2)

    tools_used = {str(ev.tool) for ev in evidence if ev.tool and ev.summary}
    covered_dims = sum(
        1 for tools in dim_map.values()
        if tools & tools_used
    )
    expected = len(dim_map)
    return round(covered_dims / expected, 2) if expected else 0.5


def _detect_conflicts(
    code: str,
    primary_desk: str,
    stance_by_desk: Dict[str, str],
    fs: Optional[FactSheet],
    setup_type: str,
) -> List[str]:
    flags: List[str] = []
    if fs is None:
        return flags

    if primary_desk == "momentum_desk" and setup_type == "capital_momentum":
        if fs.capital_direction == "outflow":
            flags.append("capital_thesis_mismatch")

    if primary_desk == "early_turn_desk":
        r = fs.range_pct_120
        if r is not None and r > _HIGH_RANGE_PCT_THRESHOLD:
            flags.append("thesis_position_mismatch")

    if primary_desk in {"momentum_desk"} and setup_type in {"trend_continuation", "capital_momentum"}:
        if fs.trend_state == "bearish":
            flags.append("trend_thesis_mismatch")

    if primary_desk == "quality_repair_desk":
        g = fs.gain_5d
        if g is not None and g >= _HIGH_GAIN_THRESHOLD:
            flags.append("quality_price_mismatch")

    if primary_desk == "theme_catalyst_desk":
        if fs.capital_direction == "outflow":
            flags.append("theme_without_capital_validation")
        g = fs.gain_5d
        if g is not None and g >= _HIGH_GAIN_THRESHOLD:
            flags.append("theme_chase_risk")

    return flags


def _select_primary_desk(
    desks: List[str],
    stance_by_desk: Dict[str, str],
    confidence_by_desk: Dict[str, float],
) -> str:
    if not desks:
        return "unknown"
    if len(desks) == 1:
        return desks[0]

    def sort_key(d: str) -> tuple:
        stance_val = _STANCE_STRENGTH.get(stance_by_desk.get(d, "watch"), 0)
        conf = confidence_by_desk.get(d, 0.5)
        prio = -_DESK_PRIORITY.get(d, 99)  # lower number = higher priority → negate
        return (stance_val, conf, prio)

    return max(desks, key=sort_key)


def _dedup_risks(risks: List[RiskNote]) -> List[RiskNote]:
    seen: Set[str] = set()
    out: List[RiskNote] = []
    for r in risks:
        key = f"{str(r.type)}:{str(r.summary)}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _parse_allocation(allocation_json: Optional[str], regime: str) -> Dict[str, int]:
    regime = str(regime or "unknown")
    if allocation_json:
        try:
            data = json.loads(allocation_json)
            if isinstance(data, dict):
                row = data.get(regime) or data.get("unknown")
                if isinstance(row, dict):
                    return {str(k): int(v) for k, v in row.items()}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("DESK_SLOT_ALLOCATION_JSON parse error: %s — using default", exc)
    row = _DEFAULT_ALLOCATION.get(regime) or _DEFAULT_ALLOCATION["unknown"]
    return dict(row)


def _parse_backfill_rules(rules_json: Optional[str]) -> Dict[str, Dict[str, List[str]]]:
    if rules_json:
        try:
            data = json.loads(rules_json)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("DESK_BACKFILL_RULES_JSON parse error: %s — using default", exc)
    return dict(_DEFAULT_BACKFILL_RULES)
