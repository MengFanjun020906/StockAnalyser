from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationStatus(StrEnum):
    GENERATED = "generated"
    OBSERVATION = "observation"
    NO_PLAN = "no_plan"


class AnchorStatus(StrEnum):
    ELIGIBLE = "eligible"
    OBSERVATION = "observation"
    REJECTED = "rejected"


class AnchorRole(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class ScenarioRole(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    OBSERVATION = "observation"


class ReachabilityBand(StrEnum):
    NEAR = "near"
    MEDIUM = "medium"
    FAR = "far"
    UNKNOWN = "unknown"


class PriceRelation(StrEnum):
    ABOVE = "above"
    INSIDE = "inside"
    BELOW = "below"
    INVALIDATED = "invalidated"


class ReviewConclusion(StrEnum):
    SUPPORT = "support"
    MIXED = "mixed"
    INSUFFICIENT = "insufficient"
    OPPOSING = "opposing"
    HARD_VETO = "hard_veto"


class PriceBarSnapshot(StrictModel):
    trade_date: date
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    adjustment_factor: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "PriceBarSnapshot":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must contain open, close and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must contain open, close and high")
        return self


class ChipSnapshot(StrictModel):
    trade_date: date
    avg_cost: Decimal | None = Field(default=None, gt=0)
    cost_70_low: Decimal | None = Field(default=None, gt=0)
    cost_70_high: Decimal | None = Field(default=None, gt=0)
    cost_90_low: Decimal | None = Field(default=None, gt=0)
    cost_90_high: Decimal | None = Field(default=None, gt=0)
    source: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class MarketRuleSnapshot(StrictModel):
    snapshot_id: str = Field(min_length=1)
    symbol: str | None = None
    venue: str = Field(min_length=1)
    board: str = Field(min_length=1)
    trade_date: date
    tick_size: Decimal = Field(gt=0)
    buy_lot_size: int = Field(default=100, gt=0)
    price_limit_ratio: Decimal | None = Field(default=None, gt=0)
    previous_close: Decimal | None = Field(default=None, gt=0)
    limit_up: Decimal | None = Field(default=None, gt=0)
    limit_down: Decimal | None = Field(default=None, gt=0)
    t_plus_one: bool
    st_status: bool = False
    st_status_verified: bool = False
    listing_status: str = "listed"
    listing_status_verified: bool = False
    instrument_type: str = "unknown"
    instrument_type_verified: bool = False
    metadata_observed_on: date | None = None
    suspended: bool = False
    calendar_version: str = "cn-calendar.v1"
    rule_source: str = "symbol_board_inference"
    rule_version: str = "cn-market-rules.v1"


class PricePlanSnapshot(StrictModel):
    schema_version: str = "price-plan-snapshot.v1"
    symbol: str = Field(min_length=1)
    market: Literal["cn"] = "cn"
    timeframe: Literal["1d"] = "1d"
    requested_as_of: date
    actual_as_of_session: date
    daily_bars: list[PriceBarSnapshot]
    weekly_bars: list[PriceBarSnapshot]
    last_cash_price: Decimal | None = Field(default=None, gt=0)
    price_basis: str = Field(min_length=1)
    price_scale_verified: bool
    adjustment_factor_semantics: str | None = None
    adjustment_factor_version: str | None = None
    history_source: str = Field(min_length=1)
    history_source_version: str | None = None
    market_rules: MarketRuleSnapshot
    chip: ChipSnapshot | None = None
    structure_evidence: dict[str, Any] | None = None
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_as_of_boundary(self) -> "PricePlanSnapshot":
        if self.actual_as_of_session > self.requested_as_of:
            raise ValueError("actual_as_of_session must not exceed requested_as_of")
        if any(bar.trade_date > self.actual_as_of_session for bar in self.daily_bars):
            raise ValueError("daily bars must be truncated at actual_as_of_session")
        if any(bar.trade_date > self.actual_as_of_session for bar in self.weekly_bars):
            raise ValueError("weekly bars must be truncated at actual_as_of_session")
        dates = [bar.trade_date for bar in self.daily_bars]
        if dates != sorted(set(dates)):
            raise ValueError("daily bars must have unique ascending trade dates")
        return self


class AnchorEvidence(StrictModel):
    anchor_id: str
    symbol: str
    market: Literal["cn"] = "cn"
    timeframe: Literal["1d"] = "1d"
    anchor_type: str
    source_family: Literal[
        "trend", "cost_volume", "price_structure", "volatility_boundary"
    ]
    price: Decimal = Field(gt=0)
    price_basis: Literal["cash"] = "cash"
    role: AnchorRole
    direction: Literal["rising", "flat", "falling", "event"]
    anchor_status: AnchorStatus
    as_of: date
    confirmation_date: date
    source_ref: str
    # Keep persisted v1 artifacts readable after source priority became explicit.
    source_priority: int = Field(default=1000, ge=0)
    distance_atr: Decimal | None = None
    age_bars: int = Field(ge=0)
    reaction_count: int = Field(ge=0)
    median_reaction_move_atr: Decimal = Field(ge=0)
    rule_version: str = "price-anchor.v1"
    calculation_fingerprint: str
    reason_codes: list[str] = Field(default_factory=list)


class RuleSpec(StrictModel):
    rule_type: str
    params: dict[str, Decimal | int | str | bool]
    rule_version: str


class ZoneScenario(StrictModel):
    scenario_id: str
    role: ScenarioRole
    base_rank: int = Field(ge=1)
    source_families: list[str]
    anchor_refs: list[str]
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    entry_reference: Decimal = Field(gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    target_1: Decimal | None = Field(default=None, gt=0)
    target_2: Decimal | None = Field(default=None, gt=0)
    target_1_exit_ratio: Decimal = Field(ge=0, le=1)
    reference_net_rr_1: Decimal | None = None
    reference_net_risk: Decimal | None = None
    reference_net_reward_1: Decimal | None = None
    reachability_band: ReachabilityBand
    price_relation: PriceRelation
    confirmation_rules: list[RuleSpec]
    invalidation_rules: list[RuleSpec]
    confirmation_seed_bars: list[PriceBarSnapshot] = Field(
        default_factory=list,
        max_length=5,
    )
    valid_from: date
    confirmation_deadline: date
    confirmation_valid_trading_days: int = Field(gt=0)
    order_valid_trading_days: int = Field(gt=0)
    scenario_reason_codes: list[str] = Field(default_factory=list)
    scenario_fingerprint: str

    @model_validator(mode="after")
    def validate_scenario(self) -> "ZoneScenario":
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")
        if self.entry_reference != self.entry_high:
            raise ValueError("long scenario entry_reference must equal entry_high")
        if self.stop_loss is not None and self.stop_loss >= self.entry_low:
            raise ValueError("long scenario stop_loss must be below entry_low")
        if self.target_1 is not None and self.target_1 <= self.entry_reference:
            raise ValueError("target_1 must be above entry_reference")
        if self.target_2 is not None and (
            self.target_1 is None or self.target_2 <= self.target_1
        ):
            raise ValueError("target_2 must be above target_1")
        if self.role is not ScenarioRole.OBSERVATION:
            if self.stop_loss is None or self.target_1 is None:
                raise ValueError("formal scenarios require stop_loss and target_1")
            if self.reference_net_rr_1 is None or self.reference_net_rr_1 <= 0:
                raise ValueError("formal scenarios require positive reference_net_rr_1")
            if not self.confirmation_rules or not self.invalidation_rules:
                raise ValueError("formal scenarios require machine rules")
        return self


class PricePlanArtifact(StrictModel):
    schema_version: str = "price-plan-artifact.v1"
    plan_id: str
    revision: int = Field(ge=1)
    supersedes_plan_id: str | None = None
    symbol: str
    market: Literal["cn"] = "cn"
    side: Literal["long"] = "long"
    timeframe: Literal["1d"] = "1d"
    requested_as_of: date
    actual_as_of_session: date
    generation_status: GenerationStatus
    scenarios: list[ZoneScenario] = Field(max_length=3)
    observation_anchors: list[AnchorEvidence]
    market_rules_snapshot_ref: str
    data_quality: Literal["sufficient", "limited", "invalid"]
    reason_codes: list[str]
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    algorithm_version: str
    params_version: str
    data_fingerprint: str
    calculation_fingerprint: str
    plan_fingerprint: str
    created_at: datetime
    trace_ref: str | None = None

    @model_validator(mode="after")
    def validate_generation_status(self) -> "PricePlanArtifact":
        formal = [item for item in self.scenarios if item.role is not ScenarioRole.OBSERVATION]
        if self.generation_status is GenerationStatus.NO_PLAN:
            if self.scenarios or not self.reason_codes:
                raise ValueError("no_plan requires no scenarios and non-empty reasons")
        elif self.generation_status is GenerationStatus.GENERATED and not formal:
            raise ValueError("generated requires at least one formal scenario")
        elif self.generation_status is GenerationStatus.OBSERVATION and formal:
            raise ValueError("observation cannot contain formal scenarios")
        return self


class PlanReviewArtifact(StrictModel):
    review_id: str
    plan_id: str
    scenario_id: str | None = None
    evidence_bundle_fingerprint: str
    prompt_version: str
    model_ref: str
    conclusion: ReviewConclusion
    evidence_refs: list[str]
    validated_output: dict[str, Any]
    dimension_reviews: list["DimensionPlanReview"] = Field(default_factory=list)
    scenario_impacts: list["ScenarioReviewImpact"] = Field(default_factory=list)
    review_status: Literal["completed", "failed", "pending"]
    review_fingerprint: str
    created_at: datetime


class DimensionPlanReview(StrictModel):
    """Absolute, price-less verdict for one evidence dimension."""

    dimension: Literal["technical", "capital", "fundamental", "intel"]
    scope: Literal["stock", "scenario"] = "stock"
    verdict: ReviewConclusion
    evidence_quality: Literal["ok", "partial", "unavailable"]
    review_status: Literal["completed", "failed", "not_run"] = "completed"
    execution_effect: Literal[
        "keep", "reduce_position", "observe_only", "veto"
    ]
    summary: str
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)


class ScenarioReviewImpact(StrictModel):
    """Deterministic permission merge for one immutable price scenario."""

    scenario_id: str
    math_effect: Literal["keep", "observe_only"]
    applicable_dimensions: list[
        Literal["technical", "capital", "fundamental", "intel"]
    ] = Field(default_factory=list)
    final_effect: Literal[
        "keep", "reduce_position", "observe_only", "veto"
    ]
    reason_codes: list[str] = Field(default_factory=list)


class AccountPlanDecision(StrictModel):
    decision_id: str
    plan_id: str
    decision_scope: str
    risk_profile: Literal["aggressive", "balanced", "conservative"]
    selected_scenario_id: str | None = None
    review_permission: str
    confirmation_policy: str
    position_sizing_ref: str | None = None
    target_position_pct: Decimal | None = Field(default=None, ge=0, le=100)
    account_net_rr_1: Decimal | None = None
    planned_quantity: int | None = Field(default=None, ge=0)
    account_cost_policy_ref: str | None = None
    account_context_absent: bool = False
    risk_gate_ref: str | None = None
    execution_status: str
    reason_codes: list[str] = Field(default_factory=list)
    input_fingerprint: str
    created_at: datetime


class PlanLifecycleState(StrictModel):
    plan_id: str
    scenario_id: str
    state_version: int = Field(ge=1)
    state: Literal[
        "waiting_touch", "touched", "confirmed", "invalidated", "expired", "ambiguous", "closed"
    ]
    first_touched_at: datetime | None = None
    confirmed_at: datetime | None = None
    invalidated_at: datetime | None = None
    expired_at: datetime | None = None
    last_evaluated_as_of: date
    last_event_id: str


class TradePlanResult(StrictModel):
    price_plan: PricePlanArtifact
    review: PlanReviewArtifact | None = None
    account_decision: AccountPlanDecision | None = None
    lifecycle_states: list[PlanLifecycleState] = Field(default_factory=list)
    execution_status: str
    reason_codes: list[str] = Field(default_factory=list)
