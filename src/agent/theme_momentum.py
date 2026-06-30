# -*- coding: utf-8 -*-
"""Deterministic theme-momentum regime helpers.

The module keeps mainline momentum detection auditable and source-agnostic.
It deliberately treats missing provider data as low confidence, not as a
negative market signal.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


THEME_UNIVERSES: Dict[str, Dict[str, Any]] = {
    "ai_compute_chain": {
        "display_name": "AI 算力产业链",
        "aliases": [
            "AI",
            "人工智能",
            "算力",
            "光模块",
            "CPO",
            "光芯片",
            "PCB",
            "先进封装",
            "封装封测",
            "玻璃基板",
            "树脂",
            "光纤",
            "存储",
            "HBM",
            "铜缆",
            "液冷",
            "服务器",
            "数据中心",
        ],
    }
}


CORE_ROLES = {"core_leader", "core_midcap", "high_beta_leader"}


@dataclass(frozen=True)
class _MatchedText:
    text: str
    aliases: List[str]


def build_theme_momentum_snapshot(
    *,
    theme: str = "ai_compute_chain",
    hot_sectors: Optional[Dict[str, Any]] = None,
    sector_rankings: Optional[Dict[str, Any]] = None,
    limit_up_pool: Optional[Dict[str, Any]] = None,
    hot_sector_leaders: Optional[Dict[str, Any]] = None,
    popularity_rank: Optional[Dict[str, Any]] = None,
    board_capital_flow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact theme-regime snapshot from available provider payloads."""

    theme_cfg = THEME_UNIVERSES.get(theme, THEME_UNIVERSES["ai_compute_chain"])
    aliases = [str(item) for item in theme_cfg.get("aliases") or []]
    source_status: Dict[str, str] = {}
    evidence: List[str] = []
    conflicts: List[str] = []

    hot_sector_rows = _rows(hot_sectors, "sectors", "items")
    sector_rank_rows = _sector_ranking_rows(sector_rankings)
    limit_rows = _rows(limit_up_pool, "items")
    leader_rows = _rows(hot_sector_leaders, "items")
    hot_rank_rows = _rows(popularity_rank, "items")
    board_flow_rows = _board_flow_rows(board_capital_flow)

    source_status["hot_sectors"] = _payload_status(hot_sectors, hot_sector_rows)
    source_status["sector_rankings"] = _payload_status(sector_rankings, sector_rank_rows)
    source_status["limit_up_pool"] = _payload_status(limit_up_pool, limit_rows)
    source_status["hot_sector_leaders"] = _payload_status(hot_sector_leaders, leader_rows)
    source_status["popularity_rank"] = _payload_status(popularity_rank, hot_rank_rows)
    source_status["board_capital_flow"] = _payload_status(board_capital_flow, board_flow_rows)

    matched_sectors = _matched_rows([*hot_sector_rows, *sector_rank_rows, *board_flow_rows], aliases)
    matched_limits = _matched_rows(limit_rows, aliases)
    matched_leaders = _matched_rows([*leader_rows, *hot_rank_rows], aliases)

    rank_score = _rank_score(matched_sectors)
    capital_score = _capital_score([row for row, _match in matched_sectors])
    limit_score, limit_stats = _limit_up_score([row for row, _match in matched_limits])
    leader_score = _leader_score([row for row, _match in matched_leaders], [row for row, _match in matched_limits])
    breadth_score = max(limit_score * 0.75, min(1.0, len(matched_sectors) / 4.0))
    exhaustion_score = _exhaustion_score([row for row, _match in matched_limits], [row for row, _match in matched_leaders])

    if matched_sectors:
        names = [(_name(row) or _code(row)) for row, _match in matched_sectors[:4]]
        evidence.append(f"主题相关板块/资金榜命中 {len(matched_sectors)} 项：{', '.join(name for name in names if name)}。")
    if matched_limits:
        names = [(_name(row) or _code(row)) for row, _match in matched_limits[:5]]
        evidence.append(f"主题相关涨停/连板命中 {len(matched_limits)} 项：{', '.join(name for name in names if name)}。")
    if matched_leaders:
        names = [(_name(row) or _code(row)) for row, _match in matched_leaders[:5]]
        evidence.append(f"主题相关热榜/龙头命中 {len(matched_leaders)} 项：{', '.join(name for name in names if name)}。")
    if limit_stats["bomb_count"] > 0:
        conflicts.append(f"主题涨停样本中开板/炸板 {limit_stats['bomb_count']} 项，追高需降级为条件确认。")

    scores = {
        "rank_persistence": round(rank_score, 4),
        "capital_flow": round(capital_score, 4),
        "limit_up_breadth": round(limit_score, 4),
        "leader_strength": round(leader_score, 4),
        "internal_breadth": round(breadth_score, 4),
        "exhaustion_risk": round(exhaustion_score, 4),
    }
    source_count = sum(1 for status in source_status.values() if status in {"ok", "partial"})
    evidence_count = len(matched_sectors) + len(matched_limits) + len(matched_leaders)
    data_quality = "sufficient" if source_count >= 3 and evidence_count >= 5 else "limited" if source_count >= 1 else "insufficient"
    confidence = _clamp((source_count / 6.0) * 0.35 + min(1.0, evidence_count / 8.0) * 0.65)
    combined_strength = (
        rank_score * 0.22
        + capital_score * 0.20
        + limit_score * 0.20
        + leader_score * 0.25
        + breadth_score * 0.13
    )
    regime = _classify_theme_regime(
        combined_strength=combined_strength,
        exhaustion_score=exhaustion_score,
        rank_score=rank_score,
        capital_score=capital_score,
        leader_score=leader_score,
        evidence_count=evidence_count,
        data_quality=data_quality,
    )
    if regime == "unknown" and not evidence:
        evidence.append("主题动量证据不足，不能把 AI 产业链默认为主线。")
    elif regime == "mainline_markup":
        evidence.append("主题强度、涨停宽度或龙头热度共同支持主线主升。")
    elif regime == "climax_extension":
        conflicts.append("主题强度与开板/拥挤风险并存，按高潮延伸处理。")
    elif regime == "theme_risk_off":
        conflicts.append("主题证据明显走弱或缺少核心承接，主动追高应降级。")

    return {
        "theme": theme,
        "theme_name": str(theme_cfg.get("display_name") or theme),
        "regime": regime,
        "confidence": round(confidence, 4),
        "data_quality": data_quality,
        "scores": scores,
        "source_status": source_status,
        "matched_counts": {
            "sectors": len(matched_sectors),
            "limit_up": len(matched_limits),
            "leaders": len(matched_leaders),
            "total": evidence_count,
        },
        "limit_up_stats": limit_stats,
        "evidence": evidence,
        "conflicts": conflicts,
        "aliases": aliases,
    }


