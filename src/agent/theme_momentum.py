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
    # sector_theme 来源本身即"主题板块成分股",算主题证据;news_theme_daily
    # 只在日报主题/相关板块/关键词命中目标主题别名时才算,避免"来自新闻池"
    # 被误当成"贴靠主线主题"(例如航运、低空经济日报)。
    if source == "sector_theme":
        membership_confidence += 0.25
    elif source == "news_theme_daily" and matched.aliases:
        membership_confidence += 0.25
    if source in {"hot_rank", "limit_up_pool", "capital_flow_anomaly", "dragon_tiger"} and matched.aliases:
        membership_confidence += 0.15
    if rank is not None and rank <= 5:
        membership_confidence += 0.10
    if net_inflow is not None and net_inflow > 0:
        membership_confidence += 0.05
    membership_confidence = _clamp(membership_confidence)

    # 三态主题归属:matched / unrelated_confirmed / unclassified。
    # unclassified 只表示"证据不足",不能当成"与主题无关"。
    confirmation = _theme_confirmation_evidence(metrics)
    if matched.aliases:
        theme_status = "matched"
        # 命中别名时不保留"他主题"确认块,避免同时出现 matched 与 explicit_other_theme。
        confirmation = None
    elif confirmation is not None:
        theme_status = "unrelated_confirmed"
    else:
        theme_status = "unclassified"

    if theme_status == "matched":
        role = "follower"
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
    elif theme_status == "unrelated_confirmed":
        role = "unrelated"
    else:
        role = "unknown"

    if theme_status == "matched" and (
        (bomb_num or 0) >= 2 or (regime == "climax_extension" and role == "follower")
    ):
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
        "theme_status": theme_status,
        "theme_confirmation": confirmation or {},
        "stock_role": role,
        "momentum_setup": setup,
        "overbought_interpretation": interpretation,
        "chase_permission": permission,
        "evidence": _profile_evidence(
            seed,
            matched,
            role,
            setup,
            strength=strength,
            net_inflow=net_inflow,
            theme_status=theme_status,
            confirmation=confirmation,
        ),
        "risk_flags": risk_flags,
        # FAC-05: display-only provenance; does not participate in role/confidence.
        "theme_attribution": _build_theme_attribution(
            seed,
            aliases=aliases,
            theme=theme,
            theme_name=str(
                snapshot.get("theme_name")
                or THEME_UNIVERSES["ai_compute_chain"]["display_name"]
            ),
            regime=regime,
            metrics=metrics,
            matched=matched,
            membership_confidence=membership_confidence,
            role=role,
            setup=setup,
            theme_status=theme_status,
            confirmation=confirmation,
        ),
    }


