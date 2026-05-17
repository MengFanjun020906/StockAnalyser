# -*- coding: utf-8 -*-
"""Internal multi-expert orchestration for watchlist scans."""

from __future__ import annotations

from typing import Any, Dict

from src.agent.evidence import build_expert_packet
from src.agent.evidence.schemas import EvidenceCard, JudgeInputPacket, StockRef
from src.agent.multi_expert.experts import (
    build_capital_expert_opinion,
    build_candidate_expert_opinion,
    build_fundamental_expert_opinion,
    build_market_regime_expert_opinion,
    build_news_sentiment_expert_opinion,
    build_portfolio_risk_expert_opinion,
    build_technical_expert_opinion,
)
from src.agent.multi_expert.state import AgentState, EvidenceBundle


class ExpertOrchestrator:
    """Build structured expert opinions from an existing selection context."""

    def __init__(self, mode: str = "legacy"):
        self.mode = mode if mode in {"legacy", "expert_graph"} else "legacy"

    @property
    def enabled(self) -> bool:
        return self.mode == "expert_graph"

    def run_watchlist_scan(
        self,
        *,
        task: str,
        run_id: str,
        market: str,
        account_summary: Dict[str, Any],
        investor_profile: Dict[str, Any],
        market_regime: Dict[str, Any],
        candidate_pool: list[Dict[str, Any]],
        base_evidence: Dict[str, Any],
        deep_dive_stage: Dict[str, Any],
        allocation_stage: Dict[str, Any],
        adversarial_stage: Dict[str, Any],
        judge_stage: Dict[str, Any],
        evidence_ledger: Dict[str, Any],
    ) -> AgentState:
        """Return a shared-state expert graph payload for Trace."""
        evidence_cards = _collect_cards(deep_dive_stage)
        stock_ref = _primary_stock_ref(candidate_pool, evidence_cards, market)
        expert_packets = _build_packets(run_id, evidence_cards, stock_ref)
        judge_input = _build_judge_input(stock_ref, expert_packets, allocation_stage, judge_stage)
        bundle = EvidenceBundle(
            market_regime=market_regime or {},
            candidate_pool=candidate_pool or [],
            base_evidence=base_evidence or {},
            deep_dive_results=(deep_dive_stage.get("full") or {}).get("results", [])
            if isinstance(deep_dive_stage, dict)
            else [],
            evidence_cards=evidence_cards,
            expert_packets=expert_packets,
            judge_input_packet=judge_input,
            allocation_plan=allocation_stage or {},
            adversarial_review=adversarial_stage or {},
            judge_decision=judge_stage or {},
            tool_quality=evidence_ledger.get("summary") if isinstance(evidence_ledger, dict) else {},
        )
        state = AgentState(
            task=task,
            intent="watchlist_scan",
            market=market,
            run_id=run_id,
            account_summary=account_summary or {},
            investor_profile=investor_profile or {},
            orchestration_mode=self.mode,
            evidence_bundle=bundle,
            judge=judge_stage.get("summary") if isinstance(judge_stage, dict) else {},
            status="ok",
        )
        for builder in (
            build_market_regime_expert_opinion,
            build_candidate_expert_opinion,
            build_technical_expert_opinion,
            build_capital_expert_opinion,
            build_news_sentiment_expert_opinion,
            build_fundamental_expert_opinion,
            build_portfolio_risk_expert_opinion,
        ):
            state.add_opinion(builder(state))
        return state


def _collect_cards(deep_dive_stage: Dict[str, Any]) -> list[EvidenceCard]:
    cards: list[EvidenceCard] = []
    if not isinstance(deep_dive_stage, dict):
        return cards
    results = (deep_dive_stage.get("full") or {}).get("results", [])
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        full = item.get("full") if isinstance(item.get("full"), dict) else {}
        for raw_card in full.get("evidence_cards") or []:
            try:
                cards.append(raw_card if isinstance(raw_card, EvidenceCard) else EvidenceCard.model_validate(raw_card))
            except Exception:
                continue
    return cards


def _primary_stock_ref(candidate_pool: list[Dict[str, Any]], cards: list[EvidenceCard], market: str) -> StockRef | None:
    if cards:
        return cards[0].stock
    for item in candidate_pool or []:
        if isinstance(item, dict) and item.get("code"):
            return StockRef(
                code=str(item.get("code")),
                name=str(item.get("name") or item.get("code")),
                market=str(item.get("market") or market or "cn"),
            )
    return None


def _build_packets(run_id: str, cards: list[EvidenceCard], stock: StockRef | None) -> list:
    specs = [
        ("technical_expert", "technical", "analyze_trend/analyze_price_structure"),
        ("capital_chip_expert", "capital_chip", "get_capital_flow/get_chip_distribution"),
        ("news_sentiment_expert", "news_sentiment", "search_comprehensive_intel/sentiment_tools"),
        ("fundamental_expert", "fundamental", "get_stock_info"),
        ("market_regime_expert", "market_regime", "detect_market_regime/get_sector_rankings"),
    ]
    return [
        build_expert_packet(
            run_id=run_id,
            expert=expert,
            dimension=dimension,
            cards=cards,
            stock=stock,
            missing_hint=missing,
        )
        for expert, dimension, missing in specs
    ]


def _build_judge_input(
    stock: StockRef | None,
    packets: list,
    allocation_stage: Dict[str, Any],
    judge_stage: Dict[str, Any],
) -> JudgeInputPacket:
    decision_matrix = []
    for packet in packets:
        if packet.stance == "invalid":
            effective_weight = 0.0
        elif packet.stance == "support":
            effective_weight = 0.2 * packet.confidence
        elif packet.stance in {"oppose", "wait_confirm"}:
            effective_weight = 0.25 * packet.confidence
        else:
            effective_weight = 0.1 * packet.confidence
        decision_matrix.append({
            "dimension": packet.dimension,
            "stance": packet.stance,
            "base_weight": 0.2,
            "effective_weight": round(effective_weight, 4),
        })
    hard_constraints = {}
    if isinstance(allocation_stage, dict):
        hard_constraints["allocation_summary"] = allocation_stage.get("summary") or {}
    if isinstance(judge_stage, dict):
        hard_constraints["judge_summary"] = judge_stage.get("summary") or {}
    return JudgeInputPacket(
        stock=stock,
        expert_packets={packet.expert: packet for packet in packets},
        hard_constraints=hard_constraints,
        decision_matrix=decision_matrix,
        top_counter_evidence=[
            risk
            for packet in packets
            for risk in packet.top_risks[:2]
        ][:5],
    )