def classify_seed_theme_profile(seed: Any, snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify one seed's role in the current theme snapshot."""

    snapshot = snapshot if isinstance(snapshot, dict) else {}
    theme = str(snapshot.get("theme") or "ai_compute_chain")
    aliases = [str(item) for item in (snapshot.get("aliases") or THEME_UNIVERSES["ai_compute_chain"]["aliases"])]
    regime = str(snapshot.get("regime") or "unknown")
    metrics = dict(getattr(seed, "extras", {}) or {}).get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}

    text = _seed_text(seed, metrics)
    matched = _match_aliases(text, aliases)
    source = str(getattr(seed, "source", "") or "")
    rank = _to_float(metrics.get("rank"))
    bomb_num = _to_float(metrics.get("bomb_num") or metrics.get("open_times"))
    limit_streak = _to_float(metrics.get("limit_up_streak"))
    net_inflow = _to_float(metrics.get("net_inflow"))
    strength = _to_float(metrics.get("strength"))
    priority = _to_float(getattr(seed, "priority_score", None)) or 0.0
    source_label = str(metrics.get("source_label") or "")

    membership_confidence = 0.0
    if matched.aliases:
        membership_confidence += 0.45
    if source in {"sector_theme", "news_theme_daily"}:
        membership_confidence += 0.25
    if source in {"hot_rank", "limit_up_pool", "capital_flow_anomaly", "dragon_tiger"} and matched.aliases:
        membership_confidence += 0.15
    if rank is not None and rank <= 5:
        membership_confidence += 0.10
    if net_inflow is not None and net_inflow > 0:
        membership_confidence += 0.05
    membership_confidence = _clamp(membership_confidence)

    role = "unrelated" if membership_confidence < 0.25 else "follower"
    if membership_confidence >= 0.25:
        if source == "sector_theme" and "leader" in source_label:
            role = "core_leader" if (rank is None or rank <= 3) else "core_midcap"
        elif source == "sector_theme":
            role = "core_midcap" if (rank is None or rank <= 6 or (net_inflow or 0) > 0) else "follower"
        elif source == "limit_up_pool":
            if (limit_streak or 0) >= 2 or priority >= 84:
                role = "high_beta_leader"
            else:
                role = "late_chaser" if regime == "climax_extension" else "follower"
        elif source == "hot_rank":
            role = "high_beta_leader" if rank is not None and rank <= 5 else "follower"
        elif source in {"capital_flow_anomaly", "dragon_tiger"}:
            role = "core_midcap" if (net_inflow or 0) > 0 and priority >= 76 else "follower"
        elif source == "news_theme_daily":
            role = "follower"

    if (bomb_num or 0) >= 2 or (regime == "climax_extension" and role == "follower"):
        role = "exhaustion_candidate" if (bomb_num or 0) >= 2 else "late_chaser"
    if regime == "theme_risk_off" and role not in {"unrelated", "unknown"}:
        role = "exhaustion_candidate"

    setup = _momentum_setup(source=source, role=role, regime=regime, bomb_num=bomb_num, limit_streak=limit_streak)
    interpretation, permission = _overbought_policy(regime, role)
    risk_flags: List[str] = []
    if role in {"late_chaser", "exhaustion_candidate"}:
        risk_flags.append("主题或个股处于高潮/衰竭候选，超买不能豁免。")
    if regime == "theme_risk_off":
        risk_flags.append("主题退潮，主动开仓应降级。")
    if (bomb_num or 0) > 0:
        risk_flags.append(f"涨停/强势样本存在开板次数 {int(bomb_num or 0)}。")
    if not matched.aliases and source in {"hot_rank", "limit_up_pool"}:
        risk_flags.append("热度来源未匹配主题别名，不能当作主线核心证据。")

    return {
        "theme": theme,
        "theme_name": snapshot.get("theme_name") or THEME_UNIVERSES["ai_compute_chain"]["display_name"],
        "theme_regime": regime,
        "theme_confidence": snapshot.get("confidence"),
        "theme_data_quality": snapshot.get("data_quality"),
        "theme_membership": [
            {
                "theme": theme,
                "matched_aliases": matched.aliases,
                "membership_confidence": round(membership_confidence, 4),
            }
        ],
        "stock_role": role,
        "momentum_setup": setup,
        "overbought_interpretation": interpretation,
        "chase_permission": permission,
        "evidence": _profile_evidence(seed, matched, role, setup, strength=strength, net_inflow=net_inflow),
        "risk_flags": risk_flags,
    }


