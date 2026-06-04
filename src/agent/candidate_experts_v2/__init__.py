# -*- coding: utf-8 -*-
"""Candidate expert committee v2.

LLM-driven multi-expert candidate discovery. See docs/选股池多专家设计.md.

This package is opt-in via AGENT_CANDIDATE_DISCOVERY_MODE=llm_expert_committee
and does NOT replace the deterministic v1 in src.agent.candidate_experts.
"""

from src.agent.candidate_experts_v2.schemas import (
    EvidenceItem,
    ExpertCandidateV2,
    ExpertPacketV2,
    RiskNote,
    SeedFactPacket,
    SeedItem,
)

__all__ = [
    "EvidenceItem",
    "ExpertCandidateV2",
    "ExpertPacketV2",
    "RiskNote",
    "SeedFactPacket",
    "SeedItem",
]
