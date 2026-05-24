# -*- coding: utf-8 -*-
"""LLM expert committee facade for candidate discovery.

This module is the *only* entry point that stock_selection should use when
``AGENT_CANDIDATE_DISCOVERY_MODE=llm_expert_committee``. It returns a payload
structurally compatible with the deterministic ``discover_watchlist_candidates``
tool so downstream pipeline stages remain untouched.

Current coverage:
- Capital dimension: real LLM expert (``CapitalFlowExpert``).
- Other dimensions: not covered; candidates list starts empty and only LLM
  capital-flow candidates are added.

Seed pool construction (four sources, deterministic, runs before any LLM call):
1. User-provided target_symbols → source="user_watchlist"
2. Daily limit-up list (get_tushare_limit_list_d) → source="limit_up_pool"
3. Hot-rank list (get_tushare_hot_rank) → source="hot_rank"
4. AlphaSift + Sequoia quant candidates → source="alphasift" / "sequoia"

If the capital expert fails / times out, the facade returns an empty candidates
payload plus a diagnostic so callers can record the degradation in their trace.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.agent.candidate_experts_v2.experts.base import (
    LLMCallable,
    LLMToolCall,
    LLMTurn,
)
from src.agent.candidate_experts_v2.experts.capital_flow import CapitalFlowExpert
from src.agent.candidate_experts_v2.experts.early_turn import EarlyTurnExpert
from src.agent.candidate_experts_v2.runtime import run_experts_parallel
from src.agent.candidate_experts_v2.schemas import SeedItem

logger = logging.getLogger(__name__)


CommitteeOverallTimeoutSeconds = 90.0


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


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
            SeedItem(code=code, name=code, market=market, source="user_watchlist")
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


def _build_seed_pool(
    *,
    market: str,
    seed_symbols: Sequence[str],
    tool_registry: Any,
    today: Optional[str] = None,
    limit_per_source: int = 15,
    total_limit: int = 40,
) -> List[SeedItem]:
    """Build the shared seed pool from four deterministic sources.

    Priority order (first occurrence of each code wins):
    1. User-provided seed_symbols → source="user_watchlist"
    2. Daily limit-up list       → source="limit_up_pool"
    3. THS hot-rank list         → source="hot_rank"
    4. AlphaSift quant candidates → source="alphasift"
       Sequoia quant candidates  → source="sequoia"
    """
    seen_codes: Dict[str, bool] = {}
    pool: List[SeedItem] = []

    def _add(item: SeedItem) -> None:
        code = str(item.code or "").strip()
        if not code or code in seen_codes:
            return
        seen_codes[code] = True
        pool.append(item)

    # --- Source 1: user watchlist ---
    for seed in _to_seed_items(seed_symbols, market):
        _add(seed)

    # Use the most recent trading day for limit-up/hot-rank so weekend/holiday
    # runs still fetch real data from the last session.
    try:
        from src.agent.tools.data_tools import _latest_tushare_trade_date
        trade_date = _latest_tushare_trade_date() or today or ""
    except Exception:
        trade_date = today or ""

    # --- Source 2: limit-up pool ---
    try:
        result = _safe_tool_call(
            tool_registry, "get_tushare_limit_list_d",
            trade_date=trade_date, limit=limit_per_source,
        )
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            # Only include true limit-up (U), exclude limit-down (D)
            if str(item.get("limit_status") or "").upper() != "U":
                continue
            streak = item.get("limit_up_streak") or 1
            _add(SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market=market,
                source="limit_up_pool",
                hint=f"涨停,连板={streak}",
            ))
    except Exception as exc:
        logger.debug("seed pool: limit_list_d failed: %s", exc)

    # --- Source 3: hot-rank ---
    try:
        result = _safe_tool_call(
            tool_registry, "get_tushare_hot_rank",
            source="ths", trade_date=trade_date, limit=limit_per_source,
        )
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            rank = item.get("rank") or ""
            _add(SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market=market,
                source="hot_rank",
                hint=f"热榜rank={rank}",
            ))
    except Exception as exc:
        logger.debug("seed pool: hot_rank failed: %s", exc)

    # --- Source 4a: AlphaSift ---
    try:
        from src.agent.candidate_providers.alphasift_provider import AlphaSiftCandidateProvider
        alpha_result = AlphaSiftCandidateProvider().discover(limit=limit_per_source)
        for cand in alpha_result.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            code = str(cand.get("code") or "").strip()
            if not code:
                continue
            strategies = ",".join(cand.get("matched_strategies") or [])
            _add(SeedItem(
                code=code,
                name=str(cand.get("name") or code),
                market=market,
                source="alphasift",
                hint=f"策略={strategies}" if strategies else "alphasift",
            ))
    except Exception as exc:
        logger.debug("seed pool: alphasift failed: %s", exc)

    # --- Source 4b: Sequoia ---
    try:
        from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
        seq_result = SequoiaCandidateProvider().discover(limit=limit_per_source)
        for cand in seq_result.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            code = str(cand.get("code") or "").strip()
            if not code:
                continue
            strategies = ",".join(cand.get("matched_strategies") or [])
            _add(SeedItem(
                code=code,
                name=str(cand.get("name") or code),
                market=market,
                source="sequoia",
                hint=f"策略={strategies}" if strategies else "sequoia",
            ))
    except Exception as exc:
        logger.debug("seed pool: sequoia failed: %s", exc)

    # --- Source 5a: fundamental low-base seeds ---
    try:
        from src.agent.candidate_providers.fundamental_provider import FundamentalCandidateProvider

        fundamental_result = FundamentalCandidateProvider().discover(limit=limit_per_source)
        for cand in fundamental_result.get("candidates") or []:
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
            _add(
                SeedItem(
                    code=code,
                    name=str(cand.get("name") or code),
                    market=market,
                    source="fundamental_snapshot",
                    hint="；".join(hint_parts) or "成长改善但估值未明显扩张",
                )
            )
    except Exception as exc:
        logger.debug("seed pool: fundamental_snapshot failed: %s", exc)

    # --- Source 5b: low-base structure seeds from shared daily DB ---
    try:
        structure_candidates = _build_low_base_structure_seeds(limit=limit_per_source)
        for seed in structure_candidates:
            _add(seed)
    except Exception as exc:
        logger.debug("seed pool: low_base_structure failed: %s", exc)

    result_pool = pool[:total_limit]
    source_counts = {}
    for item in result_pool:
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
    logger.info(
        "seed pool built: %d items total, sources=%s",
        len(result_pool),
        source_counts,
    )
    return result_pool


def _merge_capital_evidence(
    deterministic_payload: Dict[str, Any],
    capital_packet: Any,
) -> Dict[str, Any]:
    """Merge LLM capital-flow evidence into deterministic candidates.

    Strategy: for each LLM-produced candidate whose ``code`` also exists in the
    deterministic candidate list, attach the LLM expert's evidence under
    ``llm_expert_evidence`` so downstream rendering can surface the LLM view
    without disturbing the deterministic schema.
    """

    candidates = deterministic_payload.get("candidates") or []
    by_code: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        if isinstance(cand, dict):
            code_key = str(cand.get("code") or "").strip()
            if code_key:
                by_code[code_key] = cand

    llm_candidates = getattr(capital_packet, "candidates", None) or []
    matched = 0
    extras_appended: List[Dict[str, Any]] = []
    for llm_cand in llm_candidates:
        try:
            code_key = str(getattr(llm_cand, "code", "") or "").strip()
        except Exception:
            continue
        if not code_key:
            continue
        ev_list = [
            {
                "tool": getattr(ev, "tool", ""),
                "summary": getattr(ev, "summary", ""),
                "metrics": getattr(ev, "metrics", {}) or {},
            }
            for ev in (getattr(llm_cand, "evidence", None) or [])
        ]
        if code_key in by_code:
            target = by_code[code_key]
            existing = target.get("llm_expert_evidence") or {}
            existing.setdefault("capital", []).extend(ev_list)
            target["llm_expert_evidence"] = existing
            target.setdefault("llm_expert_dimensions", []).append("capital")
            matched += 1
        else:
            extras_appended.append(
                {
                    "code": code_key,
                    "name": getattr(llm_cand, "name", "") or code_key,
                    "source": "llm_capital_expert",
                    "reason": getattr(llm_cand, "reason", "") or "LLM capital-flow expert",
                    "llm_expert_evidence": {"capital": ev_list},
                    "llm_expert_dimensions": ["capital"],
                }
            )

    steps = deterministic_payload.get("discovery_steps") or []
    steps = [
        *steps,
        {
            "source": "llm_expert_committee",
            "status": getattr(capital_packet, "status", "unknown"),
            "dimension": "capital",
            "count_matched": matched,
            "count_extra": len(extras_appended),
            "elapsed_ms": getattr(capital_packet, "elapsed_ms", 0),
        },
    ]
    deterministic_payload["discovery_steps"] = steps
    if extras_appended:
        deterministic_payload["candidates"] = list(candidates) + extras_appended
        deterministic_payload["candidate_count"] = len(deterministic_payload["candidates"])
    deterministic_payload["candidate_source"] = "llm_expert_committee"
    return deterministic_payload


def _merge_early_turn_evidence(
    deterministic_payload: Dict[str, Any],
    early_turn_packet: Any,
) -> Dict[str, Any]:
    candidates = deterministic_payload.get("candidates") or []
    by_code: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        if isinstance(cand, dict):
            code_key = str(cand.get("code") or "").strip()
            if code_key:
                by_code[code_key] = cand

    llm_candidates = getattr(early_turn_packet, "candidates", None) or []
    matched = 0
    extras_appended: List[Dict[str, Any]] = []
    for llm_cand in llm_candidates:
        try:
            code_key = str(getattr(llm_cand, "code", "") or "").strip()
        except Exception:
            continue
        if not code_key:
            continue
        ev_list = [
            {
                "tool": getattr(ev, "tool", ""),
                "summary": getattr(ev, "summary", ""),
                "metrics": getattr(ev, "metrics", {}) or {},
            }
            for ev in (getattr(llm_cand, "evidence", None) or [])
        ]
        risk_list = [
            {
                "type": getattr(risk, "type", "risk"),
                "summary": getattr(risk, "summary", ""),
            }
            for risk in (getattr(llm_cand, "risks", None) or [])
        ]
        if code_key in by_code:
            target = by_code[code_key]
            existing = target.get("llm_expert_evidence") or {}
            existing.setdefault("early_turn", []).extend(ev_list)
            target["llm_expert_evidence"] = existing
            target.setdefault("llm_expert_dimensions", []).append("early_turn")
            if risk_list:
                existing_risks = target.get("llm_expert_risks") or {}
                existing_risks.setdefault("early_turn", []).extend(risk_list)
                target["llm_expert_risks"] = existing_risks
            matched += 1
        else:
            extras_appended.append(
                {
                    "code": code_key,
                    "name": getattr(llm_cand, "name", "") or code_key,
                    "source": "llm_early_turn_expert",
                    "reason": getattr(llm_cand, "reason", "") or "LLM early-turn expert",
                    "llm_expert_evidence": {"early_turn": ev_list},
                    "llm_expert_dimensions": ["early_turn"],
                    "llm_expert_risks": {"early_turn": risk_list} if risk_list else {},
                }
            )

    steps = deterministic_payload.get("discovery_steps") or []
    steps = [
        *steps,
        {
            "source": "llm_expert_committee",
            "status": getattr(early_turn_packet, "status", "unknown"),
            "dimension": "early_turn",
            "count_matched": matched,
            "count_extra": len(extras_appended),
            "elapsed_ms": getattr(early_turn_packet, "elapsed_ms", 0),
        },
    ]
    deterministic_payload["discovery_steps"] = steps
    if extras_appended:
        deterministic_payload["candidates"] = list(candidates) + extras_appended
        deterministic_payload["candidate_count"] = len(deterministic_payload["candidates"])
    deterministic_payload["candidate_source"] = "llm_expert_committee"
    return deterministic_payload


def _merge_expert_packet(
    deterministic_payload: Dict[str, Any],
    packet: Any,
) -> Dict[str, Any]:
    dimension = str(getattr(packet, "dimension", "") or getattr(packet, "expert", "")).strip()
    if dimension == "capital":
        return _merge_capital_evidence(deterministic_payload, packet)
    if dimension == "early_turn":
        return _merge_early_turn_evidence(deterministic_payload, packet)
    return deterministic_payload


def _packet_summary(packet: Any) -> Dict[str, Any]:
    return {
        "status": getattr(packet, "status", "unknown"),
        "candidate_count": len(getattr(packet, "candidates", []) or []),
        "elapsed_ms": getattr(packet, "elapsed_ms", 0),
        "tool_calls": getattr(packet, "tool_calls", []) or [],
        "errors": getattr(packet, "errors", []) or [],
        "seed_summary": getattr(packet, "seed_summary", None).model_dump() if getattr(packet, "seed_summary", None) else {},
    }


def _committee_candidate_score(item: Dict[str, Any]) -> float:
    base = _safe_float(item.get("signal_score")) or 0.0
    dims = list(dict.fromkeys(item.get("llm_expert_dimensions") or []))
    bonus = 0.0
    if "capital" in dims and "early_turn" in dims:
        bonus += 12.0
    elif len(dims) >= 2:
        bonus += 8.0
    elif "early_turn" in dims:
        bonus += 4.0
    return round(base + bonus, 2)


def _sort_committee_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(item: Dict[str, Any]) -> tuple:
        dims = list(dict.fromkeys(item.get("llm_expert_dimensions") or []))
        has_early_turn = 1 if "early_turn" in dims else 0
        resonance = len(dims)
        score = _committee_candidate_score(item)
        return (
            -has_early_turn,
            -resonance,
            -score,
            str(item.get("code") or ""),
        )

    ordered = sorted(candidates, key=sort_key)
    for item in ordered:
        item["committee_score"] = _committee_candidate_score(item)
    return ordered


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
        (attack_days["high"] - attack_days["close"]).abs() / attack_days["high"].replace(0, pd.NA) * 100
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
    """

    started = time.time()
    market_value = (market or "cn").strip().lower() or "cn"

    # Start with an empty payload — V2 committee mode only produces LLM-sourced
    # candidates; other dimensions are intentionally left empty so the pipeline
    # runs only the capital-flow LLM expert without triggering any V1 hard-rule
    # expert work.
    deterministic_payload: Dict[str, Any] = {
        "status": "ok",
        "market": market_value,
        "candidates": [],
        "candidate_count": 0,
        "candidate_source": "llm_expert_committee",
        "discovery_steps": [],
        "next_required_tools": [],
    }

    if prebuilt_seeds is not None:
        seeds = list(prebuilt_seeds)
    else:
        seeds = _to_seed_items(seed_symbols, market_value)

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

        experts = {
            "capital_flow_expert": CapitalFlowExpert(
                tool_registry=tool_registry,
                tool_decls=list(tool_decls or []),
                llm=llm_callable,
                prompt_variables=prompt_variables,
                max_llm_rounds=5,
                max_tool_calls=10,
            ),
            "early_turn_expert": EarlyTurnExpert(
                tool_registry=tool_registry,
                tool_decls=list(tool_decls or []),
                llm=llm_callable,
                prompt_variables=prompt_variables,
                max_llm_rounds=5,
                max_tool_calls=10,
            ),
        }
        tasks = {
            name: (lambda expert=expert: expert.run(seeds, market=market_value, use_cache=True))
            for name, expert in experts.items()
        }
        packets = run_experts_parallel(
            tasks,
            per_expert_timeout_s=min(30.0, budget_remaining),
            overall_timeout_s=budget_remaining,
            max_workers=len(tasks),
        )

        committee_meta: Dict[str, Any] = {
            "status": "ok",
            "seed_count": len(seeds),
            "dimensions_covered": [],
        }
        any_success = False
        for packet in packets:
            dimension = str(getattr(packet, "dimension", "")).strip()
            if getattr(packet, "status", "") in {"ok", "partial", "empty"}:
                committee_meta["dimensions_covered"].append(dimension)
                any_success = True
            deterministic_payload = _merge_expert_packet(deterministic_payload, packet)
            committee_meta[dimension] = _packet_summary(packet)

        merged_candidates = deterministic_payload.get("candidates")
        if isinstance(merged_candidates, list):
            deterministic_payload["candidates"] = _sort_committee_candidates(
                [item for item in merged_candidates if isinstance(item, dict)]
            )
            deterministic_payload["candidate_count"] = len(deterministic_payload["candidates"])

        if not any_success:
            committee_meta["status"] = "failed"
            committee_meta["error"] = "all committee experts failed or timed out"

        deterministic_payload["llm_expert_committee"] = committee_meta
        deterministic_payload["candidate_source"] = "llm_expert_committee"
    except Exception as exc:
        logger.warning("committee failed before packet merge: %s; using deterministic only", exc)
        steps = deterministic_payload.get("discovery_steps") or []
        deterministic_payload["discovery_steps"] = [
            *steps,
            {
                "source": "llm_expert_committee",
                "status": "failed",
                "dimension": "committee",
                "error": str(exc),
            },
        ]
        deterministic_payload["llm_expert_committee"] = {
            "status": "failed",
            "error": str(exc),
            "dimensions_covered": [],
        }
        deterministic_payload["candidate_source"] = "llm_expert_committee"

    deterministic_payload["committee_elapsed_ms"] = int((time.time() - started) * 1000)
    return deterministic_payload


__all__ = ["run_committee_discovery", "CommitteeOverallTimeoutSeconds", "_build_seed_pool"]
