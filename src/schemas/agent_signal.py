# -*- coding: utf-8 -*-
"""Structured signal and trade-plan contracts for Agent analysis.

The models in this module are passive contracts.  They define the shape shared
by future L1/L2/L3 signal aggregation, deterministic risk gating, trace
artifacts, simulated trading and backtesting.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SignalCategory = Literal[
    "fundamental",
    "technical",
    "capital_flow",
    "sentiment",
    "event",
    "regime",
    "portfolio",
    "valuation",
    "chip",
    "other",
]
SignalDirection = Literal["bullish", "bearish", "neutral", "mixed", "unknown"]
DataQuality = Literal["sufficient", "limited", "insufficient", "failed", "unknown"]
TradeAction = Literal[
    "open",
    "add",
    "reduce",
    "sell",
    "hold",
    "wait",
    "monitor",
    "manual_review",
    "reject",
]
RiskGateStatus = Literal["passed", "blocked", "downgraded", "manual_review"]
RiskCheckSeverity = Literal["info", "warning", "blocking"]
OrderType = Literal["market", "limit", "condition", "manual"]


OPEN_ACTIONS = {"open", "add"}
EXIT_ACTIONS = {"reduce", "sell"}
PASSIVE_ACTIONS = {"hold", "wait", "monitor", "manual_review", "reject"}


class EvidenceRef(BaseModel):
    """Reference to a tool result, trace item, document or persisted artifact."""

    model_config = ConfigDict(extra="allow")

    source: str = Field(..., min_length=1, max_length=128)
    source_type: Literal["tool", "trace", "document", "memory", "manual", "other"] = "tool"
    ref_id: Optional[str] = Field(None, max_length=256)
    summary: Optional[str] = Field(None, max_length=1000)
    data_quality: DataQuality = "unknown"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class L1Signal(BaseModel):
    """Raw signal emitted by one tool, data source or deterministic detector."""

    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = Field(None, max_length=32)
    category: SignalCategory
    source: str = Field(..., min_length=1, max_length=128)
    direction: SignalDirection = "unknown"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    data_quality: DataQuality = "unknown"
    evidence: List[str] = Field(default_factory=list)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    as_of: Optional[str] = Field(None, max_length=64)
    raw: Dict[str, Any] = Field(default_factory=dict)


class L2SignalSummary(BaseModel):
    """Aggregated summary for one analysis category."""

    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = Field(None, max_length=32)
    category: SignalCategory
    direction: SignalDirection = "unknown"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    data_quality: DataQuality = "unknown"
    key_points: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    l1_signal_ids: List[str] = Field(default_factory=list)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)


class L3Decision(BaseModel):
    """Cross-category decision proposed by the analysis layer before risk gate."""

    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = Field(None, max_length=32)
    action: TradeAction
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = Field("", max_length=4000)
    data_quality: DataQuality = "unknown"
    l2_summary_ids: List[str] = Field(default_factory=list)
    stop_loss_price: Optional[float] = Field(None, ge=0)
    stop_loss_pct: Optional[float] = Field(None, ge=0, le=100)
    take_profit_price: Optional[float] = Field(None, ge=0)
    target_position_pct: Optional[float] = Field(None, ge=0, le=100)
    invalidation_conditions: List[str] = Field(default_factory=list)
    review_triggers: List[str] = Field(default_factory=list)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)

class TradePlan(BaseModel):
    """Executable plan candidate after an L3 decision and before order routing."""

    model_config = ConfigDict(extra="allow")

    symbol: str = Field(..., min_length=1, max_length=32)
    action: TradeAction
    order_type: OrderType = "manual"
    target_position_pct: Optional[float] = Field(None, ge=0, le=100)
    entry_price: Optional[float] = Field(None, ge=0)
    entry_zone_low: Optional[float] = Field(None, ge=0)
    entry_zone_high: Optional[float] = Field(None, ge=0)
    stop_loss_price: Optional[float] = Field(None, ge=0)
    stop_loss_pct: Optional[float] = Field(None, ge=0, le=100)
    take_profit_price: Optional[float] = Field(None, ge=0)
    invalidation_conditions: List[str] = Field(default_factory=list)
    execution_window: Optional[str] = Field(None, max_length=128)
    review_triggers: List[str] = Field(default_factory=list)
    notes: Optional[str] = Field(None, max_length=2000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_entry_zone(self) -> "TradePlan":
        """Keep price-zone shape valid while risk_gate decides action safety."""
        if (
            self.entry_zone_low is not None
            and self.entry_zone_high is not None
            and self.entry_zone_low > self.entry_zone_high
        ):
            raise ValueError("entry_zone_low cannot be greater than entry_zone_high")
        return self


class RiskGateCheck(BaseModel):
    """One deterministic risk-gate rule result."""

    model_config = ConfigDict(extra="allow")

    rule_id: str = Field(..., min_length=1, max_length=128)
    passed: bool
    severity: RiskCheckSeverity
    message: str = Field(..., min_length=1, max_length=1000)
    suggested_action: Optional[TradeAction] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class RiskGateResult(BaseModel):
    """Final deterministic gate result for a proposed action."""

    model_config = ConfigDict(extra="allow")

    status: RiskGateStatus
    original_action: TradeAction
    allowed_action: TradeAction
    checks: List[RiskGateCheck] = Field(default_factory=list)
    blocked_reasons: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    required_manual_review: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Return whether the proposed action can proceed unchanged."""
        return self.status == "passed"


class ReasoningTraceRef(BaseModel):
    """Link between structured outputs and the agent trace artifacts."""

    model_config = ConfigDict(extra="allow")

    session_id: Optional[str] = Field(None, max_length=128)
    artifact_dir: Optional[str] = Field(None, max_length=1024)
    planner_ref: Optional[str] = Field(None, max_length=256)
    evidence_ledger_ref: Optional[str] = Field(None, max_length=256)
    debate_ref: Optional[str] = Field(None, max_length=256)
    risk_gate_ref: Optional[str] = Field(None, max_length=256)


__all__ = [
    "DataQuality",
    "EvidenceRef",
    "EXIT_ACTIONS",
    "L1Signal",
    "L2SignalSummary",
    "L3Decision",
    "OPEN_ACTIONS",
    "OrderType",
    "PASSIVE_ACTIONS",
    "ReasoningTraceRef",
    "RiskCheckSeverity",
    "RiskGateCheck",
    "RiskGateResult",
    "RiskGateStatus",
    "SignalCategory",
    "SignalDirection",
    "TradeAction",
    "TradePlan",
]
