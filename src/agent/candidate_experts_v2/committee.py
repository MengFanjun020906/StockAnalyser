# -*- coding: utf-8 -*-
"""LLM expert committee facade for candidate discovery.

This module is the *only* entry point that stock_selection should use when
``AGENT_CANDIDATE_DISCOVERY_MODE=llm_expert_committee`` or
``AGENT_CANDIDATE_DISCOVERY_MODE=thesis_desk_committee``.

Both modes return a payload structurally compatible with the deterministic
``discover_watchlist_candidates`` tool so downstream pipeline stages remain
untouched.

``run_committee_discovery`` (llm_expert_committee mode) delegates to
``run_thesis_desk_committee`` (P4 three-desk pipeline: recall → desks →
aggregate → allocate_slots) and preserves the legacy payload shape.

Seed pool construction (four sources, deterministic, runs before any LLM call):
1. User-provided target_symbols → source="user_watchlist"
2. Daily limit-up list (get_tushare_limit_list_d) → source="limit_up_pool"
3. Hot-rank list (get_tushare_hot_rank) → source="hot_rank"
4. AlphaSift + Sequoia quant candidates → source="alphasift" / "sequoia"

If the committee expert layer fails or returns no picks, the facade fails the
candidate-discovery stage and attaches diagnostics. The seed pool is input
provenance, not an automatic L1 candidate fallback.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.agent.candidate_experts_v2.experts.base import (
    LLMCallable,
    LLMToolCall,
    LLMTurn,
)
from src.agent.candidate_experts_v2.runtime import run_experts_parallel
from src.agent.candidate_experts_v2.schemas import (
    AggregatedCandidate,
    FeatureRow,
    SeedItem,
)
from src.agent.candidate_experts_v2.runtime import ExpertTask  # noqa: F401 — re-export for callers

logger = logging.getLogger(__name__)

# Stance strength for resolving a single top-level stance from per-desk stances.
_STANCE_RANK: Dict[str, int] = {
    "support": 3,
    "watch": 2,
    "neutral": 1,
    "oppose": 0,
    "invalid": -1,
}


CommitteeOverallTimeoutSeconds = 90.0
SEED_SOURCE_CAPS: Dict[str, int] = {
    "daily_screener": 20,
    "limit_up_pool": 10,
    "hot_rank": 8,
    "sector_theme": 6,
    "event_impact": 6,
    "news_momentum": 6,
    "capital_flow_anomaly": 8,
    "northbound_stock_connect": 6,
    "margin_financing": 6,
    "block_trade": 6,
    "dragon_tiger": 6,
    "valuation_liquidity": 6,
    "alphasift": 10,
    "sequoia": 8,
    "fundamental_snapshot": 8,
    "low_base_structure": 8,
    "fallback": 4,
}
SEED_SOURCE_ORDER = [
    "daily_screener",
    "fundamental_snapshot",
    "low_base_structure",
    "limit_up_pool",
    "hot_rank",
    "sector_theme",
    "event_impact",
    "news_momentum",
    "capital_flow_anomaly",
    "northbound_stock_connect",
    "margin_financing",
    "block_trade",
    "dragon_tiger",
    "valuation_liquidity",
    "alphasift",
    "sequoia",
    "fallback",
]
SEED_BUILD_LIMIT = 20
SEED_GATE_INPUT_LIMIT = 20
SEED_GATE_OUTPUT_LIMIT = 12
SEED_GATE_MIN_KEEP = 6
SEED_GATE_TRIGGER_THRESHOLD = 30
LOCAL_HARD_EXCLUSION_MIN_TRADING_DAYS = 60
LOCAL_HARD_EXCLUSION_MIN_AVG_TURNOVER = 10_000_000.0


def _compress_seed_priority_score(raw_score: float) -> float:
    """Keep seed priority sortable without flattening strong local signals at 100."""
    score = max(0.0, _safe_float(raw_score) or 0.0)
    if score <= 95.0:
        return round(score, 2)
    return round(95.0 + 4.8 * (1.0 - math.exp(-(score - 95.0) / 20.0)), 2)


@dataclass
class SeedPoolBuildResult:
    """Deterministic seed-pool assembly output before LLM gate filtering."""

    seeds: List[SeedItem]
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    hard_exclusion: Dict[str, Any] = field(default_factory=dict)
    source_quality: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_limit: int = 40
    market_regime: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SeedGateResult:
    """LLM gate output with deterministic fallback preserved."""

    seeds: List[SeedItem]
    status: str
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    kept_count: int = 0
    rejected_count: int = 0
    error: str = ""


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _seed_pool_summary(seeds: Sequence[SeedItem], *, total_limit: int) -> Dict[str, Any]:
    source_counts: Dict[str, int] = {}
    preview: List[Dict[str, Any]] = []
    dimension_counts: Dict[str, int] = {}
    for seed in seeds:
        source_counts[seed.source] = source_counts.get(seed.source, 0) + 1
        for signal in seed.trigger_signals or []:
            if not isinstance(signal, dict):
                continue
            dimension = str(signal.get("dimension") or "").strip()
            if dimension:
                dimension_counts[dimension] = dimension_counts.get(dimension, 0) + 1
        if len(preview) < 20:
            preview.append(
                {
                    "code": seed.code,
                    "name": seed.name,
                    "source": seed.source,
                    "hint": seed.hint,
                    "priority_score": seed.priority_score,
                    "freshness": seed.freshness,
                    "trigger_signals": seed.trigger_signals[:4],
                }
            )
    return {
        "seed_count": len(seeds),
        "seed_sources": source_counts,
        "signal_dimensions": dimension_counts,
        "total_limit": total_limit,
        "preview": preview,
    }


def _compact_desk_packet_for_trace(packet: ExpertPacketV2) -> Dict[str, Any]:
    """Serialize one thesis-desk packet for trace/UI inspection."""

    try:
        payload = packet.model_dump(mode="json")
    except Exception:
        payload = {
            "expert": getattr(packet, "expert", ""),
            "dimension": getattr(packet, "dimension", ""),
            "status": getattr(packet, "status", "unknown"),
            "candidates": [],
            "rejected": [],
            "diagnostics": [],
            "errors": [],
        }
    payload["candidate_count"] = len(payload.get("candidates") or [])
    payload["rejected_count"] = len(payload.get("rejected") or [])
    return payload


def _coerce_llm_callable(llm_adapter: Any) -> LLMCallable:
    """Wrap ``llm_adapter`` into the ``LLMCallable`` signature.

    Accepts either:
    - a callable already matching ``(messages, tool_decls) -> LLMTurn``; or
    - an object with ``.chat(messages, tools=...)`` returning either an
      ``LLMTurn`` or an OpenAI-style ``{"tool_calls": [...], "content": "..."}``
      mapping.
    """

    if callable(llm_adapter):
        # Best-effort: assume the caller already supplied the right shape.
        return llm_adapter  # type: ignore[return-value]

    chat = getattr(llm_adapter, "chat", None)
    if not callable(chat):
        raise TypeError(
            "llm_adapter must be callable or expose a .chat(messages, tools=...) method"
        )

    def _call(messages: List[Dict[str, Any]], tool_decls: List[Dict[str, Any]]) -> LLMTurn:
        raw = chat(messages, tools=tool_decls)
        if isinstance(raw, LLMTurn):
            return raw
        if isinstance(raw, dict):
            tool_calls_raw = raw.get("tool_calls") or []
            tool_calls: List[LLMToolCall] = []
            for tc in tool_calls_raw:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                tool_calls.append(
                    LLMToolCall(
                        name=str(fn.get("name") or ""),
                        arguments=dict(fn.get("arguments") or {}),
                        call_id=str(tc.get("id") or ""),
                    )
                )
            return LLMTurn(tool_calls=tool_calls, text=str(raw.get("content") or ""))
        return LLMTurn(tool_calls=[], text=str(raw))

    return _call


def _to_seed_items(seed_symbols: Sequence[str], market: str) -> List[SeedItem]:
    items: List[SeedItem] = []
    for symbol in seed_symbols or []:
        code = str(symbol or "").strip()
        if not code:
            continue
        items.append(
            SeedItem(
                code=code,
                name=code,
                market=market,
                source="user_watchlist",
                hint="用户输入/关注列表",
                trigger_signals=[
                    {
                        "dimension": "user",
                        "signal_type": "user_watchlist",
                        "value": 1,
                        "threshold": 1,
                        "deviation": 0,
                    }
                ],
                priority_score=100.0,
                freshness="request",
                context_hint="用户显式关注，必须保留给下游专家核验。",
            )
        )
    return items


def _safe_tool_call(tool_registry: Any, name: str, **kwargs: Any) -> Dict[str, Any]:
    """Execute a tool by name from ToolRegistry or dict, swallowing all errors."""
    try:
        execute_fn = getattr(tool_registry, "execute", None)
        if callable(execute_fn):
            result = execute_fn(name, **kwargs)
            return result if isinstance(result, dict) else {}
        if isinstance(tool_registry, dict):
            fn = tool_registry.get(name)
            if callable(fn):
                result = fn(**kwargs)
                return result if isinstance(result, dict) else {}
    except Exception as exc:
        logger.debug("_safe_tool_call %s failed: %s", name, exc)
    return {}


def _build_signal(
    *,
    dimension: str,
    signal_type: str,
    value: Any,
    threshold: Any = None,
    deviation: Any = None,
    label: str = "",
) -> Dict[str, Any]:
    signal: Dict[str, Any] = {
        "dimension": dimension,
        "signal_type": signal_type,
        "value": value,
    }
    if threshold is not None:
        signal["threshold"] = threshold
    if deviation is not None:
        signal["deviation"] = deviation
    if label:
        signal["label"] = label
    return signal


def _source_diagnostic(
    source: str,
    status: str,
    *,
    count: int = 0,
    error: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"source": source, "status": status, "count": count}
    if error:
        payload["error"] = error
    if detail:
        payload.update(detail)
    return payload


def _source_quality(status: str, *, freshness: str = "unknown", error: str = "") -> Dict[str, Any]:
    ok = status in {"ok", "partial", "empty"}
    quality = {
        "status": status,
        "freshness": freshness,
        "available": ok,
    }
    if error:
        quality["error"] = error
    return quality


def _seed_from_candidate(
    cand: Dict[str, Any],
    *,
    market: str,
    source: str,
    fallback_hint: str,
    default_dimension: str,
    default_signal_type: str,
    freshness: str = "latest_local",
) -> Optional[SeedItem]:
    code = str(cand.get("code") or cand.get("symbol") or "").strip()
    if not code:
        return None
    metrics = cand.get("metrics") if isinstance(cand.get("metrics"), dict) else {}
    dimensions = cand.get("reason_dimensions") if isinstance(cand.get("reason_dimensions"), list) else []
    trigger_signals: List[Dict[str, Any]] = []
    for dimension_item in dimensions[:4]:
        if not isinstance(dimension_item, dict):
            continue
        dimension = str(dimension_item.get("dimension") or default_dimension).strip() or default_dimension
        trigger_signals.append(
            _build_signal(
                dimension=dimension,
                signal_type=default_signal_type,
                value=dimension_item.get("detail") or cand.get("reason") or fallback_hint,
                label=str(dimension_item.get("label") or ""),
            )
        )
    if not trigger_signals:
        trigger_signals.append(
            _build_signal(
                dimension=default_dimension,
                signal_type=default_signal_type,
                value=cand.get("reason") or fallback_hint,
                threshold=cand.get("matched_strategies") or cand.get("strategy_tags") or None,
            )
        )
    priority = _safe_float(cand.get("signal_score")) or _safe_float(cand.get("score")) or 50.0
    latest_date = str(cand.get("latest_date") or cand.get("updated_at") or "").strip()
    return SeedItem(
        code=code,
        name=str(cand.get("name") or code),
        market=market,
        source=source,  # type: ignore[arg-type]
        hint=str(cand.get("reason") or fallback_hint)[:240],
        trigger_signals=trigger_signals,
        priority_score=round(float(priority), 2),
        freshness=latest_date or freshness,
        context_hint=str(cand.get("ranking_hints") or cand.get("reason") or fallback_hint)[:240],
        extras={
            "metrics": metrics,
            "matched_strategies": cand.get("matched_strategies") or [],
            "strategy_tags": cand.get("strategy_tags") or [],
            "raw_source": cand.get("source") or source,
        },
    )


def _dedupe_by_code(seeds: Sequence[SeedItem]) -> List[SeedItem]:
    by_code: Dict[str, SeedItem] = {}
    for seed in seeds:
        code = str(seed.code or "").strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = seed
            continue
        current = by_code[code]
        current_sources = list(current.extras.get("recall_sources") or [current.source])
        if seed.source not in current_sources:
            current_sources.append(seed.source)
        merged_signals = [*(current.trigger_signals or []), *(seed.trigger_signals or [])]
        current.trigger_signals = merged_signals[:12]
        current.priority_score = round(max(current.priority_score, seed.priority_score) + 3.0, 2)
        current.extras["recall_sources"] = current_sources
        current.extras.setdefault("merged_hints", [])
        current.extras["merged_hints"] = [*current.extras["merged_hints"], seed.hint][:6]
        if len(set(sig.get("dimension") for sig in merged_signals if isinstance(sig, dict))) >= 2:
            current.priority_score = round(current.priority_score + 2.0, 2)
    return list(by_code.values())


def _capital_flow_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    api_label: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or "").strip()
    if not code:
        return None
    net_inflow = _safe_float(item.get("net_inflow"))
    net_5d = _safe_float(item.get("net_5d_inflow"))
    large = _safe_float(item.get("large_net_inflow") or item.get("extra_large_net_inflow"))
    change_pct = _safe_float(item.get("pct_change") or item.get("change_ratio"))
    if all(value is None for value in (net_inflow, net_5d, large)):
        return None
    strongest = max(abs(value or 0.0) for value in (net_inflow, net_5d, large))
    if strongest < 20_000_000:
        return None
    direction = "净流入" if (net_inflow or 0.0) >= 0 else "净流出"
    signals = [
        _build_signal(
            dimension="capital",
            signal_type=f"{api_label}_main_net_inflow",
            value=round(net_inflow or 0.0, 2),
            threshold=20_000_000,
            deviation=round((net_inflow or 0.0) / 100_000_000, 3),
            label=f"主力资金{direction}",
        )
    ]
    if net_5d is not None:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type=f"{api_label}_5d_net_inflow",
                value=round(net_5d, 2),
                threshold=20_000_000,
                deviation=round(net_5d / 100_000_000, 3),
                label="5日资金变化",
            )
        )
    score = 64.0 + min(16.0, strongest / 100_000_000 * 3.0) + max(0.0, 8.0 - rank * 0.4)
    if change_pct is not None:
        score += min(8.0, abs(change_pct) * 0.8)
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="capital_flow_anomaly",
        hint=f"{api_label} {direction}，rank={rank}",
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="资金面异常榜单补充来源，只代表资金变化显著，后续需核验价格结构和持续性。",
        extras={
            "metrics": {
                "rank": rank,
                "api_label": api_label,
                "net_inflow": net_inflow,
                "net_5d_inflow": net_5d,
                "large_net_inflow": large,
                "pct_change": change_pct,
            }
        },
    )


def _northbound_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or _ts_code_to_symbol(item.get("ts_code")) or "").strip()
    if not code:
        return None
    net_amount = _safe_float(item.get("net_amount"))
    amount = _safe_float(item.get("amount"))
    if amount is None and net_amount is None:
        return None
    if abs(net_amount or 0.0) < 20_000_000 and (amount or 0.0) < 100_000_000:
        return None
    direction = "净买入" if (net_amount or 0.0) >= 0 else "净卖出"
    signals = [
        _build_signal(
            dimension="capital",
            signal_type="northbound_stock_connect_net_amount",
            value=round(net_amount or 0.0, 2),
            threshold=20_000_000,
            deviation=round((net_amount or 0.0) / 100_000_000, 3),
            label=f"陆股通{direction}",
        )
    ]
    if amount is not None:
        signals.append(
            _build_signal(
                dimension="attention",
                signal_type="northbound_stock_connect_turnover",
                value=round(amount, 2),
                threshold=100_000_000,
                deviation=round(amount / 100_000_000, 3),
                label="陆股通成交额",
            )
        )
    score = 63.0 + min(16.0, abs(net_amount or 0.0) / 100_000_000 * 4.0)
    score += min(10.0, (amount or 0.0) / 500_000_000 * 4.0)
    score += max(0.0, 6.0 - rank * 0.35)
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="northbound_stock_connect",
        hint=f"陆股通{direction}，rank={item.get('rank') or rank}",
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="陆股通十大成交股种子源，只代表北向/互联互通成交活跃，后续需结合行业和价格位置核验。",
        extras={
            "metrics": {
                "rank": rank,
                "market_type": item.get("market_type"),
                "amount": amount,
                "net_amount": net_amount,
                "buy": _safe_float(item.get("buy")),
                "sell": _safe_float(item.get("sell")),
            }
        },
    )


def _margin_financing_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or _ts_code_to_symbol(item.get("ts_code")) or "").strip()
    if not code:
        return None
    financing_buy = _safe_float(item.get("financing_buy") or item.get("rzmre"))
    financing_balance = _safe_float(item.get("financing_balance") or item.get("rzye"))
    margin_balance = _safe_float(item.get("margin_balance") or item.get("rzrqye"))
    if financing_buy is None and financing_balance is None and margin_balance is None:
        return None
    if (financing_buy or 0.0) < 30_000_000 and (financing_balance or 0.0) < 300_000_000:
        return None
    signals = [
        _build_signal(
            dimension="capital",
            signal_type="margin_financing_buy",
            value=round(financing_buy or 0.0, 2),
            threshold=30_000_000,
            deviation=round((financing_buy or 0.0) / 100_000_000, 3),
            label="融资买入活跃",
        )
    ]
    if financing_balance is not None:
        signals.append(
            _build_signal(
                dimension="risk",
                signal_type="margin_financing_balance",
                value=round(financing_balance, 2),
                threshold=300_000_000,
                deviation=round(financing_balance / 100_000_000, 3),
                label="融资余额规模",
            )
        )
    score = 60.0 + min(18.0, (financing_buy or 0.0) / 100_000_000 * 5.0)
    score += min(8.0, (financing_balance or 0.0) / 1_000_000_000 * 2.0)
    score += max(0.0, 6.0 - rank * 0.3)
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="margin_financing",
        hint=f"融资买入={round((financing_buy or 0.0) / 100_000_000, 2)}亿",
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="融资融券明细种子源，代表杠杆资金活跃；融资余额过高也可能是去杠杆风险。",
        extras={
            "metrics": {
                "rank": rank,
                "financing_buy": financing_buy,
                "financing_balance": financing_balance,
                "short_balance": _safe_float(item.get("short_balance") or item.get("rqye")),
                "margin_balance": margin_balance,
            }
        },
    )


def _block_trade_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or _ts_code_to_symbol(item.get("ts_code")) or "").strip()
    if not code:
        return None
    amount = _safe_float(item.get("amount"))
    price = _safe_float(item.get("price"))
    if amount is None or amount < 30_000_000:
        return None
    buyer = str(item.get("buyer") or "").strip()
    seller = str(item.get("seller") or "").strip()
    signals = [
        _build_signal(
            dimension="capital",
            signal_type="block_trade_amount",
            value=round(amount, 2),
            threshold=30_000_000,
            deviation=round(amount / 100_000_000, 3),
            label="大宗交易放量",
        )
    ]
    score = 58.0 + min(18.0, amount / 100_000_000 * 4.0) + max(0.0, 6.0 - rank * 0.35)
    if buyer and seller and buyer != seller:
        score += 3.0
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="block_trade",
        hint=f"大宗交易额={round(amount / 100_000_000, 2)}亿",
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="大宗交易种子源，可能代表机构换手、股东减持或接盘意愿；必须结合折溢价和后续走势判断方向。",
        extras={
            "metrics": {
                "rank": rank,
                "amount": amount,
                "price": price,
                "volume": _safe_float(item.get("volume") or item.get("vol")),
                "buyer": buyer,
                "seller": seller,
            }
        },
    )


def _dragon_tiger_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or "").strip()
    if not code:
        return None
    amount = _safe_float(item.get("amount") or item.get("dragon_tiger_amount"))
    net_inflow = _safe_float(item.get("net_inflow"))
    turnover_rate = _safe_float(item.get("turnover_rate") or item.get("turnover_ratio"))
    if amount is None and net_inflow is None and turnover_rate is None:
        return None
    if amount is not None and amount < 20_000_000 and abs(net_inflow or 0.0) < 10_000_000:
        return None
    reason = str(item.get("reason") or "龙虎榜上榜").strip()
    signals = [
        _build_signal(
            dimension="attention",
            signal_type="dragon_tiger_listed",
            value=reason,
            threshold="listed",
            deviation=round((net_inflow or 0.0) / 100_000_000, 3) if net_inflow is not None else None,
            label="龙虎榜上榜",
        )
    ]
    if turnover_rate is not None:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type="dragon_tiger_turnover",
                value=round(turnover_rate, 2),
                threshold=5.0,
                deviation=round(turnover_rate - 5.0, 2),
                label="龙虎榜换手",
            )
        )
    score = 66.0 + max(0.0, 8.0 - rank * 0.5)
    if net_inflow is not None:
        score += min(12.0, abs(net_inflow) / 100_000_000 * 4.0)
    if turnover_rate is not None:
        score += min(8.0, turnover_rate * 0.4)
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="dragon_tiger",
        hint=f"龙虎榜：{reason[:80]}",
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="龙虎榜只代表市场关注和交易异动，后续必须区分机构买入、游资博弈和出货风险。",
        extras={
            "metrics": {
                "rank": rank,
                "amount": amount,
                "net_inflow": net_inflow,
                "turnover_rate": turnover_rate,
                "reason": reason,
            }
        },
    )


def _valuation_liquidity_seed_from_item(
    item: Dict[str, Any],
    *,
    market: str,
    rank: int,
    freshness: str,
) -> Optional[SeedItem]:
    code = str(item.get("code") or _ts_code_to_symbol(item.get("ts_code")) or "").strip()
    if not code:
        return None
    turnover_rate = _safe_float(item.get("turnover_rate"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    pe = _safe_float(item.get("pe_ttm") or item.get("pe"))
    pb = _safe_float(item.get("pb"))
    circ_mv = _safe_float(item.get("circ_mv"))
    signals: List[Dict[str, Any]] = []
    if volume_ratio is not None and volume_ratio >= 1.8:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type="daily_basic_volume_ratio",
                value=round(volume_ratio, 2),
                threshold=1.8,
                deviation=round(volume_ratio - 1.8, 2),
                label="量比异常",
            )
        )
    if turnover_rate is not None and turnover_rate >= 5.0:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type="daily_basic_turnover_rate",
                value=round(turnover_rate, 2),
                threshold=5.0,
                deviation=round(turnover_rate - 5.0, 2),
                label="换手率异常",
            )
        )
    if pe is not None and pb is not None and 0 < pe <= 35 and 0 < pb <= 4 and turnover_rate is not None and turnover_rate >= 1.0:
        signals.append(
            _build_signal(
                dimension="fundamental",
                signal_type="valuation_liquidity_balance",
                value={"pe": pe, "pb": pb, "turnover_rate": turnover_rate},
                threshold={"pe": "<=35", "pb": "<=4", "turnover_rate": ">=1"},
                label="估值/流动性平衡",
            )
        )
    if not signals:
        return None
    score = 58.0 + max(0.0, 8.0 - rank * 0.15)
    if volume_ratio is not None:
        score += min(12.0, max(0.0, volume_ratio - 1.0) * 3.0)
    if turnover_rate is not None:
        score += min(10.0, turnover_rate * 0.7)
    if pe is not None and 0 < pe <= 35:
        score += max(0.0, min(8.0, (35 - pe) * 0.2))
    return SeedItem(
        code=code,
        name=str(item.get("name") or code),
        market=market,
        source="valuation_liquidity",
        hint="；".join(str(signal.get("label") or signal.get("signal_type")) for signal in signals[:3]),
        trigger_signals=signals,
        priority_score=round(min(100.0, score), 2),
        freshness=freshness,
        context_hint="daily_basic 日度估值/流动性补充来源，用于发现量比、换手或估值流动性同时改善的样本。",
        extras={
            "metrics": {
                "rank": rank,
                "turnover_rate": turnover_rate,
                "volume_ratio": volume_ratio,
                "pe": pe,
                "pb": pb,
                "circ_mv": circ_mv,
            }
        },
    )


def _ts_code_to_symbol(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split(".")[0].strip()


def _passes_hard_exclusion(seed: SeedItem) -> Tuple[bool, str]:
    code = str(seed.code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return False, "invalid_a_share_code"
    from dataclasses import replace as _dc_replace
    from src.agent.candidate_experts.filters import evaluate_hard_exclusion, resolve_candidate_exclusion_policy
    item_dict: Dict[str, Any] = {"code": code, "name": str(seed.name or "").strip(), "source": seed.source}
    if isinstance(seed.extras, dict) and isinstance(seed.extras.get("metrics"), dict):
        item_dict.update(seed.extras["metrics"])
    # Seed items use descriptive names, not official stock names — skip name-code match
    policy = _dc_replace(resolve_candidate_exclusion_policy(), enforce_name_code_match=False)
    reason = evaluate_hard_exclusion(item_dict, policy)
    if reason:
        return False, reason
    return True, ""


def _assemble_seed_pool(
    seeds_by_source: Dict[str, List[SeedItem]],
    *,
    total_limit: int,
    limit_per_source: int,
) -> List[SeedItem]:
    result_pool: List[SeedItem] = []
    seen_codes: Dict[str, bool] = {}
    for source in SEED_SOURCE_ORDER:
        cap = max(0, min(SEED_SOURCE_CAPS.get(source, limit_per_source), total_limit))
        ordered = sorted(
            seeds_by_source.get(source, []),
            key=lambda item: (-_safe_float(item.priority_score or 0.0) or 0.0, str(item.code or "")),
        )
        for item in ordered[:cap]:
            code = str(item.code or "").strip()
            if not code or code in seen_codes:
                continue
            seen_codes[code] = True
            result_pool.append(item)
            if len(result_pool) >= total_limit:
                break
        if len(result_pool) >= total_limit:
            break
    return result_pool


def _extract_payload_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("items", "candidates", "data", "rows"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _seed_source_counts(seeds: Sequence[SeedItem]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for seed in seeds:
        source = str(seed.source or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _seed_log_preview(seeds: Sequence[SeedItem], *, limit: int = 12) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for seed in list(seeds)[:limit]:
        preview.append(
            {
                "code": seed.code,
                "source": seed.source,
                "score": seed.priority_score,
                "freshness": seed.freshness,
                "signals": [
                    str(signal.get("signal_type") or "")
                    for signal in (seed.trigger_signals or [])[:3]
                    if isinstance(signal, dict)
                ],
            }
        )
    return preview


def _log_seed_source_diagnostic(diagnostic: Dict[str, Any]) -> None:
    source = str(diagnostic.get("source") or "unknown")
    status = str(diagnostic.get("status") or "unknown")
    count = diagnostic.get("count", 0)
    error = str(diagnostic.get("error") or "")
    detail = {
        key: diagnostic.get(key)
        for key in ("trade_date", "latest_date", "latest_period", "updated_at", "db_path")
        if diagnostic.get(key) is not None
    }
    if status in {"failed", "error", "timeout", "unavailable"}:
        logger.warning(
            "seed pool source failed: source=%s status=%s count=%s error=%s detail=%s",
            source,
            status,
            count,
            error,
            detail,
        )
    else:
        logger.info(
            "seed pool source result: source=%s status=%s count=%s detail=%s",
            source,
            status,
            count,
            detail,
        )


def _build_seed_pool(
    *,
    market: str,
    seed_symbols: Sequence[str],
    tool_registry: Any,
    today: Optional[str] = None,
    limit_per_source: int = 15,
    total_limit: int = 40,
) -> List[SeedItem]:
    """Compatibility wrapper returning only seed items."""

    return _build_seed_pool_result(
        market=market,
        seed_symbols=seed_symbols,
        tool_registry=tool_registry,
        today=today,
        limit_per_source=limit_per_source,
        total_limit=total_limit,
    ).seeds


def _build_seed_pool_result(
    *,
    market: str,
    seed_symbols: Sequence[str],
    tool_registry: Any,
    today: Optional[str] = None,
    limit_per_source: int = 15,
    total_limit: int = SEED_BUILD_LIMIT,
) -> SeedPoolBuildResult:
    """Build the shared seed pool with deterministic local scan plus online supplements.

    Priority order (first occurrence of each code wins):
    1. User-provided seed_symbols → source="user_watchlist"
    2. Local price-volume anomaly scan → source="local_price_volume"
    3. Fundamental / low-base local providers
    4. Daily limit-up + hot-rank online supplements
    5. AlphaSift quant candidates → source="alphasift"
       Sequoia quant candidates  → source="sequoia"
    """
    seeds_by_source: Dict[str, List[SeedItem]] = {key: [] for key in SEED_SOURCE_CAPS}
    diagnostics: List[Dict[str, Any]] = []
    source_quality: Dict[str, Dict[str, Any]] = {}
    exclusion_counts: Dict[str, int] = {}
    local_hard_exclusion: Dict[str, Any] = {}
    market_regime: Dict[str, Any] = {}
    started = time.time()

    def _collect(item: SeedItem) -> None:
        code = str(item.code or "").strip()
        if not code:
            return
        keep, reason = _passes_hard_exclusion(item)
        if not keep:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
            return
        bucket = seeds_by_source.setdefault(item.source, [])
        if any(existing.code == code for existing in bucket):
            return
        bucket.append(item)

    # Use the most recent trading day for limit-up/hot-rank so weekend/holiday
    # runs still fetch real data from the last session.
    try:
        from src.agent.tools.data_tools import _latest_tushare_trade_date
        trade_date = _latest_tushare_trade_date() or today or ""
    except Exception:
        trade_date = today or ""

    # --- Source 1: daily screener (replaces user_watchlist + local_price_volume) ---
    try:
        screener_seeds, screener_diag = _build_daily_screener_seeds(
            trade_date=trade_date,
            limit=limit_per_source,
        )
        for seed in screener_seeds:
            _collect(seed)
        diagnostics.append(screener_diag)
        _log_seed_source_diagnostic(screener_diag)
        source_quality["daily_screener"] = _source_quality(
            str(screener_diag.get("status") or "unknown"),
            freshness=str(screener_diag.get("trade_date") or trade_date or "latest"),
            error=str(screener_diag.get("error") or ""),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: daily_screener failed: %s", exc)
        diagnostic = _source_diagnostic("daily_screener", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["daily_screener"] = _source_quality("failed", error=message)

    # --- Source 3a: capital-flow anomaly rankings ---
    for tool_name, api_label in (
        ("get_tushare_moneyflow_ths", "moneyflow_ths"),
        ("get_tushare_moneyflow_dc", "moneyflow_dc"),
    ):
        try:
            result = _safe_tool_call(
                tool_registry,
                tool_name,
                trade_date=trade_date,
                stock_code="",
                limit=limit_per_source,
            )
            items = _extract_payload_items(result)
            accepted = 0
            for rank, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                seed = _capital_flow_seed_from_item(
                    item,
                    market=market,
                    api_label=api_label,
                    rank=rank,
                    freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"),
                )
                if seed is None:
                    continue
                _collect(seed)
                accepted += 1
            status = str(result.get("status") or ("ok" if items else "empty")).lower()
            source_key = f"capital_flow_anomaly:{api_label}"
            diagnostic = _source_diagnostic(
                source_key,
                status,
                count=accepted,
                error=str(result.get("error") or ""),
                detail={"trade_date": result.get("trade_date") or trade_date, "raw_count": len(items)},
            )
            diagnostics.append(diagnostic)
            _log_seed_source_diagnostic(diagnostic)
            source_quality[source_key] = _source_quality(status, freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"), error=str(result.get("error") or ""))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            source_key = f"capital_flow_anomaly:{api_label}"
            logger.debug("seed pool: %s failed: %s", source_key, exc)
            diagnostic = _source_diagnostic(source_key, "failed", error=message, detail={"trade_date": trade_date})
            diagnostics.append(diagnostic)
            _log_seed_source_diagnostic(diagnostic)
            source_quality[source_key] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3b: Stock Connect top traded stocks ---
    try:
        result = _safe_tool_call(
            tool_registry,
            "get_tushare_hsgt_top10",
            trade_date=trade_date,
            stock_code="",
            limit=limit_per_source,
        )
        items = _extract_payload_items(result)
        accepted = 0
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            seed = _northbound_seed_from_item(
                item,
                market=market,
                rank=rank,
                freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"),
            )
            if seed is None:
                continue
            _collect(seed)
            accepted += 1
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic(
            "northbound_stock_connect",
            status,
            count=accepted,
            error=str(result.get("error") or ""),
            detail={"trade_date": result.get("trade_date") or trade_date, "raw_count": len(items)},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["northbound_stock_connect"] = _source_quality(status, freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"), error=str(result.get("error") or ""))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: northbound_stock_connect failed: %s", exc)
        diagnostic = _source_diagnostic("northbound_stock_connect", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["northbound_stock_connect"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3c: margin financing detail ---
    try:
        result = _safe_tool_call(
            tool_registry,
            "get_tushare_margin_detail",
            trade_date=trade_date,
            stock_code="",
            limit=max(limit_per_source * 3, 30),
        )
        items = _extract_payload_items(result)
        accepted = 0
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            seed = _margin_financing_seed_from_item(
                item,
                market=market,
                rank=rank,
                freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"),
            )
            if seed is None:
                continue
            _collect(seed)
            accepted += 1
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic(
            "margin_financing",
            status,
            count=accepted,
            error=str(result.get("error") or ""),
            detail={"trade_date": result.get("trade_date") or trade_date, "raw_count": len(items)},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["margin_financing"] = _source_quality(status, freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"), error=str(result.get("error") or ""))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: margin_financing failed: %s", exc)
        diagnostic = _source_diagnostic("margin_financing", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["margin_financing"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3d: block trades ---
    try:
        result = _safe_tool_call(
            tool_registry,
            "get_tushare_block_trade",
            trade_date=trade_date,
            stock_code="",
            limit=max(limit_per_source * 3, 30),
        )
        items = _extract_payload_items(result)
        accepted = 0
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            seed = _block_trade_seed_from_item(
                item,
                market=market,
                rank=rank,
                freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"),
            )
            if seed is None:
                continue
            _collect(seed)
            accepted += 1
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic(
            "block_trade",
            status,
            count=accepted,
            error=str(result.get("error") or ""),
            detail={"trade_date": result.get("trade_date") or trade_date, "raw_count": len(items)},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["block_trade"] = _source_quality(status, freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"), error=str(result.get("error") or ""))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: block_trade failed: %s", exc)
        diagnostic = _source_diagnostic("block_trade", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["block_trade"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3e: dragon-tiger abnormal attention ---
    try:
        result = _safe_tool_call(
            tool_registry,
            "get_tushare_dragon_tiger_list",
            trade_date=trade_date,
            stock_code="",
            limit=limit_per_source,
        )
        items = _extract_payload_items(result)
        accepted = 0
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            seed = _dragon_tiger_seed_from_item(
                item,
                market=market,
                rank=rank,
                freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"),
            )
            if seed is None:
                continue
            _collect(seed)
            accepted += 1
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic(
            "dragon_tiger",
            status,
            count=accepted,
            error=str(result.get("error") or ""),
            detail={"trade_date": result.get("trade_date") or trade_date, "raw_count": len(items)},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["dragon_tiger"] = _source_quality(status, freshness=str(result.get("trade_date") or trade_date or "latest_trade_date"), error=str(result.get("error") or ""))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: dragon_tiger failed: %s", exc)
        diagnostic = _source_diagnostic("dragon_tiger", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["dragon_tiger"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3c: valuation/liquidity daily-basic anomaly ---
    try:
        result = _safe_tool_call(
            tool_registry,
            "get_tushare_daily_basic",
            stock_code="",
            trade_date=trade_date,
            limit=max(limit_per_source * 4, 40),
        )
        items = _extract_payload_items(result)
        accepted = 0
        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            seed = _valuation_liquidity_seed_from_item(
                item,
                market=market,
                rank=rank,
                freshness=str(item.get("trade_date") or trade_date or "latest_trade_date"),
            )
            if seed is None:
                continue
            _collect(seed)
            accepted += 1
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic(
            "valuation_liquidity",
            status,
            count=accepted,
            error=str(result.get("error") or ""),
            detail={"trade_date": trade_date, "raw_count": len(items)},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["valuation_liquidity"] = _source_quality(status, freshness=trade_date or "latest_trade_date", error=str(result.get("error") or ""))
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: valuation_liquidity failed: %s", exc)
        diagnostic = _source_diagnostic("valuation_liquidity", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["valuation_liquidity"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 4: limit-up pool ---
    try:
        result = _safe_tool_call(
            tool_registry, "get_tushare_limit_list_d",
            trade_date=trade_date, limit=limit_per_source,
        )
        items = _extract_payload_items(result)
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            # Only include true limit-up (U), exclude limit-down (D)
            if str(item.get("limit_status") or "").upper() != "U":
                continue
            streak = item.get("limit_up_streak") or 1
            _collect(SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market=market,
                source="limit_up_pool",
                hint=f"涨停,连板={streak}",
                trigger_signals=[
                    _build_signal(
                        dimension="technical",
                        signal_type="limit_up",
                        value="U",
                        threshold="U",
                        deviation=streak,
                        label="涨停/连板",
                    )
                ],
                priority_score=78.0 + min(float(_safe_float(streak) or 1.0), 5.0) * 2.0,
                freshness=trade_date or "latest_trade_date",
                context_hint="涨停池在线来源，只代表短线关注度上升，后续必须核验流动性和可参与空间。",
            ))
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic("limit_up_pool", status, count=len(items), detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["limit_up_pool"] = _source_quality(status, freshness=trade_date or "latest_trade_date", error=str(result.get("error") or ""))
    except Exception as exc:
        logger.debug("seed pool: limit_list_d failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("limit_up_pool", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["limit_up_pool"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 3: hot-rank ---
    try:
        result = _safe_tool_call(
            tool_registry, "get_tushare_hot_rank",
            source="ths", trade_date=trade_date, limit=limit_per_source,
        )
        items = _extract_payload_items(result)
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            rank = item.get("rank") or ""
            _collect(SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market=market,
                source="hot_rank",
                hint=f"热榜rank={rank}",
                trigger_signals=[
                    _build_signal(
                        dimension="attention",
                        signal_type="hot_rank",
                        value=rank,
                        threshold=limit_per_source,
                        deviation=max(0, limit_per_source - int(_safe_float(rank) or limit_per_source)),
                        label="市场关注度",
                    )
                ],
                priority_score=72.0 + max(0.0, min(20.0, (limit_per_source - (_safe_float(rank) or limit_per_source)) * 1.2)),
                freshness=trade_date or "latest_trade_date",
                context_hint="热榜在线来源，只代表关注度异常，不直接代表方向。",
            ))
        status = str(result.get("status") or ("ok" if items else "empty")).lower()
        diagnostic = _source_diagnostic("hot_rank", status, count=len(items), detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["hot_rank"] = _source_quality(status, freshness=trade_date or "latest_trade_date", error=str(result.get("error") or ""))
    except Exception as exc:
        logger.debug("seed pool: hot_rank failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("hot_rank", "failed", error=message, detail={"trade_date": trade_date})
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["hot_rank"] = _source_quality("failed", freshness=trade_date or "latest_trade_date", error=message)

    # --- Source 4a: AlphaSift ---
    try:
        from src.agent.candidate_providers.alphasift_provider import AlphaSiftCandidateProvider
        alpha_result = AlphaSiftCandidateProvider().discover(limit=limit_per_source)
        alpha_candidates = alpha_result.get("candidates") or []
        for cand in alpha_result.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            seed = _seed_from_candidate(
                cand,
                market=market,
                source="alphasift",
                fallback_hint="AlphaSift YAML 多因子策略命中",
                default_dimension="technical",
                default_signal_type="alphasift_strategy",
            )
            if seed is not None:
                _collect(seed)
        status = str(alpha_result.get("status") or ("ok" if alpha_candidates else "empty")).lower()
        diagnostic = _source_diagnostic(
            "alphasift",
            status,
            count=len(alpha_candidates),
            error=str(alpha_result.get("error") or ""),
            detail={"latest_date": alpha_result.get("latest_date"), "db_path": alpha_result.get("db_path")},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["alphasift"] = _source_quality(status, freshness=str(alpha_result.get("latest_date") or "latest_local"), error=str(alpha_result.get("error") or ""))
    except Exception as exc:
        logger.debug("seed pool: alphasift failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("alphasift", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["alphasift"] = _source_quality("failed", error=message)

    # --- Source 4b: Sequoia ---
    try:
        from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
        seq_result = SequoiaCandidateProvider().discover(limit=limit_per_source)
        seq_candidates = seq_result.get("candidates") or []
        for cand in seq_result.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            seed = _seed_from_candidate(
                cand,
                market=market,
                source="sequoia",
                fallback_hint="Sequoia 量化形态策略命中",
                default_dimension="technical",
                default_signal_type="sequoia_strategy",
            )
            if seed is not None:
                _collect(seed)
        status = str(seq_result.get("status") or ("ok" if seq_candidates else "empty")).lower()
        diagnostic = _source_diagnostic(
            "sequoia",
            status,
            count=len(seq_candidates),
            error=str(seq_result.get("error") or ""),
            detail={"latest_date": seq_result.get("latest_date"), "db_path": seq_result.get("db_path")},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["sequoia"] = _source_quality(status, freshness=str(seq_result.get("latest_date") or "latest_local"), error=str(seq_result.get("error") or ""))
    except Exception as exc:
        logger.debug("seed pool: sequoia failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("sequoia", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["sequoia"] = _source_quality("failed", error=message)

    # --- Source 5a: fundamental low-base seeds ---
    try:
        from src.agent.candidate_providers.fundamental_provider import FundamentalCandidateProvider

        fundamental_result = FundamentalCandidateProvider().discover(limit=limit_per_source)
        fundamental_candidates = fundamental_result.get("candidates") or []
        accepted_count = 0
        for cand in fundamental_candidates:
            if not isinstance(cand, dict):
                continue
            code = str(cand.get("code") or "").strip()
            if not code:
                continue
            metrics = cand.get("metrics") if isinstance(cand.get("metrics"), dict) else {}
            pe_ttm = metrics.get("pe_ttm")
            pb = metrics.get("pb")
            revenue_growth = metrics.get("revenue_growth")
            profit_growth = metrics.get("profit_growth")
            quality_score = metrics.get("quality_score")
            value_score = metrics.get("value_score")
            if (
                (_safe_float(revenue_growth) or 0.0) <= 0
                and (_safe_float(profit_growth) or 0.0) <= 0
            ):
                continue
            if (_safe_float(pe_ttm) or 999.0) > 60.0 and (_safe_float(pb) or 999.0) > 8.0:
                continue
            hint_parts = []
            if _safe_float(revenue_growth) is not None:
                hint_parts.append(f"营收增速={_safe_float(revenue_growth):.1f}%")
            if _safe_float(profit_growth) is not None:
                hint_parts.append(f"利润增速={_safe_float(profit_growth):.1f}%")
            if _safe_float(quality_score) is not None:
                hint_parts.append(f"质量分={_safe_float(quality_score):.1f}")
            if _safe_float(value_score) is not None:
                hint_parts.append(f"价值分={_safe_float(value_score):.1f}")
            _collect(
                SeedItem(
                    code=code,
                    name=str(cand.get("name") or code),
                    market=market,
                    source="fundamental_snapshot",
                    hint="；".join(hint_parts) or "成长改善但估值未明显扩张",
                    trigger_signals=[
                        _build_signal(
                            dimension="fundamental",
                            signal_type="growth_quality_value",
                            value={
                                "revenue_growth": revenue_growth,
                                "profit_growth": profit_growth,
                                "pe_ttm": pe_ttm,
                                "pb": pb,
                            },
                            threshold={"growth": ">0", "valuation": "pe<=60 or pb<=8"},
                            label="基本面改善",
                        )
                    ],
                    priority_score=_safe_float(cand.get("signal_score")) or _safe_float(cand.get("screen_score")) or 68.0,
                    freshness=str(fundamental_result.get("updated_at") or fundamental_result.get("latest_period") or "snapshot"),
                    context_hint="本地预计算基本面快照命中，适合作为低位/修复候选补充。",
                    extras={"metrics": metrics, "latest_period": fundamental_result.get("latest_period")},
                )
            )
            accepted_count += 1
        status = str(fundamental_result.get("status") or ("ok" if fundamental_candidates else "empty")).lower()
        diagnostic = _source_diagnostic(
            "fundamental_snapshot",
            status,
            count=accepted_count,
            error=str(fundamental_result.get("error") or ""),
            detail={"latest_period": fundamental_result.get("latest_period"), "updated_at": fundamental_result.get("updated_at")},
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["fundamental_snapshot"] = _source_quality(status, freshness=str(fundamental_result.get("updated_at") or fundamental_result.get("latest_period") or "snapshot"), error=str(fundamental_result.get("error") or ""))
    except Exception as exc:
        logger.debug("seed pool: fundamental_snapshot failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("fundamental_snapshot", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["fundamental_snapshot"] = _source_quality("failed", error=message)

    # --- Source 5b: low-base structure seeds from shared daily DB ---
    try:
        structure_candidates = _build_low_base_structure_seeds(limit=limit_per_source)
        for seed in structure_candidates:
            _collect(seed)
        diagnostic = _source_diagnostic("low_base_structure", "ok" if structure_candidates else "empty", count=len(structure_candidates))
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["low_base_structure"] = _source_quality("ok" if structure_candidates else "empty", freshness="latest_local")
    except Exception as exc:
        logger.debug("seed pool: low_base_structure failed: %s", exc)
        message = f"{type(exc).__name__}: {exc}"
        diagnostic = _source_diagnostic("low_base_structure", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["low_base_structure"] = _source_quality("failed", error=message)

    # --- Source 6: deterministic auto candidates as richness supplement ---
    # This reuses the broader existing L1 discovery stack (event/news/sector where
    # available) as an online supplement, but keeps source caps and hard filters
    # in this committee seed builder.
    try:
        result = _safe_tool_call(
            tool_registry,
            "discover_watchlist_candidates",
            market=market,
            seed_symbols=[],
            limit=min(24, max(8, limit_per_source)),
            candidate_source="auto",
        )
        auto_candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        supplement_count = 0
        for cand in auto_candidates:
            if not isinstance(cand, dict):
                continue
            raw_source = str(cand.get("source") or cand.get("candidate_source") or "").strip()
            if raw_source.startswith("event_impact"):
                source = "event_impact"
                default_dimension = "news_event"
                signal_type = "event_impact"
            elif raw_source.startswith("news_momentum"):
                source = "news_momentum"
                default_dimension = "news_event"
                signal_type = "news_momentum"
            elif raw_source.startswith("sector"):
                source = "sector_theme"
                default_dimension = "sector_theme"
                signal_type = "sector_theme"
            elif raw_source.startswith("alphasift"):
                source = "alphasift"
                default_dimension = "technical"
                signal_type = "alphasift_strategy"
            elif raw_source.startswith("sequoia"):
                source = "sequoia"
                default_dimension = "technical"
                signal_type = "sequoia_strategy"
            else:
                continue
            seed = _seed_from_candidate(
                cand,
                market=market,
                source=source,
                fallback_hint=str(cand.get("reason") or "discover_watchlist_candidates auto supplement"),
                default_dimension=default_dimension,
                default_signal_type=signal_type,
                freshness=str(cand.get("latest_date") or "online_or_cached"),
            )
            if seed is None:
                continue
            seed.extras["supplement_source"] = raw_source
            _collect(seed)
            supplement_count += 1
        status = str(result.get("status") or ("ok" if auto_candidates else "empty")).lower()
        diagnostic = _source_diagnostic(
            "discover_watchlist_candidates_auto",
            status,
            count=supplement_count,
            error=str(result.get("error") or ""),
        )
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["discover_watchlist_candidates_auto"] = _source_quality(
            status,
            freshness="online_or_cached",
            error=str(result.get("error") or ""),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.debug("seed pool: discover_watchlist_candidates_auto failed: %s", exc)
        diagnostic = _source_diagnostic("discover_watchlist_candidates_auto", "failed", error=message)
        diagnostics.append(diagnostic)
        _log_seed_source_diagnostic(diagnostic)
        source_quality["discover_watchlist_candidates_auto"] = _source_quality("failed", freshness="online_or_cached", error=message)

    for source, bucket in list(seeds_by_source.items()):
        seeds_by_source[source] = _dedupe_by_code(bucket)

    result_pool = _assemble_seed_pool(
        seeds_by_source,
        total_limit=total_limit,
        limit_per_source=limit_per_source,
    )
    source_counts = {}
    for item in result_pool:
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
    hard_exclusion = {
        "excluded_count": int(sum(exclusion_counts.values())) + int(local_hard_exclusion.get("excluded_symbols") or 0),
        "post_source_excluded_count": sum(exclusion_counts.values()),
        "post_source_reasons": exclusion_counts,
        "local_universe": local_hard_exclusion,
    }
    logger.info(
        "seed pool built: total=%d sources=%s hard_exclusion=%s elapsed_ms=%d preview=%s",
        len(result_pool),
        source_counts,
        hard_exclusion,
        int((time.time() - started) * 1000),
        _seed_log_preview(result_pool),
    )
    return SeedPoolBuildResult(
        seeds=result_pool,
        diagnostics=diagnostics,
        hard_exclusion=hard_exclusion,
        source_quality=source_quality,
        total_limit=total_limit,
        market_regime=market_regime,
    )

def _build_daily_screener_seeds(*, trade_date: str, limit: int) -> Tuple[List[SeedItem], Dict[str, Any]]:
    """Post-close screener — SQLite-first, Tushare as lightweight supplement.

    Execution order (to minimise network calls):
    1. Load last 22 trading days from local SQLite for ALL A-shares (fast, offline).
    2. Apply pct_chg / volume_ratio / 3-day volume trend / MA5/10/20 filters entirely
       from SQLite data.  volume_ratio is approximated as today's volume divided by
       the 5-day average of the previous 5 sessions.
    3. If Tushare token is available, call daily_basic ONLY for the stocks that
       survived step 2 (typically ≤100 stocks) to get turnover_rate_f and circ_mv,
       then apply the remaining two filters.  If the call fails, skip those two
       filters and return the step-2 results with a warning.
    4. Tushare index_daily (000001.SH) is still used for the market-regime check
       (relax upper bound to 7% if Shanghai Composite up >10%).

    Filter criteria:
    1. Daily gain 3%–5% (7% if Shanghai Composite pct_chg > 10%)
    2. Volume ratio (量比) > 1  — computed from SQLite volume history
    3. Volume rising 3 consecutive trading days (including today)
    4. MA5, MA10, MA20 all trending upward (today > yesterday)
    5. Free-float turnover rate 5%–12%  — from Tushare daily_basic (optional)
    6. Circulating market cap 50亿–700亿  — from Tushare daily_basic (optional)
    """
    import os
    import sqlite3
    from pathlib import Path

    import pandas as pd

    SOURCE = "daily_screener"

    if not trade_date:
        return [], _source_diagnostic(SOURCE, "unavailable", error="trade_date not resolved")

    # ── Step 1: open local SQLite DB ──────────────────────────────────────────
    db_path = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "Sequoia-X/data/sequoia_v2.db"
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return [], _source_diagnostic(SOURCE, "unavailable", error=f"stock_daily DB not found: {path}")

    try:
        with sqlite3.connect(str(path)) as conn:
            # Determine the cutoff date for the last 22 distinct trading sessions
            cutoff_row = conn.execute(
                "SELECT MIN(date) FROM (SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 22)"
            ).fetchone()
            cutoff = cutoff_row[0] if cutoff_row and cutoff_row[0] else None
            if not cutoff:
                return [], _source_diagnostic(SOURCE, "empty", detail={"reason": "stock_daily has fewer than 22 trading days"})

            hist_df = pd.read_sql(
                "SELECT symbol, date, close, volume FROM stock_daily WHERE date >= ? ORDER BY symbol, date",
                conn,
                params=[cutoff],
            )
    except Exception as exc:
        return [], _source_diagnostic(SOURCE, "failed", error=f"SQLite read error: {exc}")

    if hist_df.empty:
        return [], _source_diagnostic(SOURCE, "empty", detail={"reason": "stock_daily returned no rows"})

    for col in ("close", "volume"):
        hist_df[col] = pd.to_numeric(hist_df[col], errors="coerce")
    hist_df = hist_df.dropna(subset=["symbol", "date", "close", "volume"])
    hist_df = hist_df[hist_df["volume"] > 0]
    hist_df = hist_df.sort_values(["symbol", "date"])

    total_symbols = hist_df["symbol"].nunique()

    # ── Step 2: market regime check (Tushare index_daily, tiny call) ─────────
    upper_pct = 5.0
    try:
        from data_provider.tushare_client import get_tushare_token, query_tushare_api
        if get_tushare_token():
            idx_df = query_tushare_api(
                "index_daily",
                {"ts_code": "000001.SH", "trade_date": trade_date.replace("-", "")},
                "ts_code,trade_date,pct_chg",
                timeout=8,
            )
            if idx_df is not None and not idx_df.empty:
                sh_pct = float(pd.to_numeric(idx_df.iloc[0].get("pct_chg"), errors="coerce") or 0)
                if sh_pct > 10.0:
                    upper_pct = 7.0
    except Exception:
        pass

    # ── Step 3: SQLite-based filters (pct_chg, volume_ratio, volume trend, MA) ─
    pre_filter_candidates: List[Dict] = []

    for symbol, group in hist_df.groupby("symbol", sort=False):
        symbol = str(symbol)
        grp = group.reset_index(drop=True)

        # need at least 22 bars: MA20 (20) + 1 prev day + 1 today
        if len(grp) < 22:
            continue

        closes = grp["close"].to_numpy(dtype=float)
        volumes = grp["volume"].to_numpy(dtype=float)

        prev_close = closes[-2]
        today_close = closes[-1]
        if prev_close <= 0:
            continue

        # Filter 1: daily gain 3%–upper_pct
        pct_chg = (today_close - prev_close) / prev_close * 100.0
        if not (3.0 <= pct_chg <= upper_pct):
            continue

        # Filter 2: volume_ratio > 1  (today / 5-session average of prior 5 days)
        if len(volumes) < 6:
            continue
        avg_vol_5d = float(volumes[-6:-1].mean())
        vr = float(volumes[-1]) / avg_vol_5d if avg_vol_5d > 0 else 0.0
        if vr <= 1.0:
            continue

        # Filter 3: volume rising 3 consecutive days including today
        if not (volumes[-3] < volumes[-2] < volumes[-1]):
            continue

        # Filter 4: MA5/10/20 all upward
        ma5_t  = float(closes[-5:].mean())
        ma5_p  = float(closes[-6:-1].mean())
        ma10_t = float(closes[-10:].mean())
        ma10_p = float(closes[-11:-1].mean())
        ma20_t = float(closes[-20:].mean())
        ma20_p = float(closes[-21:-1].mean())
        if not (ma5_t > ma5_p and ma10_t > ma10_p and ma20_t > ma20_p):
            continue

        pre_filter_candidates.append({
            "symbol": symbol,
            "pct_chg": pct_chg,
            "volume_ratio": vr,
            "ma5_t": ma5_t, "ma5_p": ma5_p,
            "ma10_t": ma10_t, "ma10_p": ma10_p,
            "ma20_t": ma20_t, "ma20_p": ma20_p,
            "prev_close": prev_close,
        })

    if not pre_filter_candidates:
        return [], _source_diagnostic(
            SOURCE, "empty",
            detail={"trade_date": trade_date, "upper_pct": upper_pct,
                    "scanned_symbols": total_symbols, "reason": "no stocks passed SQLite filters"},
        )

    # ── Step 4: optional Tushare supplement — turnover_rate_f + circ_mv ──────
    # Call daily_basic only for the small set that passed SQLite filters
    tushare_lookup: Dict[str, Dict[str, float]] = {}
    tushare_used = False
    tushare_warning = ""

    pre_symbols = [c["symbol"] for c in pre_filter_candidates]
    pre_ts_codes = ",".join(
        (s + ".SH" if s.startswith(("6", "9")) else s + ".SZ") for s in pre_symbols
    )
    try:
        from data_provider.tushare_client import get_tushare_token, query_tushare_api
        if get_tushare_token():
            basic_df = query_tushare_api(
                "daily_basic",
                {"trade_date": trade_date.replace("-", ""), "ts_code": pre_ts_codes},
                "ts_code,turnover_rate_f,circ_mv",
                timeout=15,
            )
            if basic_df is not None and not basic_df.empty:
                for col in ("turnover_rate_f", "circ_mv"):
                    basic_df[col] = pd.to_numeric(basic_df[col], errors="coerce")
                basic_df["symbol"] = basic_df["ts_code"].astype(str).str.split(".").str[0]
                tushare_lookup = (
                    basic_df.set_index("symbol")[["turnover_rate_f", "circ_mv"]]
                    .to_dict("index")
                )
                tushare_used = True
    except Exception as exc:
        tushare_warning = f"daily_basic supplement skipped: {exc}"

    # ── Step 5: build SeedItems, apply turnover/circ_mv if Tushare data present ─
    seeds: List[SeedItem] = []

    for cand in pre_filter_candidates:
        symbol = cand["symbol"]
        pct_chg = cand["pct_chg"]
        vr = cand["volume_ratio"]
        prev_close = cand["prev_close"]
        ma5_t, ma5_p = cand["ma5_t"], cand["ma5_p"]
        ma10_t, ma10_p = cand["ma10_t"], cand["ma10_p"]
        ma20_t, ma20_p = cand["ma20_t"], cand["ma20_p"]

        info = tushare_lookup.get(symbol, {})
        tr = _safe_float(info.get("turnover_rate_f")) or 0.0
        circ_mv_wan = _safe_float(info.get("circ_mv")) or 0.0
        circ_yi = circ_mv_wan / 10_000.0  # 万元 → 亿元

        # Filter 5+6: only applied when Tushare data is available
        if tushare_used and symbol in tushare_lookup:
            if not (5.0 <= tr <= 12.0):
                continue
            if not (500_000.0 <= circ_mv_wan <= 7_000_000.0):  # 50亿–700亿 in 万元
                continue

        score = 50.0
        score += (pct_chg - 3.0) / max(upper_pct - 3.0, 1.0) * 10.0
        score += min(10.0, (vr - 1.0) * 4.0)
        if tushare_used and 7.0 <= tr <= 9.0:
            score += 5.0
        ma_slope = (ma5_t - ma5_p) / prev_close * 100
        score += min(5.0, ma_slope * 10)

        hint_parts = [f"涨幅{pct_chg:.1f}%", f"量比{vr:.1f}"]
        if tushare_used and symbol in tushare_lookup:
            hint_parts += [f"换手{tr:.1f}%", f"流通{circ_yi:.0f}亿"]
        hint_parts += ["均线多头", "量能连升3日"]

        seeds.append(
            SeedItem(
                code=symbol,
                name="",
                market="cn",
                source=SOURCE,  # type: ignore[arg-type]
                hint=" ".join(hint_parts),
                trigger_signals=[{
                    "signal_type": "daily_screener",
                    "pct_chg": round(pct_chg, 2),
                    "volume_ratio": round(vr, 2),
                    "turnover_rate_f": round(tr, 2) if tushare_used else None,
                    "circ_mv_yi": round(circ_yi, 1) if tushare_used else None,
                    "ma5_slope": round(ma5_t - ma5_p, 4),
                    "ma10_slope": round(ma10_t - ma10_p, 4),
                    "ma20_slope": round(ma20_t - ma20_p, 4),
                    "vol_rising_3d": True,
                    "upper_pct_limit": upper_pct,
                    "tushare_supplement": tushare_used,
                }],
                priority_score=round(min(100.0, score), 2),
                freshness=trade_date,
            )
        )

    seeds.sort(key=lambda x: (-(_safe_float(x.priority_score or 0) or 0.0), str(x.code)))
    selected = seeds[: max(1, int(limit))]

    diag_detail: Dict[str, Any] = {
        "trade_date": trade_date,
        "upper_pct_limit": upper_pct,
        "scanned_symbols": total_symbols,
        "sqlite_passed": len(pre_filter_candidates),
        "passed_all_filters": len(seeds),
        "tushare_supplement": tushare_used,
        "db_path": str(path),
    }
    if tushare_warning:
        diag_detail["tushare_warning"] = tushare_warning

    return selected, _source_diagnostic(
        SOURCE,
        "ok" if selected else "empty",
        count=len(selected),
        detail=diag_detail,
    )


def _build_local_price_volume_seeds(*, limit: int) -> Tuple[List[SeedItem], Dict[str, Any]]:
    import os
    import sqlite3
    from pathlib import Path

    import pandas as pd

    db_path = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "Sequoia-X/data/sequoia_v2.db"
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return [], _source_diagnostic(
            "local_price_volume",
            "unavailable",
            error=f"stock_daily DB not found: {path}",
            detail={"db_path": str(path)},
        )

    with sqlite3.connect(str(path)) as conn:
        df = pd.read_sql(
            "SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily",
            conn,
        )
    if df.empty:
        return [], _source_diagnostic("local_price_volume", "empty", detail={"db_path": str(path)})

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["symbol", "date", "open", "high", "low", "close", "volume"])
    df = df[df["symbol"].str.fullmatch(r"\d{6}", na=False)]
    df = df[df["volume"].fillna(0) > 0]
    if df.empty:
        return [], _source_diagnostic("local_price_volume", "empty", detail={"db_path": str(path)})
    df = df.sort_values(["symbol", "date"])
    latest_date = str(df["date"].max().date()) if not df.empty else ""
    market_regime = _infer_market_regime_from_daily_df(df)

    seeds: List[SeedItem] = []
    scanned = 0
    excluded_counts: Dict[str, int] = {}
    for symbol, group in df.groupby("symbol", sort=False):
        exclusion_reason = _local_daily_hard_exclusion_reason(str(symbol), group.reset_index(drop=True))
        if exclusion_reason:
            excluded_counts[exclusion_reason] = excluded_counts.get(exclusion_reason, 0) + 1
            continue
        scanned += 1
        seed = _local_price_volume_seed_from_bars(
            str(symbol),
            group.reset_index(drop=True),
            market_regime=market_regime,
        )
        if seed is not None:
            seeds.append(seed)

    seeds.sort(key=lambda item: (-(_safe_float(item.priority_score or 0.0) or 0.0), str(item.code)))
    selected = seeds[: max(1, int(limit))]
    return selected, _source_diagnostic(
        "local_price_volume",
        "ok" if selected else "empty",
        count=len(selected),
        detail={
            "universe_symbols": int(df["symbol"].nunique()),
            "scanned_symbols": scanned,
            "excluded_symbols": sum(excluded_counts.values()),
            "exclusion_reasons": excluded_counts,
            "matched_symbols": len(seeds),
            "latest_date": latest_date,
            "db_path": str(path),
            "market_regime": market_regime,
        },
    )


def _local_daily_hard_exclusion_reason(symbol: str, df: Any) -> str:
    if len(df) < LOCAL_HARD_EXCLUSION_MIN_TRADING_DAYS:
        return "new_listing_or_insufficient_history"
    recent20 = df.tail(20)
    if recent20.empty:
        return "insufficient_recent_bars"
    close = _safe_float(recent20.iloc[-1].get("close"))
    volume = _safe_float(recent20.iloc[-1].get("volume"))
    if close is None or close <= 0 or volume is None or volume <= 0:
        return "suspended_or_no_trade"
    avg_turnover = _safe_float(recent20["turnover"].mean()) if "turnover" in recent20 else None
    if avg_turnover is not None and avg_turnover > 0 and avg_turnover < LOCAL_HARD_EXCLUSION_MIN_AVG_TURNOVER:
        return "low_liquidity"
    if _has_consecutive_one_word_limits(df.tail(5)):
        return "consecutive_one_word_limit"
    return ""


def _has_consecutive_one_word_limits(df: Any) -> bool:
    if len(df) < 3:
        return False
    limit_like = 0
    ordered = df.reset_index(drop=True)
    for index in range(1, len(ordered)):
        row = ordered.iloc[index]
        prev_close = _safe_float(ordered.iloc[index - 1].get("close"))
        close = _safe_float(row.get("close"))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if prev_close is None or prev_close <= 0 or close is None or high is None or low is None:
            continue
        pct = (close - prev_close) / prev_close * 100.0
        one_word = abs(high - low) / prev_close < 0.003
        if one_word and abs(pct) >= 9.5:
            limit_like += 1
    return limit_like >= 3


def _infer_market_regime_from_daily_df(df: Any) -> Dict[str, Any]:
    try:
        latest_dates = sorted(df["date"].dropna().unique())[-5:]
        if not latest_dates:
            return {"regime": "unknown", "method": "local_daily_breadth"}
        recent = df[df["date"].isin(latest_dates)].copy()
        recent = recent.sort_values(["symbol", "date"])
        recent["prev_close"] = recent.groupby("symbol")["close"].shift(1)
        recent["pct"] = (recent["close"] - recent["prev_close"]) / recent["prev_close"] * 100.0
        valid = recent.dropna(subset=["pct"])
        up_ratio = float((valid["pct"] > 0).mean()) if not valid.empty else 0.0
        avg_abs_pct = float(valid["pct"].abs().mean()) if not valid.empty else 0.0
        if up_ratio >= 0.58:
            regime = "bullish"
        elif up_ratio <= 0.42:
            regime = "bearish"
        elif avg_abs_pct <= 1.8:
            regime = "range_bound"
        else:
            regime = "neutral"
        return {
            "regime": regime,
            "method": "local_daily_breadth",
            "lookback_days": len(latest_dates),
            "up_ratio": round(up_ratio, 3),
            "avg_abs_pct": round(avg_abs_pct, 3),
        }
    except Exception as exc:
        return {"regime": "unknown", "method": "local_daily_breadth", "error": str(exc)}


def _local_price_volume_seed_from_bars(symbol: str, df: Any, *, market_regime: Optional[Dict[str, Any]] = None) -> Optional[SeedItem]:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    window120 = df.tail(120)
    window60 = df.tail(60)
    window20 = df.tail(20)
    if window60.empty or window20.empty:
        return None

    close = _safe_float(last.get("close"))
    prev_close = _safe_float(prev.get("close"))
    high20 = _safe_float(window20.iloc[:-1]["high"].max()) if len(window20) > 1 else None
    high60 = _safe_float(window60.iloc[:-1]["high"].max()) if len(window60) > 1 else None
    high120 = _safe_float(window120["high"].max())
    low120 = _safe_float(window120["low"].min())
    volume = _safe_float(last.get("volume"))
    vol_ma20 = _safe_float(window20.iloc[:-1]["volume"].mean()) if len(window20) > 1 else None
    turnover = _safe_float(last.get("turnover"))
    turnover_ma20 = _safe_float(window20.iloc[:-1]["turnover"].mean()) if len(window20) > 1 else None
    high = _safe_float(last.get("high"))
    low = _safe_float(last.get("low"))
    ma20 = _safe_float(window20["close"].mean())
    ma60 = _safe_float(window60["close"].mean())
    prev_ma20 = _safe_float(window20.iloc[:-1]["close"].mean()) if len(window20) > 1 else None
    prev_ma60_window = window60.iloc[:-1].tail(60)
    prev_ma60 = _safe_float(prev_ma60_window["close"].mean()) if len(prev_ma60_window) > 1 else None
    range20 = None
    if high is not None and low is not None:
        ranges = (window20["high"] - window20["low"]) / window20["close"].replace(0, math.nan)
        range20 = _safe_float(ranges.mean() * 100)
    if close is None or prev_close is None or prev_close <= 0 or volume is None or vol_ma20 is None or vol_ma20 <= 0:
        return None

    change_pct = (close - prev_close) / prev_close * 100
    volume_ratio = volume / vol_ma20
    turnover_ratio = turnover / turnover_ma20 if turnover is not None and turnover_ma20 and turnover_ma20 > 0 else None
    amplitude = (high - low) / prev_close * 100 if high is not None and low is not None else None
    position120 = None
    if high120 is not None and low120 is not None and high120 > low120:
        position120 = (close - low120) / (high120 - low120)

    signals: List[Dict[str, Any]] = []
    if high20 is not None and close > high20 and volume_ratio >= 1.4:
        signals.append(
            _build_signal(
                dimension="technical",
                signal_type="volume_breakout_20d",
                value=round(close, 3),
                threshold=round(high20, 3),
                deviation=round(volume_ratio, 2),
                label="20日放量突破",
            )
        )
    if high60 is not None and close > high60:
        signals.append(
            _build_signal(
                dimension="technical",
                signal_type="price_breakout_60d",
                value=round(close, 3),
                threshold=round(high60, 3),
                deviation=round((close - high60) / high60 * 100, 2) if high60 else None,
                label="60日价格突破",
            )
        )
    if volume_ratio >= 2.0 and abs(change_pct) >= 2.0:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type="volume_surge",
                value=round(volume_ratio, 2),
                threshold=2.0,
                deviation=round(change_pct, 2),
                label="量能突增",
            )
        )
    if turnover_ratio is not None and turnover_ratio >= 1.8:
        signals.append(
            _build_signal(
                dimension="capital",
                signal_type="turnover_surge",
                value=round(turnover_ratio, 2),
                threshold=1.8,
                deviation=round((turnover or 0.0) - (turnover_ma20 or 0.0), 2),
                label="成交额突增",
            )
        )
    if ma20 is not None and ma60 is not None and prev_ma20 is not None and prev_ma60 is not None:
        if prev_ma20 <= prev_ma60 and ma20 > ma60:
            signals.append(
                _build_signal(
                    dimension="technical",
                    signal_type="ma20_ma60_golden_cross",
                    value=round(ma20, 3),
                    threshold=round(ma60, 3),
                    deviation=round((ma20 - ma60) / ma60 * 100, 2) if ma60 else None,
                    label="MA20/MA60 金叉",
                )
            )
        elif prev_ma20 >= prev_ma60 and ma20 < ma60:
            signals.append(
                _build_signal(
                    dimension="technical",
                    signal_type="ma20_ma60_death_cross",
                    value=round(ma20, 3),
                    threshold=round(ma60, 3),
                    deviation=round((ma20 - ma60) / ma60 * 100, 2) if ma60 else None,
                    label="MA20/MA60 死叉",
                )
            )
    if len(window20) >= 20:
        recent5_vol = _safe_float(window20.tail(5)["volume"].mean())
        recent5_amp = _safe_float(((window20.tail(5)["high"] - window20.tail(5)["low"]) / window20.tail(5)["close"].replace(0, math.nan) * 100).mean())
        prior15_amp = _safe_float(((window20.head(15)["high"] - window20.head(15)["low"]) / window20.head(15)["close"].replace(0, math.nan) * 100).mean())
        if recent5_vol is not None and vol_ma20 and recent5_amp is not None and prior15_amp is not None:
            if recent5_vol <= vol_ma20 * 0.65 and recent5_amp <= max(2.5, prior15_amp * 0.65):
                signals.append(
                    _build_signal(
                        dimension="technical",
                        signal_type="low_volume_coiling",
                        value=round(recent5_vol / vol_ma20, 2),
                        threshold=0.65,
                        deviation=round(recent5_amp, 2),
                        label="缩量蓄势",
                    )
                )
    if position120 is not None and position120 <= 0.45 and 0.5 <= change_pct <= 5.5 and volume_ratio >= 1.2:
        signals.append(
            _build_signal(
                dimension="technical",
                signal_type="low_base_turn_attempt",
                value=round(position120, 3),
                threshold=0.45,
                deviation=round(change_pct, 2),
                label="低位转强尝试",
            )
        )
    if amplitude is not None and amplitude >= 7.0 and volume_ratio >= 1.5:
        signals.append(
            _build_signal(
                dimension="technical",
                signal_type="intraday_volatility_expansion",
                value=round(amplitude, 2),
                threshold=7.0,
                deviation=round(volume_ratio, 2),
                label="波动扩张",
            )
        )
    if abs(change_pct) >= 1.5 and high is not None and low is not None:
        prev_high = _safe_float(prev.get("high"))
        prev_low = _safe_float(prev.get("low"))
        if prev_high is not None and low > prev_high * 1.005:
            signals.append(
                _build_signal(
                    dimension="news_event",
                    signal_type="gap_up",
                    value=round((low - prev_high) / prev_high * 100, 2),
                    threshold=1.5,
                    deviation=round(volume_ratio, 2),
                    label="跳空高开缺口",
                )
            )
        elif prev_low is not None and high < prev_low * 0.995:
            signals.append(
                _build_signal(
                    dimension="news_event",
                    signal_type="gap_down",
                    value=round((high - prev_low) / prev_low * 100, 2),
                    threshold=-1.5,
                    deviation=round(volume_ratio, 2),
                    label="跳空低开缺口",
                )
            )

    if not signals:
        return None

    dimension_count = len({signal.get("dimension") for signal in signals})
    raw_score = 58.0 + min(18.0, max(0.0, volume_ratio - 1.0) * 5.0) + min(12.0, abs(change_pct) * 1.2)
    if dimension_count >= 2:
        raw_score += 8.0
    if dimension_count >= 3:
        raw_score += 5.0
    if position120 is not None and position120 <= 0.45:
        raw_score += 4.0
    if turnover is not None and turnover >= 100_000_000:
        raw_score += 3.0
    regime = str((market_regime or {}).get("regime") or "")
    if regime == "bullish" and any(signal.get("signal_type") in {"volume_breakout_20d", "price_breakout_60d"} for signal in signals):
        raw_score += 3.0
    elif regime == "bearish" and change_pct > 0 and any(signal.get("signal_type") in {"low_base_turn_attempt", "low_volume_coiling"} for signal in signals):
        raw_score += 4.0
    elif regime == "range_bound" and any(signal.get("signal_type") in {"low_volume_coiling", "intraday_volatility_expansion"} for signal in signals):
        raw_score += 3.0
    priority_score = _compress_seed_priority_score(raw_score)
    hint = "；".join(str(signal.get("label") or signal.get("signal_type")) for signal in signals[:4])
    metrics = {
        "change_pct": round(change_pct, 3),
        "volume_ratio": round(volume_ratio, 3),
        "turnover_ratio": round(turnover_ratio, 3) if turnover_ratio is not None else None,
        "turnover": turnover,
        "position_120d": round(position120, 3) if position120 is not None else None,
        "amplitude_pct": round(amplitude, 3) if amplitude is not None else None,
        "market_regime": regime or "unknown",
        "priority_score_raw": round(raw_score, 3),
        "priority_score": priority_score,
        "score_kind": "seed_recall_priority",
    }
    return SeedItem(
        code=symbol,
        name=symbol,
        market="cn",
        source="local_price_volume",
        hint=hint,
        trigger_signals=signals,
        priority_score=priority_score,
        freshness=str(last.get("date").date()) if hasattr(last.get("date"), "date") else str(last.get("date") or "latest_local"),
        context_hint="本地日线全市场价量异常扫描命中，不依赖在线接口。",
        extras={"metrics": metrics},
    )


def _extract_worth(raw_item: Dict[str, Any]) -> bool:
    """从 LLM 返回的 decision 字典中提取 keep/reject 意图。

    接受常见变体字段名，避免因 LLM 返回 decision/accept/keep 等非标准 key
    而静默把所有种子判为 reject。字段全缺时默认保留（宁可多不少）。
    """
    for key in ("worth_deep_analysis", "decision", "accept", "keep", "include", "selected"):
        val = raw_item.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.strip().lower() in ("true", "yes", "keep", "accept", "1", "include", "selected")
    return True  # 字段全缺默认保留


def _seed_for_gate(seed: SeedItem) -> Dict[str, Any]:
    dimensions = sorted(
        {
            str(signal.get("dimension") or "")
            for signal in (seed.trigger_signals or [])
            if isinstance(signal, dict) and str(signal.get("dimension") or "").strip()
        }
    )
    return {
        "code": seed.code,
        "name": seed.name,
        "source": seed.source,
        "hint": seed.hint,
        "priority_score": seed.priority_score,
        "freshness": seed.freshness,
        "dimensions": dimensions,
        "trigger_signals": (seed.trigger_signals or [])[:5],
        "context_hint": seed.context_hint,
    }


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
    return {}

def _humanize_signal_detail(signal: Dict[str, Any]) -> str:
    """Convert structured signal {value, threshold, deviation} into readable Chinese text."""
    sig_type = str(signal.get("signal_type") or "")
    v = signal.get("value")
    t = signal.get("threshold")
    d = signal.get("deviation")
    label = str(signal.get("label") or "")

    def _fmt(x: Any, decimals: int = 2) -> str:
        if x is None:
            return "--"
        if isinstance(x, dict):
            return ";".join(f"{k}={v}" for k, v in list(x.items())[:4])
        try:
            return f"{float(x):,.{decimals}f}"
        except (TypeError, ValueError):
            return str(x)

    def _yi(x: Any) -> str:
        if x is None:
            return "--"
        if isinstance(x, dict):
            return ";".join(f"{k}={v}" for k, v in list(x.items())[:4])
        try:
            return f"{float(x) / 1e8:.2f}亿"
        except (TypeError, ValueError):
            return str(x)

    if sig_type == "volume_breakout_20d":
        return f"收盘价 {_fmt(v)} 突破 20 日高点 {_fmt(t)}，量比 {_fmt(d, 1)}"
    if sig_type == "price_breakout_60d":
        return f"收盘价 {_fmt(v)} 突破 60 日高点 {_fmt(t)}（涨幅 {_fmt(d, 1)}%）"
    if sig_type == "volume_surge":
        return f"量比 {_fmt(v, 1)} 倍（阈值 {_fmt(t, 1)}），涨跌幅 {_fmt(d, 1)}%"
    if sig_type == "turnover_surge":
        return f"成交额达到 20 日均值 {_fmt(v, 1)} 倍（阈值 {_fmt(t, 1)}），超出 {_yi(d)}"
    if sig_type == "intraday_volatility_expansion":
        return f"日内振幅 {_fmt(v, 1)}%（阈值 {_fmt(t, 1)}%），量比 {_fmt(d, 1)}"
    if sig_type == "gap_up":
        return f"跳空缺口 {_fmt(v, 1)}%（阈值 {_fmt(t, 1)}%），量比 {_fmt(d, 1)}"
    if sig_type == "gap_down":
        return f"向下跳空 {_fmt(v, 1)}%（阈值 {_fmt(t, 1)}%），量比 {_fmt(d, 1)}"
    if sig_type == "ma20_ma60_golden_cross":
        return f"MA20（{_fmt(v)}）上穿 MA60（{_fmt(t)}），偏离 {_fmt(d, 1)}%"
    if sig_type == "ma20_ma60_death_cross":
        return f"MA20（{_fmt(v)}）下穿 MA60（{_fmt(t)}），偏离 {_fmt(d, 1)}%"
    if sig_type == "low_volume_coiling":
        return f"近 5 日成交量仅为 20 日均量的 {_fmt(v, 0)}%，振幅收窄至 {_fmt(d, 1)}%"
    if sig_type == "low_base_turn_attempt":
        return f"120 日区间位置 {_fmt(v, 0)}%（低于 45%），涨幅 {_fmt(d, 1)}%，量比 ≥1.2"
    if sig_type == "low_base_structure":
        return f"低位结构评分 {_fmt(v, 1)}（阈值 {_fmt(t, 1)}）"
    if sig_type == "turnover_repair":
        return f"换手修复信号：当前 {_fmt(v, 1)}（阈值 {_fmt(t, 1)}）"
    if sig_type == "limit_up":
        return f"涨停，涨幅 {_fmt(v, 1)}%"
    if sig_type == "hot_rank":
        return f"热度排名第 {_fmt(v, 0)}"
    if "net_inflow" in sig_type:
        direction = "流入" if (v or 0) >= 0 else "流出"
        return f"主力资金净{direction} {_yi(v)}（阈值 {_yi(t)}）"
    if "turnover" in sig_type and "rate" not in sig_type:
        return f"成交额 {_yi(v)}"
    if "northbound" in sig_type:
        return f"北向资金 {_yi(v)}"
    if "margin_financing" in sig_type:
        return f"融资余额 {_yi(v)}"
    if "block_trade" in sig_type:
        return f"大宗交易 {_yi(v)}"
    if "dragon_tiger" in sig_type:
        if "turnover" in sig_type:
            return f"龙虎榜成交额 {_yi(v)}"
        return f"登上龙虎榜"
    if "daily_basic_volume_ratio" in sig_type:
        return f"量比 {_fmt(v, 1)}（阈值 {_fmt(t, 1)}）"
    if "daily_basic_turnover_rate" in sig_type:
        return f"换手率 {_fmt(v, 1)}%（阈值 {_fmt(t, 1)}%）"
    if "valuation_liquidity_balance" in sig_type:
        if isinstance(v, dict):
            pe_s = f"PE={_fmt(v.get('pe'), 1)}" if v.get("pe") is not None else ""
            pb_s = f"PB={_fmt(v.get('pb'), 2)}" if v.get("pb") is not None else ""
            tr_s = f"换手={_fmt(v.get('turnover_rate'), 2)}%" if v.get("turnover_rate") is not None else ""
            return "；".join(s for s in [pe_s, pb_s, tr_s] if s) or "估值/流动性平衡"
        return f"估值流动性平衡分 {_fmt(v, 1)}"
    if "growth_quality_value" in sig_type:
        if isinstance(v, dict):
            rg = v.get("revenue_growth")
            pg = v.get("profit_growth")
            parts_g = []
            if rg is not None:
                parts_g.append(f"营收增速={_fmt(rg, 1)}%")
            if pg is not None:
                parts_g.append(f"利润增速={_fmt(pg, 1)}%")
            return "；".join(parts_g) if parts_g else "成长改善"
        return f"成长质量评分 {_fmt(v, 1)}（阈值 {_fmt(t, 1)}）"
    if v is not None and isinstance(v, str):
        return str(v)
    parts = []
    if v is not None:
        parts.append(f"当前值 {_fmt(v)}")
    if t is not None:
        parts.append(f"阈值 {_fmt(t)}")
    if d is not None:
        parts.append(f"偏离 {_fmt(d)}")
    return "；".join(parts) if parts else (label or "暂无详情")


def _seed_to_candidate_payload(seed: SeedItem) -> Dict[str, Any]:
    dimensions: List[Dict[str, Any]] = []
    for signal in seed.trigger_signals or []:
        if not isinstance(signal, dict):
            continue
        dimension = str(signal.get("dimension") or "strategy").strip() or "strategy"
        label = str(signal.get("label") or signal.get("signal_type") or dimension).strip()
        detail = _humanize_signal_detail(signal)
        dimensions.append({
            "dimension": dimension,
            "label": label,
            "detail": detail,
        })
    recall_sources = seed.extras.get("recall_sources") if isinstance(seed.extras, dict) else None
    return {
        "code": seed.code,
        "name": seed.name or seed.code,
        "market": seed.market,
        "source": seed.source,
        "candidate_source": "llm_expert_committee_seed_pool",
        "reason": seed.context_hint or seed.hint,
        "signal_score": seed.priority_score,
        "score": seed.priority_score,
        "priority_score": seed.priority_score,
        "score_kind": "seed_recall_priority",
        "score_label": "入池优先级",
        "score_note": "L1 种子池召回排序分，不代表买入推荐分或最终决策分。",
        "reason_dimensions": dimensions[:8],
        "recall_sources": list(recall_sources or [seed.source]),
        "matched_strategies": seed.extras.get("matched_strategies", []) if isinstance(seed.extras, dict) else [],
        "strategy_tags": seed.extras.get("strategy_tags", []) if isinstance(seed.extras, dict) else [],
        "trigger_signals": seed.trigger_signals,
        "freshness": seed.freshness,
        "context_hint": seed.context_hint,
        "metrics": seed.extras.get("metrics", {}) if isinstance(seed.extras, dict) else {},
        "seed_gate": seed.extras.get("seed_gate") if isinstance(seed.extras, dict) else None,
    }


def _build_low_base_structure_seeds(*, limit: int) -> List[SeedItem]:
    import os
    import sqlite3
    from pathlib import Path

    import pandas as pd

    db_path = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "Sequoia-X/data/sequoia_v2.db"
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return []

    with sqlite3.connect(str(path)) as conn:
        df = pd.read_sql(
            "SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily",
            conn,
        )
    if df.empty:
        return []

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["symbol", "date", "open", "high", "low", "close"])
    df = df[df["symbol"].str.fullmatch(r"\d{6}", na=False)]
    df = df.sort_values(["symbol", "date"])

    seeds: List[SeedItem] = []
    for symbol, group in df.groupby("symbol", sort=False):
        if len(group) < 80:
            continue
        seed = _low_base_seed_from_bars(str(symbol), group.reset_index(drop=True))
        if seed is not None:
            seeds.append(seed)
        if len(seeds) >= max(1, int(limit)):
            break
    return seeds


def _low_base_seed_from_bars(symbol: str, df: Any) -> Optional[SeedItem]:
    last = df.iloc[-1]
    window120 = df.tail(120)
    window60 = df.tail(60)
    window30 = df.tail(30)
    window20 = df.tail(20)
    if window120.empty or window60.empty or window30.empty or window20.empty:
        return None

    current_close = _safe_float(last["close"])
    high120 = _safe_float(window120["high"].max())
    low120 = _safe_float(window120["low"].min())
    if current_close is None or high120 is None or low120 is None or high120 <= low120:
        return None

    relative_position = (current_close - low120) / (high120 - low120)
    if relative_position > 0.65:
        return None

    prior_high = _safe_float(window60.iloc[:-1]["high"].max()) if len(window60) > 1 else None
    if prior_high is None or prior_high <= 0:
        return None

    test_threshold = prior_high * 0.985
    attack_days = window20[window20["high"] >= test_threshold]
    if len(attack_days) < 2:
        return None

    retreat_pct = (
        (attack_days["high"] - attack_days["close"]).abs() / attack_days["high"].replace(0, math.nan) * 100
    ).fillna(999)
    if (retreat_pct > 5.0).sum() >= len(attack_days):
        return None

    turnover_mean = _safe_float(window20["turnover"].mean())
    latest_turnover = _safe_float(last.get("turnover"))
    if turnover_mean is None or latest_turnover is None or latest_turnover < turnover_mean * 0.8:
        return None

    hint = (
        f"120日位置={relative_position:.2f}；"
        f"近20日试探压力位{len(attack_days)}次；"
        f"前高≈{prior_high:.2f}"
    )
    return SeedItem(
        code=symbol,
        name=symbol,
        market="cn",
        source="low_base_structure",
        hint=hint,
        trigger_signals=[
            _build_signal(
                dimension="technical",
                signal_type="low_base_structure",
                value=round(relative_position, 3),
                threshold=0.65,
                deviation=len(attack_days),
                label="低位结构试探",
            ),
            _build_signal(
                dimension="capital",
                signal_type="turnover_repair",
                value=round(latest_turnover, 2) if latest_turnover is not None else None,
                threshold=round(turnover_mean * 0.8, 2) if turnover_mean is not None else None,
                deviation=round(latest_turnover / turnover_mean, 2) if turnover_mean and latest_turnover is not None else None,
                label="量能修复",
            ),
        ],
        priority_score=74.0 + min(10.0, len(attack_days) * 2.0) + max(0.0, (0.65 - relative_position) * 10.0),
        freshness=str(last.get("date").date()) if hasattr(last.get("date"), "date") else str(last.get("date") or "latest_local"),
        context_hint="低位结构扫描命中，用于保障 early_turn 专家看到低位启动样本。",
        extras={
            "metrics": {
                "position_120d": round(relative_position, 3),
                "attack_days_20d": int(len(attack_days)),
                "prior_high": round(prior_high, 3),
                "turnover_ratio": round(latest_turnover / turnover_mean, 3) if turnover_mean and latest_turnover is not None else None,
            }
        },
    )


def run_committee_discovery(
    *,
    market: str,
    seed_symbols: Sequence[str],
    limit: int,
    tool_registry: Any,
    llm_adapter: Any,
    today: Optional[str] = None,
    deterministic_fn: Optional[Callable[..., Dict[str, Any]]] = None,  # kept for test injection; not used in production
    tool_decls: Optional[Sequence[Dict[str, Any]]] = None,
    overall_timeout_s: float = CommitteeOverallTimeoutSeconds,
    prebuilt_seeds: Optional[Sequence[SeedItem]] = None,
    seed_pool_result: Optional[SeedPoolBuildResult] = None,
    enable_seed_gate: bool = True,
) -> Dict[str, Any]:
    """Run the LLM expert committee and return a discover-compatible payload.

    Args:
        market: Market code (currently only "cn" is fully supported).
        seed_symbols: User-provided seed codes; used only if prebuilt_seeds is None.
        limit: Unused in LLM-only mode; kept for API compatibility.
        tool_registry: Tool registry (dict[str, callable]) compatible with BaseExpert.
        llm_adapter: Either an ``LLMCallable``-shaped function or an object
            with ``.chat(messages, tools=...)``.
        today: Optional ``YYYYMMDD`` date string passed to the capital expert
            prompt for trading-date binding.
        deterministic_fn: Deprecated; kept only for test injection compatibility.
            No longer called in the production path.
        tool_decls: OpenAI-style tool declarations passed to the LLM. When
            omitted, an empty list is used (the LLM will not be able to
            invoke tools — useful for tests).
        overall_timeout_s: Soft wall-clock budget for the LLM expert.
        prebuilt_seeds: Pre-built seed pool (SeedItem list). When provided,
            seed_symbols is ignored. Use this to pass a multi-source seed pool
            built by the caller before entering committee mode.
        seed_pool_result: Optional rich seed-pool build result carrying source
            diagnostics and quality metadata. Prefer this over prebuilt_seeds.
        enable_seed_gate: Whether to run the no-tool LLM noise gate before
            dimension experts. Gate failure falls back to deterministic seeds.
    """

    started = time.time()
    market_value = (market or "cn").strip().lower() or "cn"

    # Keep the deterministic shared seed pool as provenance only. The three
    # thesis desks are the candidate gate; if they do not produce candidates,
    # downstream stages must see a real failure instead of a seed fallback.
    deterministic_payload: Dict[str, Any] = {
        "status": "ok",
        "market": market_value,
        "candidates": [],
        "candidate_count": 0,
        "candidate_source": "llm_expert_committee",
        "discovery_steps": [],
        "next_required_tools": [],
    }

    build_result: Optional[SeedPoolBuildResult] = seed_pool_result
    if build_result is not None:
        seeds = list(build_result.seeds)
    elif prebuilt_seeds is not None:
        seeds = list(prebuilt_seeds)
    else:
        seeds = _to_seed_items(seed_symbols, market_value)
    deterministic_payload["seed_pool_summary"] = _seed_pool_summary(seeds, total_limit=SEED_GATE_OUTPUT_LIMIT)
    if build_result is not None:
        deterministic_payload["seed_pool_diagnostics"] = build_result.diagnostics
        deterministic_payload["seed_pool_hard_exclusion"] = build_result.hard_exclusion
        deterministic_payload["seed_source_quality"] = build_result.source_quality
        deterministic_payload["seed_market_regime"] = build_result.market_regime
    deterministic_payload["candidates"] = []
    deterministic_payload["candidate_count"] = 0
    logger.info(
        "committee discovery seeds prepared: count=%d sources=%s build_diagnostics=%d preview=%s",
        len(seeds),
        _seed_source_counts(seeds),
        len(build_result.diagnostics) if build_result is not None else 0,
        _seed_log_preview(seeds),
    )

    prompt_variables: Dict[str, Any] = {
        "seed_codes": ",".join(item.code for item in seeds),
    }
    if today:
        prompt_variables["today"] = today

    try:
        llm_callable = _coerce_llm_callable(llm_adapter)
        budget_remaining = max(0.0, overall_timeout_s - (time.time() - started))
        if budget_remaining <= 0:
            raise TimeoutError("committee overall_timeout_s exhausted before experts ran")

        regime = build_result.market_regime if build_result is not None else "unknown"
        thesis_result = run_thesis_desk_committee(
            market=market_value,
            seed_symbols=[],
            tool_registry=tool_registry,
            llm_adapter=llm_callable,
            regime=regime,
            today=today,
            tool_decls=tool_decls,
            overall_timeout_s=budget_remaining,
            seed_pool_result=build_result,
            prebuilt_seeds=seeds if build_result is None else None,
        )
        # Merge thesis result into payload while preserving seed-pool diagnostics.
        # Empty desk output is a hard candidate-discovery failure, not permission
        # to promote raw seeds into L1 candidates.
        thesis_candidates = thesis_result.get("candidates")
        if not isinstance(thesis_candidates, list):
            thesis_candidates = []
        _SKIP_MERGE = {"market", "status", "candidates", "candidate_count"}
        for k, v in thesis_result.items():
            if k not in _SKIP_MERGE:
                deterministic_payload[k] = v
        if thesis_candidates:
            deterministic_payload["candidates"] = thesis_candidates
            deterministic_payload["candidate_count"] = len(thesis_candidates)
        else:
            deterministic_payload["candidates"] = []
            deterministic_payload["candidate_count"] = 0
            deterministic_payload["status"] = "failed"
            deterministic_payload["discovery_steps"] = [
                *(deterministic_payload.get("discovery_steps") or []),
                {
                    "source": "thesis_desk_committee",
                    "status": "failed",
                    "dimension": "committee",
                    "error": "thesis_desk_committee returned no candidates",
                    "fallback": False,
                },
            ]
            deterministic_payload["llm_expert_committee"] = {
                "status": "failed",
                "seed_count": len(seeds),
                "candidate_count": 0,
                "degraded": False,
                "dimensions_covered": [],
                "delegate": "thesis_desk_committee",
                "error": "thesis_desk_committee returned no candidates",
                "fallback": False,
            }
            deterministic_payload["thesis_desk_committee"] = {
                "status": "failed",
                "candidate_count": 0,
                "degraded": False,
                "diagnostics": thesis_result.get("thesis_desk_diagnostics") or [],
                "recall_total_in": thesis_result.get("recall_total_in"),
                "recall_total_kept": thesis_result.get("recall_total_kept"),
                "elapsed_ms": thesis_result.get("thesis_desk_committee_elapsed_ms"),
                "error": "thesis_desk_committee returned no candidates",
                "fallback": False,
            }
            deterministic_payload["candidate_source"] = "llm_expert_committee"
            deterministic_payload["committee_elapsed_ms"] = int((time.time() - started) * 1000)
            return deterministic_payload
        # Backward-compat key so callers checking "llm_expert_committee" still see a result
        deterministic_payload["llm_expert_committee"] = {
            "status": thesis_result.get("status", "ok"),
            "seed_count": len(seeds),
            "candidate_count": len(thesis_candidates),
            "degraded": not bool(thesis_candidates),
            "dimensions_covered": ["early_turn_desk", "momentum_desk", "quality_repair_desk"],
            "delegate": "thesis_desk_committee",
        }
        deterministic_payload["thesis_desk_committee"] = {
            "status": thesis_result.get("status", "ok"),
            "candidate_count": len(thesis_candidates),
            "degraded": not bool(thesis_candidates),
            "diagnostics": thesis_result.get("thesis_desk_diagnostics") or [],
            "recall_total_in": thesis_result.get("recall_total_in"),
            "recall_total_kept": thesis_result.get("recall_total_kept"),
            "elapsed_ms": thesis_result.get("thesis_desk_committee_elapsed_ms"),
        }
        deterministic_payload["candidate_source"] = "llm_expert_committee"
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("committee failed before packet merge: %s\n%s", exc, tb)
        steps = deterministic_payload.get("discovery_steps") or []
        deterministic_payload["discovery_steps"] = [
            *steps,
            {
                "source": "llm_expert_committee",
                "status": "failed",
                "dimension": "committee",
                "error": str(exc),
                "traceback": tb,
            },
        ]
        deterministic_payload["llm_expert_committee"] = {
            "status": "failed",
            "error": str(exc),
            "traceback": tb,
            "dimensions_covered": [],
        }
        deterministic_payload["thesis_desk_committee"] = {
            "status": "failed",
            "error": str(exc),
            "traceback": tb,
            "candidate_count": 0,
            "diagnostics": [],
        }
        deterministic_payload["candidate_source"] = "llm_expert_committee"
        deterministic_payload["status"] = "failed"
        deterministic_payload["candidates"] = []
        deterministic_payload["candidate_count"] = 0

    deterministic_payload["committee_elapsed_ms"] = int((time.time() - started) * 1000)
    return deterministic_payload


def run_thesis_desk_committee(
    *,
    market: str,
    seed_symbols: Sequence[str],
    tool_registry: Any,
    llm_adapter: Any,
    regime: str = "unknown",
    today: Optional[str] = None,
    tool_decls: Optional[Sequence[Dict[str, Any]]] = None,
    overall_timeout_s: float = CommitteeOverallTimeoutSeconds,
    prebuilt_seeds: Optional[Sequence[SeedItem]] = None,
    seed_pool_result: Optional[SeedPoolBuildResult] = None,
    coarse_cap: int = 120,
    total_slots: int = 8,
    pick_top_n: int = 5,
    desk_fallback_supplement_n: int = 10,
    allocation_json: Optional[str] = None,
    backfill_rules_json: Optional[str] = None,
    backfill_max: int = 3,
) -> Dict[str, Any]:
    """Run P4 thesis-desk committee and return a discover-compatible payload.

    Flow: build_recall_pool → [EarlyTurn|Momentum|QualityRepair] desks
    (parallel) → aggregate_desk_picks → allocate_slots → payload.

    The returned dict has the same top-level shape as run_committee_discovery
    but carries candidate_source="thesis_desk_committee".
    """
    from src.agent.candidate_experts_v2.aggregator import (
        aggregate_desk_picks,
        allocate_slots,
    )
    from src.agent.candidate_experts_v2.experts.early_turn_desk import EarlyTurnDeskExpert
    from src.agent.candidate_experts_v2.experts.momentum_desk import MomentumDeskExpert
    from src.agent.candidate_experts_v2.experts.quality_repair_desk import QualityRepairDeskExpert
    from src.agent.candidate_experts_v2.recall import build_recall_pool

    started = time.time()
    market_value = (market or "cn").strip().lower() or "cn"
    tdecls = list(tool_decls or [])

    payload: Dict[str, Any] = {
        "status": "ok",
        "market": market_value,
        "candidates": [],
        "candidate_count": 0,
        "candidate_source": "thesis_desk_committee",
        "discovery_steps": [],
        "next_required_tools": [],
    }

    # ── Step 1: build recall pool ────────────────────────────────────────
    build_result: Optional[SeedPoolBuildResult] = seed_pool_result
    seed_symbols_list = list(seed_symbols or [])
    try:
        recall_result = build_recall_pool(
            market=market_value,
            seed_symbols=seed_symbols_list,
            tool_registry=tool_registry,
            today=today,
            coarse_cap=coarse_cap,
            prebuilt_pool=build_result,
        )
        rows: List[FeatureRow] = recall_result.rows
        payload["recall_diagnostics"] = recall_result.diagnostics
        payload["recall_sources"] = recall_result.sources
        payload["recall_total_in"] = recall_result.total_in
        payload["recall_total_kept"] = recall_result.total_kept
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("thesis_desk_committee: recall failed: %s\n%s", exc, tb)
        payload["status"] = "failed"
        payload["error"] = f"recall failed: {exc}"
        payload["traceback"] = tb
        payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
        return payload

    if not rows:
        logger.warning("thesis_desk_committee: recall pool empty")
        payload["status"] = "failed"
        payload["error"] = "recall pool empty"
        payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
        return payload

    # ── Step 2: run desks in parallel ────────────────────────────────────
    try:
        llm_callable = _coerce_llm_callable(llm_adapter)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("thesis_desk_committee: llm_adapter coerce failed: %s\n%s", exc, tb)
        payload["status"] = "failed"
        payload["error"] = f"llm coerce failed: {exc}"
        payload["traceback"] = tb
        payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
        return payload

    prompt_variables: Dict[str, Any] = {}
    if today:
        prompt_variables["today"] = today

    early_turn_desk = EarlyTurnDeskExpert(
        tool_registry=tool_registry,
        tool_decls=tdecls,
        llm=llm_callable,
        prompt_variables=prompt_variables,
        fallback_supplement_n=desk_fallback_supplement_n,
    )
    momentum_desk = MomentumDeskExpert(
        tool_registry=tool_registry,
        tool_decls=tdecls,
        llm=llm_callable,
        prompt_variables=prompt_variables,
        fallback_supplement_n=desk_fallback_supplement_n,
    )
    quality_repair_desk = QualityRepairDeskExpert(
        tool_registry=tool_registry,
        tool_decls=tdecls,
        llm=llm_callable,
        prompt_variables=prompt_variables,
        fallback_supplement_n=desk_fallback_supplement_n,
    )

    budget = max(10.0, overall_timeout_s - (time.time() - started))
    desk_deadline_s = time.time() + max(1.0, budget - 1.0)
    per_seed_timeout_s = max(
        3.0,
        min(180.0, (budget - 5.0) / max(1, len(rows))),
    )

    desk_tasks: Dict[str, Any] = {
        "early_turn_desk": lambda: early_turn_desk.run_desk(
            rows,
            market=market_value,
            regime=regime,
            deadline_s=desk_deadline_s,
            per_seed_timeout_s=per_seed_timeout_s,
        ),
        "momentum_desk": lambda: momentum_desk.run_desk(
            rows,
            market=market_value,
            regime=regime,
            deadline_s=desk_deadline_s,
            per_seed_timeout_s=per_seed_timeout_s,
        ),
        "quality_repair_desk": lambda: quality_repair_desk.run_desk(
            rows,
            market=market_value,
            regime=regime,
            deadline_s=desk_deadline_s,
            per_seed_timeout_s=per_seed_timeout_s,
        ),
    }

    desk_packets = run_experts_parallel(
        desk_tasks,
        per_expert_timeout_s=budget * 0.8,
        overall_timeout_s=budget,
    )
    payload["thesis_desk_packets"] = [
        _compact_desk_packet_for_trace(packet)
        for packet in desk_packets
    ]
    failed_packets = [
        packet for packet in desk_packets
        if packet.status in {"failed", "timeout", "unavailable"}
    ]
    if failed_packets:
        errors = []
        for packet in failed_packets:
            errors.extend([str(err) for err in (packet.errors or []) if err])
        payload["status"] = "failed"
        payload["error"] = "thesis desk timeout or failure"
        payload["thesis_desk_diagnostics"] = [
            {
                "desk": packet.expert,
                "status": packet.status,
                "errors": list(packet.errors or []),
            }
            for packet in desk_packets
        ]
        payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
        payload["discovery_steps"].append(
            {
                "source": "thesis_desk_committee",
                "status": "failed",
                "dimension": "committee",
                "error": "; ".join(errors) or "thesis desk timeout or failure",
            }
        )
        return payload

    # ── Step 3: aggregate + allocate ─────────────────────────────────────
    try:
        agg_pool = aggregate_desk_picks(desk_packets, rows)
        agg_pool.regime = regime
        final_candidates: List[AggregatedCandidate] = allocate_slots(
            agg_pool,
            regime,
            total=total_slots,
            allocation_json=allocation_json,
            backfill_rules_json=backfill_rules_json,
            backfill_max=backfill_max,
            pick_top_n=pick_top_n,
        )
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("thesis_desk_committee: aggregation failed: %s\n%s", exc, tb)
        payload["status"] = "failed"
        payload["error"] = f"aggregation failed: {exc}"
        payload["traceback"] = tb
        steps = payload.get("discovery_steps") or []
        payload["discovery_steps"] = [
            *steps,
            {
                "source": "thesis_desk_committee",
                "status": "failed",
                "dimension": "aggregate_allocate",
                "error": str(exc),
                "traceback": tb,
            },
        ]
        payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
        return payload

    # ── Step 4: convert to payload-compatible candidate dicts ────────────
    candidate_dicts: List[Dict[str, Any]] = []
    for ac in final_candidates:
        all_evidence = [
            {"tool": ev.tool, "summary": ev.summary, "metrics": ev.metrics}
            for evs in ac.evidence_by_desk.values()
            for ev in evs
        ]
        primary_stance = (ac.stance_by_desk or {}).get(ac.primary_desk)
        if not primary_stance and ac.stance_by_desk:
            # No stance for primary desk → fall back to the strongest desk stance.
            primary_stance = max(
                ac.stance_by_desk.values(),
                key=lambda s: _STANCE_RANK.get(str(s), 0),
            )
        d: Dict[str, Any] = {
            "code": ac.code,
            "name": ac.name,
            "market": ac.market,
            "stance": str(primary_stance or "support"),
            "stance_by_desk": dict(ac.stance_by_desk or {}),
            "setup_type": ac.setup_type,
            "reason": ac.reason,
            "confidence": ac.confidence,
            "primary_desk": ac.primary_desk,
            "desks": ac.desks,
            "multi_desk_conviction": ac.multi_desk_conviction,
            "conflict_flags": ac.conflict_flags,
            "llm_expert_evidence": {
                desk: [{"tool": ev.tool, "summary": ev.summary} for ev in evs]
                for desk, evs in ac.evidence_by_desk.items()
            },
            "risks": [{"type": r.type, "summary": r.summary} for r in ac.risks],
            "candidate_source": "thesis_desk_committee",
            "valid_until": "next_trading_day",
        }
        candidate_dicts.append(d)

    payload["candidates"] = candidate_dicts
    payload["candidate_count"] = len(candidate_dicts)
    payload["regime"] = regime
    payload["thesis_desk_diagnostics"] = agg_pool.diagnostics
    payload["thesis_desk_committee_elapsed_ms"] = int((time.time() - started) * 1000)
    return payload


__all__ = [
    "run_committee_discovery",
    "run_thesis_desk_committee",
    "CommitteeOverallTimeoutSeconds",
    "SeedPoolBuildResult",
    "SeedGateResult",
    "_build_seed_pool",
    "_build_seed_pool_result",
]
