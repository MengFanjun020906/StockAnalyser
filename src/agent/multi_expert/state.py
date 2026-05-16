# -*- coding: utf-8 -*-
"""Structured state contracts for the internal multi-expert orchestration layer."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.agent.evidence.schemas import EvidenceCard, ExpertEvidencePacket, JudgeInputPacket


ExpertVerdict = Literal["support", "neutral", "caution", "oppose", "insufficient_data"]


class EvidenceBundle(BaseModel):
    """Shared evidence visible to every expert.

    This is intentionally compact and JSON-first because it is persisted into
    Agent Trace artifacts. Experts may summarize or classify evidence, but the
    final Judge/risk layers remain responsible for trading decisions.
    """

    model_config = ConfigDict(extra="allow")

    market_regime: Dict[str, Any] = Field(default_factory=dict)
    candidate_pool: List[Dict[str, Any]] = Field(default_factory=list)
    base_evidence: Dict[str, Any] = Field(default_factory=dict)
    deep_dive_results: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_cards: List[EvidenceCard] = Field(default_factory=list)
    expert_packets: List[ExpertEvidencePacket] = Field(default_factory=list)
    judge_input_packet: Optional[JudgeInputPacket] = None
    allocation_plan: Dict[str, Any] = Field(default_factory=dict)
    adversarial_review: Dict[str, Any] = Field(default_factory=dict)
    judge_decision: Dict[str, Any] = Field(default_factory=dict)
    tool_quality: Dict[str, Any] = Field(default_factory=dict)


class ExpertOpinion(BaseModel):
    """One expert's structured opinion over the shared evidence."""

    model_config = ConfigDict(extra="allow")

    expert_name: str
    dimension: str
    verdict: ExpertVerdict = "insufficient_data"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    supporting_evidence: List[str] = Field(default_factory=list)
    opposing_evidence: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    candidate_impacts: List[Dict[str, Any]] = Field(default_factory=list)
    recommended_action: Optional[str] = None


class AgentState(BaseModel):
    """Shared state for a watchlist-scan expert graph run."""

    model_config = ConfigDict(extra="allow")

    task: str
    intent: str = "watchlist_scan"
    market: str = "cn"
    run_id: str = "selection-run"
    account_summary: Dict[str, Any] = Field(default_factory=dict)
    investor_profile: Dict[str, Any] = Field(default_factory=dict)
    orchestration_mode: str = "legacy"
    evidence_bundle: EvidenceBundle = Field(default_factory=EvidenceBundle)
    expert_opinions: Dict[str, ExpertOpinion] = Field(default_factory=dict)
    judge: Dict[str, Any] = Field(default_factory=dict)
    risk_gate: Dict[str, Any] = Field(default_factory=dict)
    status: str = "partial"

    def add_opinion(self, opinion: ExpertOpinion) -> None:
        """Attach or replace an expert opinion by expert name."""
        self.expert_opinions[opinion.expert_name] = opinion

    def to_trace_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe payload for Trace artifacts."""
        return self.model_dump(mode="json")
