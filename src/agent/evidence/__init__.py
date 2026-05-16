# -*- coding: utf-8 -*-
"""Evidence-card contracts and adapters for multi-expert Agent flows."""

from src.agent.evidence.adapter import build_evidence_cards_for_stock, build_expert_packet
from src.agent.evidence.schemas import (
    CounterEvidence,
    DataQuality,
    EvidenceCard,
    EvidenceExpiry,
    EvidenceImpact,
    EvidenceSignal,
    ExpertEvidencePacket,
    JudgeInputPacket,
    StockRef,
)

__all__ = [
    "CounterEvidence",
    "DataQuality",
    "EvidenceCard",
    "EvidenceExpiry",
    "EvidenceImpact",
    "EvidenceSignal",
    "ExpertEvidencePacket",
    "JudgeInputPacket",
    "StockRef",
    "build_evidence_cards_for_stock",
    "build_expert_packet",
]
