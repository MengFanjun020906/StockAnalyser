# -*- coding: utf-8 -*-
"""Schemas for first-stage candidate discovery experts."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


CandidateStance = Literal["support", "watch", "oppose", "invalid"]
CandidateExpertStatus = Literal["ok", "partial", "empty", "failed", "timeout", "unavailable"]


class CandidateDataQuality(BaseModel):
    """Data quality metadata for one candidate expert packet."""

    model_config = ConfigDict(extra="allow")

    freshness: str = "unknown"
    as_of: Optional[str] = None
    source_chain: List[Any] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ExpertCandidate(BaseModel):
    """One stock candidate produced by a discovery expert."""

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    score: float = Field(default=50.0, ge=0.0, le=100.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    stance: CandidateStance = "support"
    reason: str = ""
    evidence_refs: List[str] = Field(default_factory=list)
    reason_dimensions: List[Dict[str, Any]] = Field(default_factory=list)
    counter_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    valid_until: Optional[str] = None
    refresh_policy: str = "next_trading_day"
    raw: Dict[str, Any] = Field(default_factory=dict)


class ThemeWatchItem(BaseModel):
    """Theme/event observation that should not be treated as a stock candidate."""

    model_config = ConfigDict(extra="allow")

    theme: str
    event_title: str = ""
    status: str = "watch"
    reason: str = ""
    evidence_refs: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ExpertCandidatePacket(BaseModel):
    """One discovery expert's output before candidate merge and deep-dive."""

    model_config = ConfigDict(extra="allow")

    expert: str
    dimension: str
    status: CandidateExpertStatus = "empty"
    data_quality: CandidateDataQuality = Field(default_factory=CandidateDataQuality)
    themes: List[ThemeWatchItem] = Field(default_factory=list)
    candidates: List[ExpertCandidate] = Field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    timeout_s: Optional[float] = None

    def to_discovery_step(self) -> Dict[str, Any]:
        """Return a compact Trace step compatible with discover_watchlist_candidates."""
        payload: Dict[str, Any] = {
            "source": f"candidate_expert:{self.expert}",
            "expert": self.expert,
            "dimension": self.dimension,
            "status": self.status,
            "count": len(self.candidates),
            "theme_count": len(self.themes),
            "data_quality": self.data_quality.model_dump(mode="json"),
            "diagnostics": self.diagnostics,
        }
        if self.errors:
            payload["errors"] = self.errors
        if self.themes:
            payload["themes"] = [item.model_dump(mode="json") for item in self.themes]
        return payload
