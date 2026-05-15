# -*- coding: utf-8 -*-
"""Watchlist-scan expert opinion builders.

The first expert-graph phase is deterministic: it classifies the evidence that
the existing staged stock-selection pipeline already collected. This gives the
Trace a real multi-expert state without adding more tool churn or model calls.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from src.agent.multi_expert.state import AgentState, ExpertOpinion, ExpertVerdict


def build_market_regime_expert_opinion(state: AgentState) -> ExpertOpinion:
    regime = state.evidence_bundle.market_regime or {}
    regime_name = str(regime.get("regime") or "unknown")
    risk_level = str(regime.get("risk_level") or "unknown")
    volatility = str(regime.get("volatility_bucket") or "unknown")
    hints = _as_text_list(regime.get("strategy_hints"))[:4]
    data_quality = str(regime.get("data_quality") or "")

    verdict: ExpertVerdict = "neutral"
    confidence = 0.55
    risks: List[str] = []
    action = "selectively_open"
    if regime_name in {"risk_off", "panic"} or risk_level in {"high", "extreme", "critical"} or volatility == "extreme":
        verdict = "caution"
        confidence = 0.8
        risks.append("市场状态限制主动开仓或追高。")
        action = "wait_or_reduce"
    elif data_quality in {"insufficient", "limited"}:
        verdict = "caution"
        confidence = 0.6
        risks.append("市场状态数据质量有限，需要降低动作强度。")

    return ExpertOpinion(
        expert_name="market_regime_expert",
        dimension="market_regime",
        verdict=verdict,
        confidence=confidence,
        summary=f"市场状态 {regime_name}，风险等级 {risk_level}，波动档位 {volatility}。",
        supporting_evidence=hints,
        missing_evidence=[] if regime else ["detect_market_regime"],
        risk_flags=risks,
        recommended_action=action,
    )


def build_candidate_expert_opinion(state: AgentState) -> ExpertOpinion:
    candidates = state.evidence_bundle.candidate_pool or []
    impacts = []
    labels: List[str] = []
    for item in candidates:
        dims = item.get("reason_dimensions") if isinstance(item, dict) else []
        dim_labels = [str(dim.get("label") or dim.get("dimension") or "") for dim in dims or [] if isinstance(dim, dict)]
        labels.extend([label for label in dim_labels if label])
        impacts.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "source": item.get("source"),
            "recall_sources": item.get("recall_sources") or [],
            "reason_dimensions": dims or [],
            "signal_score": item.get("signal_score"),
        })
    unique_labels = list(dict.fromkeys(labels))
    verdict: ExpertVerdict = "support" if candidates else "insufficient_data"
    return ExpertOpinion(
        expert_name="candidate_discovery_expert",
        dimension="candidate_discovery",
        verdict=verdict,
        confidence=0.7 if candidates else 0.1,
        summary=(
            f"候选池共 {len(candidates)} 只，来源维度包含：{'、'.join(unique_labels) or '未标注'}。"
        ),
        supporting_evidence=[_candidate_line(item) for item in impacts[:8]],
        missing_evidence=[] if candidates else ["discover_watchlist_candidates"],
        risk_flags=[] if candidates else ["候选池为空，不能进入排序和配置。"],
        candidate_impacts=impacts,
        recommended_action="continue_evidence_collection" if candidates else "wait",
    )


def build_technical_expert_opinion(state: AgentState) -> ExpertOpinion:
    return _dimension_opinion(
        state,
        expert_name="technical_expert",
        dimension="technical",
        labels=("technical", "price_structure"),
        positive_summary="技术结构证据已覆盖趋势/结构工具。",
        missing_hint="analyze_trend/analyze_price_structure",
    )


def build_capital_expert_opinion(state: AgentState) -> ExpertOpinion:
    return _dimension_opinion(
        state,
        expert_name="capital_chip_expert",
        dimension="capital_chip",
        labels=("capital_flow", "chip"),
        positive_summary="资金/筹码证据已覆盖主力资金或筹码结构。",
        missing_hint="get_capital_flow/get_chip_distribution",
    )


def build_news_sentiment_expert_opinion(state: AgentState) -> ExpertOpinion:
    candidate_hits = _candidate_dimension_hits(state.evidence_bundle.candidate_pool, {"sentiment", "message", "news_event"})
    opinion = _dimension_opinion(
        state,
        expert_name="news_sentiment_expert",
        dimension="news_sentiment",
        labels=("news_event",),
        positive_summary="消息/情绪证据已覆盖候选来源或综合情报。",
        missing_hint="search_comprehensive_intel/sentiment_tools",
        candidate_hits=candidate_hits,
    )
    if candidate_hits and opinion.verdict == "insufficient_data":
        opinion.verdict = "neutral"
        opinion.confidence = 0.45
        opinion.summary = "候选池存在消息/热点/情绪来源，但深度情绪工具仍未闭环。"
    return opinion


def build_fundamental_expert_opinion(state: AgentState) -> ExpertOpinion:
    return _dimension_opinion(
        state,
        expert_name="fundamental_expert",
        dimension="fundamental",
        labels=("fundamental",),
        positive_summary="基本面证据已覆盖公司信息或估值字段。",
        missing_hint="get_stock_info",
    )


def build_portfolio_risk_expert_opinion(state: AgentState) -> ExpertOpinion:
    allocation = state.evidence_bundle.allocation_plan or {}
    judge = state.evidence_bundle.judge_decision or {}
    summary = allocation.get("summary") if isinstance(allocation.get("summary"), dict) else allocation
    judge_summary = judge.get("summary") if isinstance(judge.get("summary"), dict) else judge
    action = str(judge_summary.get("final_action") or summary.get("portfolio_action") or "wait")
    total_pct = summary.get("initial_total_position_pct")
    risks = _as_text_list((allocation.get("full") or {}).get("risk_controls") if isinstance(allocation.get("full"), dict) else [])
    verdict: ExpertVerdict = "support" if action == "open" else "caution"
    confidence = 0.7 if action in {"open", "wait", "reject", "monitor"} else 0.45
    return ExpertOpinion(
        expert_name="portfolio_risk_expert",
        dimension="portfolio_risk",
        verdict=verdict,
        confidence=confidence,
        summary=f"组合层最终动作 {action}，初始总仓位 {total_pct if total_pct is not None else '-'}%。",
        supporting_evidence=[str(summary.get("core_reason") or "")] if summary.get("core_reason") else [],
        missing_evidence=[],
        risk_flags=risks[:5],
        recommended_action=action,
    )


def _dimension_opinion(
    state: AgentState,
    *,
    expert_name: str,
    dimension: str,
    labels: Iterable[str],
    positive_summary: str,
    missing_hint: str,
    candidate_hits: List[str] | None = None,
) -> ExpertOpinion:
    label_set = set(labels)
    results = state.evidence_bundle.deep_dive_results or []
    supports: List[str] = []
    missing: List[str] = []
    risks: List[str] = []
    impacts: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        full = item.get("full") if isinstance(item.get("full"), dict) else {}
        stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
        code = str(summary.get("code") or stock.get("code") or "")
        name = str(summary.get("name") or stock.get("name") or code)
        dim_summary = full.get("dimension_summary") if isinstance(full.get("dimension_summary"), dict) else {}
        matched = {
            key: value
            for key, value in dim_summary.items()
            if key in label_set and isinstance(value, dict)
        }
        missing_evidence = _as_text_list(full.get("missing_evidence"))
        tool_failures = full.get("tool_failures") if isinstance(full.get("tool_failures"), list) else []
        risk_flags = _as_text_list(full.get("risk_flags"))
        if matched:
            supports.append(f"{code} {name}: " + "; ".join(f"{key}={value.get('verdict')}" for key, value in matched.items()))
            impacts.append({"code": code, "name": name, "dimension_summary": matched})
        if any(label in str(evidence) for label in label_set for evidence in missing_evidence):
            missing.append(f"{code} {name}: {missing_hint} 缺失")
        for failure in tool_failures:
            if isinstance(failure, dict) and any(label in str(failure.get("tool") or "") for label in label_set):
                missing.append(f"{code} {name}: {failure.get('tool')} 失败")
        risks.extend([f"{code} {name}: {risk}" for risk in risk_flags[:2]])

    candidate_hits = candidate_hits or []
    supports.extend(candidate_hits[:5])
    verdict: ExpertVerdict = "support" if supports else "insufficient_data"
    if missing and supports:
        verdict = "caution"
    confidence = 0.7 if supports and not missing else (0.55 if supports else 0.2)
    return ExpertOpinion(
        expert_name=expert_name,
        dimension=dimension,
        verdict=verdict,
        confidence=confidence,
        summary=positive_summary if supports else f"{dimension} 证据不足，缺少 {missing_hint}。",
        supporting_evidence=supports[:8],
        missing_evidence=list(dict.fromkeys(missing or ([missing_hint] if not supports else [])))[:8],
        risk_flags=list(dict.fromkeys(risks))[:8],
        candidate_impacts=impacts,
        recommended_action="continue_or_wait" if verdict in {"support", "caution"} else "wait",
    )


def _candidate_dimension_hits(candidates: List[Dict[str, Any]], dimensions: set[str]) -> List[str]:
    hits: List[str] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        name = item.get("name") or code
        for dim in item.get("reason_dimensions") or []:
            if not isinstance(dim, dict):
                continue
            if str(dim.get("dimension") or "") in dimensions or str(dim.get("label") or "") in {"情绪/热点", "消息/输入"}:
                hits.append(f"{code} {name}: {dim.get('label') or dim.get('dimension')} - {dim.get('detail')}")
    return hits


def _candidate_line(item: Dict[str, Any]) -> str:
    dims = item.get("reason_dimensions") or []
    reason = "；".join(
        str(dim.get("detail") or "")
        for dim in dims
        if isinstance(dim, dict) and dim.get("detail")
    )
    return f"{item.get('code')} {item.get('name')}: {reason or item.get('source') or '-'}"


def _as_text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False, default=str)]
    text = str(value).strip()
    return [text] if text else []
