# -*- coding: utf-8 -*-
"""Schemas for candidate expert committee v2.

Differences vs v1 (src.agent.candidate_experts.schemas):
- ``evidence`` is a list of structured EvidenceItem (tool/summary/metrics),
  NOT a list of source-name strings. No v1 compatibility shim.
- Stance enum extended with ``neutral``.
- Adds ``RiskNote`` for per-candidate structured risks.
- Adds ``SeedItem`` for the shared seed pool consumed by all experts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


CandidateStanceV2 = Literal["support", "watch", "neutral", "oppose", "invalid"]
ExpertStatusV2 = Literal["ok", "partial", "empty", "failed", "timeout", "unavailable"]
CandidateSetupTypeV2 = Literal[
    "trend_continuation",
    "early_turn",
    "theme_follow",
    "quality_repair",
    "capital_momentum",
    "unknown",
]
SeedSource = Literal[
    "user_watchlist",
    "limit_up_pool",
    "hot_rank",
    "strong_sector",
    "alphasift",
    "sequoia",
    "fundamental_snapshot",
    "low_base_structure",
    "fallback",
]


class SeedItem(BaseModel):
    """One candidate seed shared across experts before LLM filtering."""

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    source: SeedSource = "fallback"
    hint: str = ""
    extras: Dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    """Structured evidence emitted by an expert tied to one tool call."""

    model_config = ConfigDict(extra="allow")

    tool: str
    summary: str = ""
    metrics: Dict[str, Any] = Field(default_factory=dict)


class RiskNote(BaseModel):
    """Structured risk note attached to a candidate."""

    model_config = ConfigDict(extra="allow")

    type: str
    summary: str = ""


class ExpertCandidateV2(BaseModel):
    """One stock candidate produced by a v2 LLM expert."""

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    score: float = Field(default=50.0, ge=0.0, le=100.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    stance: CandidateStanceV2 = "support"
    setup_type: Optional[CandidateSetupTypeV2] = None
    reason: str = ""
    evidence: List[EvidenceItem] = Field(default_factory=list)
    risks: List[RiskNote] = Field(default_factory=list)
    valid_until: str = "next_trading_day"


class SeedSummaryV2(BaseModel):
    """Summary of the seed pool seen and filtered by one expert."""

    model_config = ConfigDict(extra="allow")

    seed_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    seed_sources: Dict[str, int] = Field(default_factory=dict)


class ExpertDataQualityV2(BaseModel):
    """Data-quality metadata for one expert packet."""

    model_config = ConfigDict(extra="allow")

    freshness: str = "unknown"
    as_of: Optional[str] = None
    source_chain: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ExpertPacketV2(BaseModel):
    """One v2 expert's output packet."""

    model_config = ConfigDict(extra="allow")

    expert: str
    dimension: str
    status: ExpertStatusV2 = "empty"
    seed_summary: SeedSummaryV2 = Field(default_factory=SeedSummaryV2)
    data_quality: ExpertDataQualityV2 = Field(default_factory=ExpertDataQualityV2)
    candidates: List[ExpertCandidateV2] = Field(default_factory=list)
    rejected: List[Dict[str, Any]] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    cache_hit: bool = False