def apply_theme_profile_to_seed(seed: Any, snapshot: Optional[Dict[str, Any]]) -> Any:
    """Attach theme profile to a mutable/pydantic SeedItem-like object."""

    profile = classify_seed_theme_profile(seed, snapshot)
    extras = getattr(seed, "extras", None)
    if not isinstance(extras, dict):
        extras = {}
        seed.extras = extras
    extras["theme_profile"] = profile
    metrics = extras.setdefault("metrics", {})
    if isinstance(metrics, dict):
        metrics.update(
            {
                "theme_regime": profile.get("theme_regime"),
                "stock_role": profile.get("stock_role"),
                "momentum_setup": profile.get("momentum_setup"),
                "overbought_interpretation": profile.get("overbought_interpretation"),
                "chase_permission": profile.get("chase_permission"),
            }
        )
    if profile.get("stock_role") not in {"unrelated", "unknown"}:
        signals = list(getattr(seed, "trigger_signals", None) or [])
        signals.append(
            {
                "dimension": "theme_regime",
                "signal_type": "mainline_momentum_profile",
                "value": {
                    "theme_regime": profile.get("theme_regime"),
                    "stock_role": profile.get("stock_role"),
                    "momentum_setup": profile.get("momentum_setup"),
                },
                "threshold": "auditable_theme_profile",
                "deviation": profile.get("theme_confidence"),
                "label": "主线动量分型",
            }
        )
        seed.trigger_signals = signals
    return seed


