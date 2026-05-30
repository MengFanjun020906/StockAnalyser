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

from dataclasses import dataclass, field as dc_field
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
    "daily_screener",
    "user_watchlist",
    "local_price_volume",
    "limit_up_pool",
    "hot_rank",
    "strong_sector",
    "sector_theme",
    "event_impact",
    "news_momentum",
    "capital_flow_anomaly",
    "northbound_stock_connect",
    "margin_financing",
    "block_trade",
    "dragon_tiger",
    "valuation_liquidity",
    "alphasift",
    "sequoia",
    "fundamental_snapshot",
    "low_base_structure",
    "fallback",
]


# ---------------------------------------------------------------------------
# P3 — 召回层数据结构
# ---------------------------------------------------------------------------

FeatureFlagKind = Literal[
    "pattern", "capital", "limit", "fundamental", "position", "sector", "news", "unknown"
]


class FeatureFlag(BaseModel):
    """One detector's observation for a single stock (objective fact, no score).

    ``detector`` follows the naming convention ``{source}:{signal}`` (e.g.
    ``moneyflow_ths``, ``screener:ma_breakout``, ``low_base:range_low``).
    ``metrics`` stores raw numbers — never normalised or scored.
    """

    model_config = ConfigDict(extra="allow")

    detector: str
    kind: FeatureFlagKind = "unknown"
    summary: str = ""
    metrics: Dict[str, Any] = Field(default_factory=dict)
    as_of: str = "unknown"


class FeatureRow(BaseModel):
    """One stock's recall-layer record: merged feature flags from all detectors.

    ``flags`` grows by merging FeatureFlags from every detector that fired;
    ``recall_sources`` records which source buckets contributed (audit trail).
    ``coarse_kept=False`` rows are in the RecallResult.all_rows list but are NOT
    passed to desks.  ``fact_sheet`` is populated by build_recall_pool Phase A.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    flags: List[FeatureFlag] = Field(default_factory=list)
    fact_sheet: Optional["FactSheet"] = None
    recall_sources: List[str] = Field(default_factory=list)
    coarse_kept: bool = True
    coarse_drop_reason: str = ""


@dataclass
class RecallResult:
    """Output of build_recall_pool(): kept rows + diagnostics."""

    rows: List[FeatureRow]                     # coarse_kept=True only
    all_rows: List[FeatureRow]                 # includes dropped rows
    diagnostics: List[Dict[str, Any]] = dc_field(default_factory=list)
    coarse_truncated: bool = False
    hit_count_hist: Dict[int, int] = dc_field(default_factory=dict)
    sources: Dict[str, int] = dc_field(default_factory=dict)
    total_in: int = 0
    total_kept: int = 0


# ---------------------------------------------------------------------------
# Seed pool (pre-P3 / existing)
# ---------------------------------------------------------------------------


class SeedItem(BaseModel):
    """One candidate seed shared across experts before LLM filtering."""

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    source: SeedSource = "fallback"
    hint: str = ""
    trigger_signals: List[Dict[str, Any]] = Field(default_factory=list)
    priority_score: float = 0.0
    freshness: str = "unknown"
    context_hint: str = ""
    extras: Dict[str, Any] = Field(default_factory=dict)


class FactSheet(BaseModel):
    """Deterministic fact底表 computed once per stock.

    全委员会共享的同一份确定性事实(资金方向/趋势/位置分位/量比/乖离/板块强弱/
    硬风险)。只陈述可证伪事实,不打分/不排序;红线 bool 默认 False(阈值留空时
    永不触发,见 veto_gate)。
    """

    model_config = ConfigDict(extra="allow")

    code: str
    # 资金(确定性,来自 get_capital_flow / moneyflow_ths,缺失填 unknown)
    capital_direction: Literal["inflow", "outflow", "neutral", "unknown"] = "unknown"
    capital_violent_outflow: bool = False  # 红线候选:放量主力夺路出逃
    # 趋势/位置(本地日线算)
    trend_state: Literal["bullish", "neutral", "bearish", "unknown"] = "unknown"
    breakdown_accelerating: bool = False  # 红线候选:有效跌破关键支撑且加速下跌
    range_pct_60: Optional[float] = None  # 60日区间分位 0-1
    range_pct_120: Optional[float] = None
    dist_to_high_20: Optional[float] = None  # 距20日高点 %
    gain_5d: Optional[float] = None  # 近5日涨幅 %
    bias_ma20: Optional[float] = None  # 距MA20乖离 %
    volume_ratio: Optional[float] = None  # 量比
    rsi14: Optional[float] = None
    # 流动性 & 硬风险
    avg_turnover_20: Optional[float] = None
    liquidity_ok: bool = True
    hard_risk_flags: List[str] = Field(default_factory=list)  # st/delist/suspended/...
    # 板块/题材(共享上下文,替代独立题材席)
    sector_name: str = ""
    sector_strength: Literal["strong", "neutral", "weak", "unknown"] = "unknown"
    sector_rank_pct: Optional[float] = None
    leader_already_up: Optional[bool] = None  # 板块龙头是否已涨(补涨判断用)
    freshness: str = "unknown"
    warnings: List[str] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# P4 — 打法席位汇总数据结构
# ---------------------------------------------------------------------------


class AggregatedCandidate(BaseModel):
    """One stock that has been picked by ≥1 thesis desk (or placed in observe pool).

    ``confidence`` is computed deterministically from tool coverage rate — LLM-
    reported scores are discarded.  ``conflict_flags`` are forwarded to deep-dive
    so the playbook can explicitly arbitrate them.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    name: str = ""
    market: str = "cn"
    setup_type: CandidateSetupTypeV2 = "unknown"
    setup_subtype: Optional[str] = None
    desks: List[str] = Field(default_factory=list)
    primary_desk: str = ""
    stance_by_desk: Dict[str, str] = Field(default_factory=dict)
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    multi_desk_conviction: bool = False
    conflict_flags: List[str] = Field(default_factory=list)
    fact_sheet: Optional["FactSheet"] = None
    evidence_by_desk: Dict[str, List[EvidenceItem]] = Field(default_factory=dict)
    risks: List[RiskNote] = Field(default_factory=list)
    observe_only: bool = False


class AggregatedPool(BaseModel):
    """Output of aggregate_desk_picks(): per-desk sorted picks + metadata."""

    model_config = ConfigDict(extra="allow")

    by_desk: Dict[str, List[AggregatedCandidate]] = Field(default_factory=dict)
    vetoed: List[Dict[str, Any]] = Field(default_factory=list)
    observe: List[AggregatedCandidate] = Field(default_factory=list)
    regime: str = "unknown"
    diagnostics: List[Dict[str, Any]] = Field(default_factory=list)
