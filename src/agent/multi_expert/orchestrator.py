# -*- coding: utf-8 -*-
"""Internal multi-expert orchestration for watchlist scans."""

from __future__ import annotations

from typing import Any, Dict

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
        bundle = EvidenceBundle(
            market_regime=market_regime or {},
            candidate_pool=candidate_pool or [],
            base_evidence=base_evidence or {},
            deep_dive_results=(deep_dive_stage.get("full") or {}).get("results", [])
            if isinstance(deep_dive_stage, dict)
            else [],
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