def apply_theme_profile_to_seed(seed: Any, snapshot: Optional[Dict[str, Any]]) -> Any:
    """Attach theme profile to a mutable/pydantic SeedItem-like object."""

    profile = classify_seed_theme_profile(seed, snapshot)
    extras = getattr(seed, "extras", None)
    if not isinstance(extras, dict):
        extras = {}
        seed.extras = extras
    extras["theme_profile"] = profile
    # 显式字段:无论 stock_role 是 unrelated/unknown 都落盘,供快照回放与反证使用。
    if isinstance(seed, dict):
        seed["theme_profile"] = profile
    else:
        try:
            seed.theme_profile = profile
        except Exception:
            pass
    metrics = extras.setdefault("metrics", {})
    if isinstance(metrics, dict):
        metrics.update(
            {
                "theme_regime": profile.get("theme_regime"),
                "theme_status": profile.get("theme_status"),
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


# 主题归属三态里的"正证据"白名单/黑名单。
# 只有 exclusion_rule 或 explicit_other_theme 才能把"没查到主题"升级为
# unrelated_confirmed;其余一律 unclassified(证据不足),不能当成"与主题无关"。
_GENERIC_THEME_LABELS = {
    "",
    "其他",
    "其它",
    "综合",
    "综合类",
    "A股",
    "沪深A股",
    "全市场",
    "大盘",
    "市场",
    "个股",
    "股票",
    "热点",
    "异动",
    "强势",
    "涨停",
    "连板",
    "概念",
    "板块",
    "行业",
    # 交易属性/指数成分等非主题标签:不能作为"明确他主题"的正证据。
    "融资融券",
    "沪股通",
    "深股通",
    "港股通",
    "MSCI",
    "MSCI中国",
    "标准普尔",
    "富时罗素",
    "转债标的",
    "预盈预增",
    "预亏预减",
    "机构重仓",
    "基金重仓",
    "QFII重仓",
    "社保重仓",
    "券商重仓",
    "保险重仓",
    "养老金",
    "高送转",
    "次新股",
    "壳资源",
    "股权激励",
    "回购增持",
    "破净股",
    "低价股",
    "小盘股",
    "中盘股",
    "大盘股",
    "上证50",
    "沪深300",
    "中证500",
    "中证1000",
    "创业板综",
    "科创50",
    "unknown",
    "other",
}

_EXCLUSION_THEME_RELATIONS = {
    "opposite",
    "excluded",
    "exclude",
    "exclusion",
    "against",
    "faded",
    "fade",
    "outdated",
}

_EXPLICIT_THEME_LABEL_KEYS: Tuple[str, ...] = (
    "theme",
    "bk_name",
    "bkName",
    "plate_name",
    "industry",
    "related_boards",
    "concepts",
    "concept",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _explicit_theme_labels(metrics: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return concrete non-generic theme/board/industry labels from metrics."""

    generic = {_normalize_text(item) for item in _GENERIC_THEME_LABELS}
    labels: List[Dict[str, str]] = []
    seen: set = set()
    for key in _EXPLICIT_THEME_LABEL_KEYS:
        value = metrics.get(key)
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            text = str(item or "").strip()
            if not text:
                continue
            normalized = _normalize_text(text)
            if normalized in generic or normalized in seen:
                continue
            seen.add(normalized)
            labels.append({"field": f"metrics.{key}", "value": text[:40]})
    return labels[:8]


def _theme_confirmation_evidence(metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return positive evidence that a seed belongs to *another* theme.

    ``None`` means "evidence insufficient" and must never be read as
    "unrelated". Only two shapes confirm a mismatch:

    - ``exclusion_rule``: explicit opposite / excluded / faded signal.
    - ``explicit_other_theme``: a concrete non-generic theme/board/industry
      label that exists while no target-theme alias matched.
    """

    relation = str(metrics.get("theme_relation") or "").strip().lower()
    if relation in _EXCLUSION_THEME_RELATIONS or _truthy(metrics.get("theme_excluded")):
        return {
            "evidence_kind": "exclusion_rule",
            "severity": 0.85,
            "labels": [],
            "rule": (
                f"theme_relation={relation or 'unknown'}, "
                f"theme_excluded={metrics.get('theme_excluded')}"
            ),
        }
    labels = _explicit_theme_labels(metrics)
    if labels:
        return {
            "evidence_kind": "explicit_other_theme",
            "severity": 0.7,
            "labels": labels,
            "rule": "存在明确他主题/板块/行业标签，且未命中目标主题别名",
        }
    return None


def _row_text(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in (
        "bk_name",
        "bkName",
        "plate_name",
        "industry",
        "concepts",
        "concept",
        # 来源主题字段:盘前日报的 theme / related_boards / matched_keywords 必须
        # 参与主题匹配,否则"日报主题是航运"也会被当成目标主题来源。
        "theme",
        "related_boards",
        "matched_keywords",
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


def _profile_evidence(
    seed: Any,
    match: _MatchedText,
    role: str,
    setup: str,
    *,
    strength: Optional[float],
    net_inflow: Optional[float],
    theme_status: str = "",
    confirmation: Optional[Dict[str, Any]] = None,
) -> List[str]:
    evidence = [f"来源={getattr(seed, 'source', '')}，角色={role}，setup={setup}。"]
    if match.aliases:
        evidence.append(f"主题别名命中：{', '.join(match.aliases)}。")
    if theme_status == "unrelated_confirmed":
        kind = str((confirmation or {}).get("evidence_kind") or "unknown")
        labels = [
            str(item.get("value"))
            for item in (confirmation or {}).get("labels") or []
            if isinstance(item, dict) and item.get("value")
        ]
        label_text = "、".join(labels[:4]) or str((confirmation or {}).get("rule") or "")
        evidence.append(f"确认他主题（{kind}）：{label_text}；未命中目标主题别名。")
    elif theme_status == "unclassified":
        evidence.append("主题证据不足：未命中主题别名，也没有明确他主题/排除证据（不判为无关）。")
    if strength is not None:
        evidence.append(f"主题/板块强度={strength:.2f}。")
    if net_inflow is not None:
        evidence.append(f"资金净流入={net_inflow:.0f}。")
    return evidence


# FAC-05: 主题来源分解。以下字段与 ``_row_text`` 保持一致,保证归因展示与
# 原判定读取同一批文本;新增内容只做审计,不参与 stock_role / 置信度计算。
_ROW_TEXT_KEYS: Tuple[str, ...] = (
    "bk_name",
    "bkName",
    "plate_name",
    "industry",
    "concepts",
    "concept",
    "theme",
    "related_boards",
    "matched_keywords",
    "stock_reason",
    "plate_reason",
    "reason",
    "rank_reason",
    "name",
    "ts_name",
    "hint",
)

_CAPITAL_SOURCES = {
    "capital_flow_anomaly",
    "dragon_tiger",
    "margin_financing",
    "block_trade",
}

_ROLE_RULES: Dict[str, str] = {
    "sector_theme": "热点板块来源：source_label 含 leader 且 rank<=3 → core_leader；rank<=6 或净流入>0 → core_midcap；否则 follower",
    "limit_up_pool": "涨停池来源：连板>=2 或 priority>=84 → high_beta_leader；climax_extension 下 → late_chaser；否则 follower",
    "hot_rank": "热榜来源：rank<=5 → high_beta_leader；否则 follower",
    "news_theme_daily": "盘前日报来源：日报主题/相关板块/关键词命中主题别名后才 +0.25 并固定 follower；未命中不赠主题权重",
}

_THEME_STATUS_RULE = (
    "别名命中 → matched；无命中但有 exclusion_rule / explicit_other_theme 正证据 → "
    "unrelated_confirmed；其余 → unclassified（证据不足，只记录，不否决）"
)


def _theme_text_fields(seed: Any, metrics: Dict[str, Any]) -> List[Dict[str, str]]:
    """Enumerate the exact text fields consumed by the theme classifier."""

    fields: List[Dict[str, str]] = []

    def _push(field: str, value: Any) -> None:
        if value in (None, "", []):
            return
        if isinstance(value, list):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value)
        text = text.strip()
        if text:
            fields.append({"field": field, "text": text})

    _push("name", getattr(seed, "name", None))
    _push("hint", getattr(seed, "hint", None))
    _push("context_hint", getattr(seed, "context_hint", None))
    for key in _ROW_TEXT_KEYS:
        _push(f"metrics.{key}", metrics.get(key))
    for idx, signal in enumerate(getattr(seed, "trigger_signals", None) or []):
        if not isinstance(signal, dict):
            continue
        _push(f"trigger_signals[{idx}].label", signal.get("label"))
        _push(f"trigger_signals[{idx}].value", signal.get("value"))
    return fields


def _compact_detail(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value not in (None, "", [])
    }


def _structured_theme_sources(
    seed: Any,
    metrics: Dict[str, Any],
    *,
    freshness: str,
) -> List[Dict[str, Any]]:
    """Extract concrete provenance records (news / card / sector / rank / limit-up)."""

    source = str(getattr(seed, "source", "") or "unknown")
    as_of = str(freshness or "unknown")
    out: List[Dict[str, Any]] = []

    if source == "news_theme_daily":
        out.append(
            {
                "source_type": "news_theme",
                "source_id": str(metrics.get("source_url") or metrics.get("source_title") or source),
                "label": str(metrics.get("theme") or getattr(seed, "name", "") or source),
                "as_of": as_of,
                "detail": _compact_detail(
                    {
                        "theme": metrics.get("theme"),
                        "source_title": metrics.get("source_title"),
                        "source_url": metrics.get("source_url"),
                        "evidence_section": metrics.get("evidence_section"),
                        "matched_keywords": metrics.get("matched_keywords"),
                        "related_boards": metrics.get("related_boards"),
                        "polarity": metrics.get("polarity"),
                        "directness": metrics.get("directness"),
                        "mapping_role": metrics.get("mapping_role"),
                        "rank": metrics.get("rank"),
                    }
                ),
            }
        )
    elif source == "sector_theme":
        out.append(
            {
                "source_type": "leading_sector",
                "source_id": str(
                    metrics.get("bk_code")
                    or metrics.get("bk_name")
                    or getattr(seed, "code", "")
                    or source
                ),
                "label": str(metrics.get("bk_name") or getattr(seed, "name", "") or source),
                "as_of": as_of,
                "detail": _compact_detail(
                    {
                        "bk_code": metrics.get("bk_code"),
                        "bk_name": metrics.get("bk_name"),
                        "rank": metrics.get("rank"),
                        "strength": metrics.get("strength"),
                        "net_inflow": metrics.get("net_inflow"),
                        "inflow_days": metrics.get("inflow_days"),
                        "change_ratio": metrics.get("change_ratio"),
                        "source_label": metrics.get("source_label"),
                    }
                ),
            }
        )
    elif source == "hot_rank":
        out.append(
            {
                "source_type": "hot_rank",
                "source_id": str(getattr(seed, "code", "") or source),
                "label": str(getattr(seed, "name", "") or source),
                "as_of": as_of,
                "detail": _compact_detail(
                    {
                        "rank": metrics.get("rank"),
                        "rank_reason": metrics.get("rank_reason"),
                        "source_label": metrics.get("source_label"),
                    }
                ),
            }
        )
    elif source == "limit_up_pool":
        out.append(
            {
                "source_type": "limit_up_pool",
                "source_id": str(getattr(seed, "code", "") or source),
                "label": str(getattr(seed, "name", "") or source),
                "as_of": as_of,
                "detail": _compact_detail(
                    {
                        "limit_up_streak": metrics.get("limit_up_streak"),
                        "bomb_num": metrics.get("bomb_num") or metrics.get("open_times"),
                        "reason": metrics.get("reason") or metrics.get("stock_reason"),
                    }
                ),
            }
        )
    elif source in _CAPITAL_SOURCES:
        out.append(
            {
                "source_type": "capital_flow",
                "source_id": str(getattr(seed, "code", "") or source),
                "label": str(getattr(seed, "name", "") or source),
                "as_of": as_of,
                "detail": _compact_detail(
                    {
                        "net_inflow": metrics.get("net_inflow"),
                        "rank": metrics.get("rank"),
                        "source_label": metrics.get("source_label"),
                    }
                ),
            }
        )

    for idx, signal in enumerate(getattr(seed, "trigger_signals", None) or []):
        if not isinstance(signal, dict):
            continue
        if str(signal.get("signal_type") or "").strip() != "news_signal_card":
            continue
        value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
        out.append(
            {
                "source_type": "news_card",
                "source_id": str(value.get("card_id") or f"trigger_signals[{idx}]"),
                "label": str(value.get("summary_short") or signal.get("label") or "新闻信号卡")[:120],
                "as_of": str(value.get("signal_date") or as_of),
                "detail": _compact_detail(
                    {
                        "primary_industries": value.get("primary_industries"),
                        "company_rationale": value.get("company_rationale"),
                        "company_direction": value.get("company_direction"),
                        "signal_score": value.get("signal_score"),
                        "mapping_confidence": value.get("mapping_confidence"),
                        "evidence_grade": value.get("evidence_grade"),
                        "gate_result": value.get("gate_result"),
                    }
                ),
            }
        )
    return out


def _build_theme_attribution(
    seed: Any,
    *,
    aliases: Sequence[str],
    theme: str,
    theme_name: str,
    regime: str,
    metrics: Dict[str, Any],
    matched: _MatchedText,
    membership_confidence: float,
    role: str,
    setup: str,
    theme_status: str = "",
    confirmation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the display-only theme attribution block (FAC-05).

    It replays the same text fields / weights used by the role decision so a
    reviewer can answer "why this theme" or "why unrelated" without changing
    any classification output.
    """

    source = str(getattr(seed, "source", "") or "unknown")
    freshness = str(getattr(seed, "freshness", "") or "unknown")
    fields = _theme_text_fields(seed, metrics)
    field_hits: List[Dict[str, Any]] = []
    hit_aliases: set = set()
    for field in fields:
        field_match = _match_aliases(field["text"], aliases)
        if not field_match.aliases:
            continue
        hit_aliases.update(field_match.aliases)
        field_hits.append(
            {
                "source_type": "seed_text",
                "source_id": field["field"],
                "label": field["field"],
                "as_of": freshness,
                "matched_aliases": field_match.aliases,
                "detail": {"excerpt": field["text"][:120]},
            }
        )

    rank = _to_float(metrics.get("rank"))
    net_inflow = _to_float(metrics.get("net_inflow"))
    top_rank = rank is not None and rank <= 5
    positive_inflow = net_inflow is not None and net_inflow > 0
    heat_with_alias = (
        source in {"hot_rank", "limit_up_pool", "capital_flow_anomaly", "dragon_tiger"}
        and bool(matched.aliases)
    )
    source_theme_weight = source == "sector_theme" or (
        source == "news_theme_daily" and bool(matched.aliases)
    )
    contributions = [
        {
            "factor": "alias_match",
            "weight": 0.45 if matched.aliases else 0.0,
            "hit": bool(matched.aliases),
        },
        {
            "factor": "source_theme_priority",
            "weight": 0.25 if source_theme_weight else 0.0,
            "hit": source_theme_weight,
            "detail": (
                "sector_theme 来源即主题板块成分"
                if source == "sector_theme"
                else "news_theme_daily 需来源主题命中别名才计权重"
            ),
        },
        {
            "factor": "heat_source_with_alias",
            "weight": 0.15 if heat_with_alias else 0.0,
            "hit": heat_with_alias,
        },
        {
            "factor": "top_rank",
            "weight": 0.10 if top_rank else 0.0,
            "hit": top_rank,
        },
        {
            "factor": "positive_net_inflow",
            "weight": 0.05 if positive_inflow else 0.0,
            "hit": positive_inflow,
        },
    ]

    decision = theme_status or (
        "unrelated_confirmed" if role == "unrelated" else ("unclassified" if role == "unknown" else "matched")
    )
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    missing_reason = ""
    if decision == "unclassified":
        if hit_aliases:
            missing_reason = (
                f"membership_confidence={membership_confidence:.2f}；"
                f"虽命中别名 {', '.join(sorted(hit_aliases))}，但来源 {source} 与排名/资金未形成足够权重；"
                "证据不足，不等于与主题无关"
            )
        else:
            missing_reason = (
                f"membership_confidence={membership_confidence:.2f}；"
                f"检查 {len(fields)} 个文本字段未命中主题别名，来源 {source} 不提供主题权重，"
                "且无他主题/排除证据（证据不足，不等于与主题无关）"
            )
    elif decision == "unrelated_confirmed":
        kind = str(confirmation.get("evidence_kind") or "unknown")
        labels = [
            str(item.get("value"))
            for item in confirmation.get("labels") or []
            if isinstance(item, dict) and item.get("value")
        ]
        if kind == "exclusion_rule":
            missing_reason = f"排除规则命中：{confirmation.get('rule') or 'unknown'}；未命中目标主题别名"
        else:
            missing_reason = (
                f"明确他主题标签：{'、'.join(labels[:4]) or 'unknown'}；未命中目标主题别名"
            )

    sources: List[Dict[str, Any]] = [
        {
            "source_type": "seed_source",
            "source_id": source,
            "label": source,
            "as_of": freshness,
            "matched_aliases": sorted(hit_aliases),
            "detail": _compact_detail(
                {
                    "priority_score": getattr(seed, "priority_score", None),
                    "rank": metrics.get("rank"),
                    "source_label": metrics.get("source_label"),
                    "theme": metrics.get("theme"),
                    "related_boards": metrics.get("related_boards"),
                    "hint": str(getattr(seed, "hint", "") or "")[:120],
                }
            ),
        }
    ]
    sources.extend(_structured_theme_sources(seed, metrics, freshness=freshness))
    sources.extend(field_hits[:6])

    role_rule = _ROLE_RULES.get(source, f"来源 {source} 不在主题权重表内")
    if role in {"late_chaser", "exhaustion_candidate"}:
        role_rule += "；开板/高潮/退潮规则覆盖角色"
    elif role in {"core_leader", "core_midcap", "high_beta_leader"}:
        role_rule += "；命中核心角色规则"
    elif role == "follower":
        role_rule += "；落入 follower"
    elif role == "unrelated":
        role_rule = "存在他主题/排除正证据 → unrelated_confirmed"
    elif role == "unknown":
        role_rule = "无主题别名且无正证据 → unclassified（只记录，不否决）"

    return {
        "theme": theme,
        "theme_name": theme_name,
        "decision": decision,
        "theme_status": decision,
        "confirmation": confirmation,
        "stock_role": role,
        "theme_regime": regime,
        "membership_confidence": round(membership_confidence, 4),
        "rule": {
            "membership_threshold": 0.25,
            "theme_status_rule": _THEME_STATUS_RULE,
            "role_rule": role_rule,
            "momentum_setup": setup,
        },
        "contributions": contributions,
        "sources": sources[:12],
        "fields_checked": [field["field"] for field in fields],
        "missing_reason": missing_reason,
        "as_of": freshness,
        "display_only": True,
        "affects_ranking": False,
        "affects_theme_decision": False,
    }


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
