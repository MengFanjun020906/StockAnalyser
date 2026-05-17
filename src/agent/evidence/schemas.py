# -*- coding: utf-8 -*-
"""Pydantic schemas for compact evidence transfer between experts."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


EvidenceStatus = Literal["ok", "partial", "empty", "stale", "failed", "timeout", "not_supported"]
Freshness = Literal["realtime", "intraday", "eod_current", "recent", "stale", "unknown"]
EvidenceStance = Literal["support", "oppose", "neutral", "wait_confirm", "invalid"]
ActionBias = Literal["open", "add", "hold", "reduce", "exit", "wait", "reject"]
SignalDirection = Literal["positive", "negative", "neutral", "mixed", "unknown"]
SignalStrength = Literal["weak", "medium", "strong", "extreme"]
Severity = Literal["low", "medium", "high", "veto"]


class StockRef(BaseModel):
    """Canonical stock identity carried by every evidence unit."""

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"


class DataQuality(BaseModel):
    """Data validity, freshness and source diagnostics."""

    model_config = ConfigDict(extra="allow")

    status: EvidenceStatus = "partial"
    as_of: Optional[str] = None
    freshness: Freshness = "unknown"
    source: str = ""
    source_chain: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)


class EvidenceSignal(BaseModel):
    """One compact signal extracted from a raw tool result."""

    model_config = ConfigDict(extra="allow")

    name: str
    value: Optional[Any] = None
    unit: Optional[str] = None
    direction: SignalDirection = "unknown"
    strength: SignalStrength = "weak"
    score_delta: float = Field(default=0.0, ge=-25.0, le=25.0)
    change: Dict[str, Any] = Field(default_factory=dict)
    interpretation: str = ""


class EvidenceImpact(BaseModel):
    """Decision impact produced by an evidence card."""

    model_config = ConfigDict(extra="allow")

    stance: EvidenceStance = "neutral"
    action_bias: ActionBias = "wait"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    score_delta: float = Field(default=0.0, ge=-25.0, le=25.0)
    reason: str = ""


class CounterEvidence(BaseModel):
    """A refutation against a trading hypothesis."""

    model_config = ConfigDict(extra="allow")

    refuted_claim: str
    refutation: str
    severity: Severity = "medium"


class EvidenceExpiry(BaseModel):
    """Validity window metadata."""

    model_config = ConfigDict(extra="allow")

    valid_until: Optional[str] = None
    refresh_trigger: str = ""
    window: str = ""


class EvidenceCard(BaseModel):
    """Compact evidence card visible to experts."""

    model_config = ConfigDict(extra="allow")

    card_id: str
    run_id: str
    stock: StockRef
    dimension: str
    producer: Dict[str, Any] = Field(default_factory=dict)
    data_quality: DataQuality = Field(default_factory=DataQuality)
    signals: List[EvidenceSignal] = Field(default_factory=list)
    impact: EvidenceImpact = Field(default_factory=EvidenceImpact)
    counter_evidence: List[CounterEvidence] = Field(default_factory=list)
    expiry: EvidenceExpiry = Field(default_factory=EvidenceExpiry)
    raw_ref: str = ""


class ExpertEvidencePacket(BaseModel):
    """One expert's compact output for Judge and downstream experts."""

    model_config = ConfigDict(extra="allow")

    packet_id: str
    expert: str
    dimension: str
    stock: Optional[StockRef] = None
    stance: EvidenceStance = "invalid"
    action_bias: ActionBias = "wait"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    top_supports: List[str] = Field(default_factory=list)
    top_risks: List[str] = Field(default_factory=list)
    key_cards: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    recommended_next_tools: List[Dict[str, Any]] = Field(default_factory=list)
    raw_refs: List[str] = Field(default_factory=list)


class JudgeInputPacket(BaseModel):
    """Compact Judge input. Raw tool JSON must not be embedded here."""

    model_config = ConfigDict(extra="allow")

    stock: Optional[StockRef] = None
    expert_packets: Dict[str, ExpertEvidencePacket] = Field(default_factory=dict)
    hard_constraints: Dict[str, Any] = Field(default_factory=dict)
    decision_matrix: List[Dict[str, Any]] = Field(default_factory=list)
    top_counter_evidence: List[str] = Field(default_factory=list)