def build_single_stock_theme_profile(
    *,
    symbol: str,
    name: str = "",
    theme: str = "ai_compute_chain",
    snapshot: Optional[Dict[str, Any]] = None,
    hot_sectors: Optional[Dict[str, Any]] = None,
    sector_rankings: Optional[Dict[str, Any]] = None,
    limit_up_pool: Optional[Dict[str, Any]] = None,
    hot_sector_leaders: Optional[Dict[str, Any]] = None,
    popularity_rank: Optional[Dict[str, Any]] = None,
    board_capital_flow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a single-symbol theme profile using the same rules as seed pools.

    Single-stock analysis must not infer market membership from a whole
    candidate pool.  This helper only upgrades a symbol when that exact symbol
    appears in stock-level theme payloads, or when user-provided text directly
    matches a theme alias.
    """

    snapshot = (
        snapshot
        if isinstance(snapshot, dict)
        else build_theme_momentum_snapshot(
            theme=theme,
            hot_sectors=hot_sectors,
            sector_rankings=sector_rankings,
            limit_up_pool=limit_up_pool,
            hot_sector_leaders=hot_sector_leaders,
            popularity_rank=popularity_rank,
            board_capital_flow=board_capital_flow,
        )
    )
    normalized_symbol = _normalize_stock_code(symbol)
    limit_matches = _symbol_rows(_rows(limit_up_pool, "items"), normalized_symbol)
    leader_matches = _symbol_rows(_rows(hot_sector_leaders, "items"), normalized_symbol)
    rank_matches = _symbol_rows(_rows(popularity_rank, "items"), normalized_symbol)

    source = "user_watchlist"
    source_label = "single_symbol_context"
    priority = 50.0
    if leader_matches:
        source = "sector_theme"
        source_label = "stockapi_hot_sector_leader"
        priority = _priority_from_row(leader_matches[0], base=88.0)
    elif limit_matches:
        source = "limit_up_pool"
        source_label = "stockapi_limit_up_pool"
        priority = _priority_from_row(limit_matches[0], base=84.0)
    elif rank_matches:
        source = "hot_rank"
        source_label = "stockapi_popularity_rank"
        priority = _priority_from_row(rank_matches[0], base=78.0)

    matched_rows = {
        "hot_sector_leaders": [_compact_row_for_profile(row) for row in leader_matches[:3]],
        "limit_up_pool": [_compact_row_for_profile(row) for row in limit_matches[:3]],
        "popularity_rank": [_compact_row_for_profile(row) for row in rank_matches[:3]],
    }
    metrics: Dict[str, Any] = {"source_label": source_label}
    for label, rows in matched_rows.items():
        if not rows:
            continue
        row = rows[0]
        metrics[f"{label}_matched"] = True
        for key, value in row.items():
            metrics[f"{label}_{key}"] = value
            metrics.setdefault(key, value)

    resolved_name = (name or "").strip()
    for row in [*(leader_matches[:1]), *(limit_matches[:1]), *(rank_matches[:1])]:
        resolved_name = resolved_name or _name(row)
    hint_parts = []
    for row in [*leader_matches[:2], *limit_matches[:2], *rank_matches[:2]]:
        text = _row_text(row)
        if text:
            hint_parts.append(text)
    hint = " ".join(hint_parts[:6])

    seed = SimpleNamespace(
        code=symbol,
        name=resolved_name,
        source=source,
        hint=hint,
        context_hint="",
        priority_score=priority,
        trigger_signals=[],
        extras={"metrics": metrics},
    )
    profile = classify_seed_theme_profile(seed, snapshot)

    matched_counts = {key: len(value) for key, value in matched_rows.items()}
    stock_evidence_count = sum(matched_counts.values())
    status = "ok" if stock_evidence_count > 0 else "limited"
    if snapshot.get("data_quality") == "insufficient" and stock_evidence_count == 0:
        status = "insufficient"

    return {
        "status": status,
        "symbol": symbol,
        "name": resolved_name,
        "theme_momentum": snapshot,
        "profile": profile,
        "matched_sources": matched_counts,
        "matched_rows": matched_rows,
    }


def _classify_theme_regime(
    *,
    combined_strength: float,
    exhaustion_score: float,
    rank_score: float,
    capital_score: float,
    leader_score: float,
    evidence_count: int,
    data_quality: str,
) -> str:
    if evidence_count <= 0 or data_quality == "insufficient":
        return "unknown"
    if combined_strength >= 0.56 and exhaustion_score >= 0.58:
        return "climax_extension"
    if combined_strength >= 0.62 and exhaustion_score < 0.58:
        return "mainline_markup"
    if combined_strength >= 0.45 and leader_score >= 0.35:
        return "mainline_divergence"
    if rank_score >= 0.30 and capital_score < 0.20:
        return "rotation_weakening"
    if combined_strength < 0.22 and evidence_count <= 2:
        return "theme_risk_off"
    return "range_rotation"


def _momentum_setup(*, source: str, role: str, regime: str, bomb_num: Optional[float], limit_streak: Optional[float]) -> str:
    if role in {"unrelated", "unknown"}:
        return "unknown"
    if regime == "theme_risk_off" or role == "exhaustion_candidate" or (bomb_num or 0) >= 2:
        return "fakeout_exhaustion"
    if source == "limit_up_pool" or (limit_streak or 0) >= 1:
        return "limit_up_continuation"
    if regime == "mainline_markup" and role in CORE_ROLES:
        return "breakout_confirmation"
    if regime == "mainline_divergence":
        return "divergence_to_consensus"
    if regime == "climax_extension":
        return "fakeout_exhaustion"
    return "mean_reversion_only" if role == "follower" else "breakout_confirmation"


def _overbought_policy(regime: str, role: str) -> Tuple[str, str]:
    if regime == "theme_risk_off":
        return "risk_signal", "blocked"
    if regime == "mainline_markup" and role in CORE_ROLES:
        return "strength_requires_confirmation", "conditional_only"
    if regime == "mainline_divergence" and role in CORE_ROLES:
        return "strength_but_divergent", "conditional_only"
    if regime == "climax_extension":
        return "exhaustion_risk", "strong_watch_only"
    if role in {"late_chaser", "exhaustion_candidate"}:
        return "exhaustion_risk", "blocked"
    if role == "unrelated":
        return "not_theme_evidence", "none"
    return "neutral_requires_price_plan", "conditional_only"


def _rows(payload: Optional[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _sector_ranking_rows(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for key in ("top", "top_sectors", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("top", "top_sectors", "items"):
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
    return rows


def _board_flow_rows(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = _rows(payload, "items", "sectors", "top")
    flow_sources = payload.get("flow_sources")
    if isinstance(flow_sources, list):
        for source in flow_sources:
            if isinstance(source, dict):
                rows.extend(_rows(source, "items", "sectors", "top"))
    return rows


def _payload_status(payload: Optional[Dict[str, Any]], rows: Sequence[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return "missing"
    status = str(payload.get("status") or "").strip().lower()
    if rows:
        return "ok" if status in {"", "ok", "partial"} else status
    return status or "empty"


def _matched_rows(rows: Iterable[Dict[str, Any]], aliases: Sequence[str]) -> List[Tuple[Dict[str, Any], _MatchedText]]:
    matched: List[Tuple[Dict[str, Any], _MatchedText]] = []
    for row in rows:
        text = _row_text(row)
        match = _match_aliases(text, aliases)
        if match.aliases:
            matched.append((row, match))
    return matched


def _match_aliases(text: str, aliases: Sequence[str]) -> _MatchedText:
    normalized = _normalize_text(text)
    found: List[str] = []
    for alias in aliases:
        item = str(alias or "").strip()
        if not item:
            continue
        if _normalize_text(item) in normalized:
            found.append(item)
    return _MatchedText(text=text, aliases=found[:8])


def _row_text(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in (
        "bk_name",
        "bkName",
        "plate_name",
        "industry",
        "concepts",
        "concept",
        "stock_reason",
        "plate_reason",
        "reason",
        "rank_reason",
        "name",
        "ts_name",
        "hint",
    ):
        value = row.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


def _seed_text(seed: Any, metrics: Dict[str, Any]) -> str:
    parts = [
        str(getattr(seed, "name", "") or ""),
        str(getattr(seed, "hint", "") or ""),
        str(getattr(seed, "context_hint", "") or ""),
        _row_text(metrics),
    ]
    for signal in getattr(seed, "trigger_signals", []) or []:
        if not isinstance(signal, dict):
            continue
        parts.append(str(signal.get("label") or ""))
        parts.append(str(signal.get("value") or ""))
    return " ".join(parts)


def _profile_evidence(seed: Any, match: _MatchedText, role: str, setup: str, *, strength: Optional[float], net_inflow: Optional[float]) -> List[str]:
    evidence = [f"来源={getattr(seed, 'source', '')}，角色={role}，setup={setup}。"]
    if match.aliases:
        evidence.append(f"主题别名命中：{', '.join(match.aliases)}。")
    if strength is not None:
        evidence.append(f"主题/板块强度={strength:.2f}。")
    if net_inflow is not None:
        evidence.append(f"资金净流入={net_inflow:.0f}。")
    return evidence


def _rank_score(matches: Sequence[Tuple[Dict[str, Any], _MatchedText]]) -> float:
    if not matches:
        return 0.0
    scores = []
    for row, _match in matches:
        rank = _to_float(row.get("rank"))
        pct = _to_float(row.get("return_pct") or row.get("change_pct") or row.get("pct_change"))
        item_score = 0.35
        if rank is not None:
            item_score = max(item_score, 1.0 - min(rank, 30.0) / 30.0)
        if pct is not None and pct > 0:
            item_score += min(0.25, pct / 20.0)
        scores.append(_clamp(item_score))
    return _clamp(sum(scores[:6]) / max(1, min(6, len(scores))) + min(0.15, len(matches) * 0.025))


def _capital_score(rows: Sequence[Dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    positive = 0
    score = 0.0
    for row in rows[:8]:
        inflow = _to_float(row.get("net_inflow") or row.get("main_net_inflow") or row.get("qjje"))
        strength = _to_float(row.get("strength"))
        inflow_days = _to_float(row.get("inflow_days"))
        if inflow is not None and inflow > 0:
            positive += 1
            score += min(0.45, inflow / 1_000_000_000.0)
        if strength is not None and strength > 0:
            score += min(0.25, strength / 100.0)
        if inflow_days is not None and inflow_days > 0:
            score += min(0.15, inflow_days / 20.0)
    if positive:
        score += min(0.20, positive * 0.04)
    return _clamp(score / max(1, min(4, len(rows))))


def _limit_up_score(rows: Sequence[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    if not rows:
        return 0.0, {"matched_count": 0, "bomb_count": 0, "max_streak": 0}
    bomb_count = 0
    max_streak = 0.0
    sealed_count = 0
    for row in rows:
        bomb = _to_float(row.get("bomb_num") or row.get("open_times") or row.get("open_num")) or 0.0
        streak = _to_float(row.get("limit_up_streak") or row.get("limit_times") or row.get("lbNum")) or 1.0
        max_streak = max(max_streak, streak)
        if bomb > 0:
            bomb_count += 1
        if str(row.get("limit_status") or row.get("limit_type") or "U").upper() != "D":
            sealed_count += 1
    breadth = min(1.0, len(rows) / 8.0)
    streak_score = min(0.35, max_streak / 10.0)
    quality_penalty = min(0.35, bomb_count / max(1, len(rows)) * 0.45)
    score = _clamp(breadth * 0.75 + streak_score - quality_penalty)
    return score, {"matched_count": len(rows), "bomb_count": bomb_count, "max_streak": int(max_streak), "sealed_count": sealed_count}


def _leader_score(leader_rows: Sequence[Dict[str, Any]], limit_rows: Sequence[Dict[str, Any]]) -> float:
    rows = list(leader_rows[:8])
    if not rows and limit_rows:
        rows = list(limit_rows[:5])
    if not rows:
        return 0.0
    score = 0.0
    for row in rows:
        rank = _to_float(row.get("rank"))
        pct = _to_float(row.get("change_ratio") or row.get("pct_change") or row.get("return_pct"))
        inflow = _to_float(row.get("net_inflow") or row.get("main_net_inflow"))
        item = 0.35
        if rank is not None:
            item = max(item, 1.0 - min(rank, 20.0) / 22.0)
        if pct is not None and pct > 0:
            item += min(0.20, pct / 50.0)
        if inflow is not None and inflow > 0:
            item += min(0.20, inflow / 1_000_000_000.0)
        score += _clamp(item)
    return _clamp(score / len(rows) + min(0.12, len(rows) * 0.02))


def _exhaustion_score(limit_rows: Sequence[Dict[str, Any]], leader_rows: Sequence[Dict[str, Any]]) -> float:
    rows = [*limit_rows, *leader_rows]
    if not rows:
        return 0.0
    risk = 0.0
    for row in rows:
        bomb = _to_float(row.get("bomb_num") or row.get("open_times") or row.get("open_num")) or 0.0
        pct = _to_float(row.get("change_ratio") or row.get("pct_change") or row.get("return_pct")) or 0.0
        turnover = _to_float(row.get("turnover_ratio") or row.get("turnover_rate")) or 0.0
        text = _normalize_text(_row_text(row))
        item = 0.0
        if bomb >= 2:
            item += 0.45
        elif bomb > 0:
            item += 0.25
        if pct >= 9.0:
            item += 0.15
        if turnover >= 20.0:
            item += 0.20
        if any(term in text for term in ("炸板", "开板", "长上影", "放量滞涨")):
            item += 0.25
        risk += min(1.0, item)
    return _clamp(risk / len(rows) + min(0.20, len(rows) / 30.0))


def _name(row: Dict[str, Any]) -> str:
    return str(row.get("name") or row.get("stock_name") or row.get("ts_name") or row.get("bk_name") or row.get("bkName") or "").strip()


def _code(row: Dict[str, Any]) -> str:
    return str(row.get("code") or row.get("ts_code") or row.get("bk_code") or row.get("bkCode") or "").strip()


def _stock_code(row: Dict[str, Any]) -> str:
    return str(
        row.get("code")
        or row.get("stock_code")
        or row.get("symbol")
        or row.get("ts_code")
        or row.get("secucode")
        or ""
    ).strip()


def _normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        head, tail = text.split(".", 1)
        if tail in {"SH", "SZ", "BJ", "HK"}:
            text = head
    for prefix in ("SH", "SZ", "BJ", "HK"):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
    return re.sub(r"[^0-9A-Z]", "", text)


def _symbol_rows(rows: Iterable[Dict[str, Any]], normalized_symbol: str) -> List[Dict[str, Any]]:
    if not normalized_symbol:
        return []
    return [
        row
        for row in rows
        if _normalize_stock_code(_stock_code(row)) == normalized_symbol
    ]


def _priority_from_row(row: Dict[str, Any], *, base: float) -> float:
    rank = _to_float(row.get("rank"))
    streak = _to_float(row.get("limit_up_streak") or row.get("limit_times") or row.get("lbNum"))
    score = base
    if rank is not None:
        score += max(0.0, 8.0 - min(rank, 20.0) * 0.4)
    if streak is not None:
        score += min(6.0, streak * 2.0)
    return min(100.0, round(score, 2))


def _compact_row_for_profile(row: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "code",
        "stock_code",
        "symbol",
        "ts_code",
        "name",
        "stock_name",
        "ts_name",
        "bk_name",
        "bkName",
        "concept",
        "concepts",
        "rank",
        "return_pct",
        "change_pct",
        "pct_change",
        "change_ratio",
        "net_inflow",
        "main_net_inflow",
        "strength",
        "limit_up_streak",
        "limit_times",
        "lbNum",
        "bomb_num",
        "open_times",
        "turnover_ratio",
        "turnover_rate",
        "reason",
        "stock_reason",
        "rank_reason",
    )
    return {key: row[key] for key in keys if key in row and _is_profile_value(row[key])}


def _is_profile_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(isinstance(item, (str, int, float, bool)) for item in value[:20])
    return False


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").upper())


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
