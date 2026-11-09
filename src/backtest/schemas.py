# -*- coding: utf-8 -*-
"""Schemas and immutable value objects for the portfolio backtest engine.

The backtest engine is intentionally independent from Portfolio and from any
network/provider singleton.  These models are the only contract shared by the
snapshot builder, execution kernel, worker and HTTP layer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "backtest-experiment.v1"
ENGINE_VERSION = "portfolio-backtest.v1"
METRIC_VERSION = "portfolio-metrics.v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BacktestMode(StrEnum):
    RECORDED_PLAN_REPLAY = "recorded_plan_replay"
    HISTORICAL_RULE_STRATEGY = "historical_rule_strategy"
    HISTORICAL_AGENT_RESEARCH = "historical_agent_research"


class RunStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }


class Granularity(StrEnum):
    MINUTE_5 = "5m"
    DAILY = "1d"


class MissingDataPolicy(StrEnum):
    STRICT = "strict"
    DAILY_DOWNGRADE = "daily_downgrade"


class ScenarioSelection(StrEnum):
    ACCOUNT_DECISION = "account_decision"
    RANK_DIAGNOSTIC = "rank_diagnostic"


class CorporateActionPolicy(StrEnum):
    STRICT = "strict"
    ASSUME_NONE = "assume_none"


class SourcePipeline(StrEnum):
    """Workflow/sample source, orthogonal to the three backtest modes."""

    SELECTION_CONFIRMED = "selection_confirmed"
    SINGLE_STOCK = "single_stock"
    RESEARCH = "research"
    ANY = "any"


class ResearchFrequency(StrEnum):
    MONTHLY = "monthly"


class ResearchToolPolicy(StrEnum):
    CRITICAL_FAIL_CLOSED = "critical_fail_closed"


class ResearchUniverseMode(StrEnum):
    EXPLICIT = "explicit"
    AUTO_LOCAL_COVERAGE = "auto_local_coverage"


class ResearchConfirmationMode(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    LLM = "llm"


class ResearchEntryMode(StrEnum):
    ADAPTIVE = "adaptive"
    PULLBACK = "pullback"
    BREAKOUT = "breakout"


class RiskProfile(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class EndPolicy(StrEnum):
    MARK = "mark"
    LIQUIDATE = "liquidate"


class PriceBasis(StrEnum):
    MINUTE_UNADJUSTED = "minute_unadjusted"
    DAILY_DECLARED_UNADJUSTED = "daily_declared_unadjusted"
    DAILY_BASIS_UNVERIFIED = "daily_basis_unverified"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_MARKET = "stop_market"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    MAX_HOLD = "max_hold"
    END_OF_WINDOW = "end_of_window"


class DataQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class SessionStatus(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    MISSING = "missing"
    SUSPENDED = "suspended"
    PRE_LISTING = "pre_listing"
    MARKET_CLOSED = "market_closed"


class FeeRule(StrictModel):
    """Versioned A-share cash-fee schedule for one effective period."""

    effective_from: date
    effective_to: date | None = None
    commission_bps: Decimal = Field(default=Decimal("2.5"), ge=0)
    minimum_commission: Decimal = Field(default=Decimal("5"), ge=0)
    transfer_fee_bps: Decimal = Field(default=Decimal("0.1"), ge=0)
    stamp_duty_sell_bps: Decimal = Field(default=Decimal("5"), ge=0)
    stamp_duty_buy_bps: Decimal = Field(default=Decimal("0"), ge=0)

    def applies_on(self, trade_date: date) -> bool:
        if trade_date < self.effective_from:
            return False
        return self.effective_to is None or trade_date <= self.effective_to

    def validate_window(self) -> None:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")


class FeePolicy(StrictModel):
    """Ordered, versioned fee policy.

    Fee values are assumptions, not broker statements.  They are always
    versioned in the experiment fingerprint and must be shown in the UI.
    """

    version: str = Field(min_length=1)
    rules: list[FeeRule] = Field(min_length=1)
    description: str = ""

    def rule_for(self, trade_date: date) -> FeeRule:
        candidates = [rule for rule in self.rules if rule.applies_on(trade_date)]
        if not candidates:
            raise ValueError(f"no fee rule for {trade_date.isoformat()} in {self.version}")
        return max(candidates, key=lambda item: item.effective_from)

    def validate_policy(self) -> None:
        for rule in self.rules:
            rule.validate_window()
        ordered = sorted(self.rules, key=lambda item: item.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or previous.effective_to >= current.effective_from:
                raise ValueError("fee rule windows must not overlap")


class RiskPolicy(StrictModel):
    """Frozen account-level hard gates applied on top of every plan request.

    The plan/LLM may *request* a target position; this policy is the account
    authority that can only reduce it.  The resolved policy is embedded in the
    experiment config so a profile change cannot silently rewrite history.
    """

    policy_version: str = "risk-policy.v1"
    profile: RiskProfile = RiskProfile.BALANCED
    max_single_position_pct: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    max_total_exposure_pct: Decimal = Field(default=Decimal("80"), gt=0, le=100)
    max_concurrent_positions: int = Field(default=5, ge=1, le=100)
    min_cash_pct: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    risk_per_trade_pct: Decimal = Field(default=Decimal("1.0"), gt=0, le=100)
    require_stop_loss: bool = True
    allow_pyramiding: bool = False
    selection_top_n: int | None = Field(default=None, ge=1, le=500)

    @model_validator(mode="after")
    def validate_policy(self) -> "RiskPolicy":
        if self.max_single_position_pct > self.max_total_exposure_pct:
            raise ValueError("max_single_position_pct must not exceed max_total_exposure_pct")
        if self.max_total_exposure_pct + self.min_cash_pct > 100:
            raise ValueError("max_total_exposure_pct + min_cash_pct must not exceed 100")
        return self

    @property
    def effective_total_exposure_pct(self) -> Decimal:
        return min(self.max_total_exposure_pct, Decimal("100") - self.min_cash_pct)


RISK_POLICY_PRESETS: dict[RiskProfile, RiskPolicy] = {
    RiskProfile.CONSERVATIVE: RiskPolicy(
        profile=RiskProfile.CONSERVATIVE,
        max_single_position_pct=Decimal("10"),
        max_total_exposure_pct=Decimal("40"),
        max_concurrent_positions=5,
        min_cash_pct=Decimal("20"),
        risk_per_trade_pct=Decimal("0.5"),
        selection_top_n=10,
    ),
    RiskProfile.BALANCED: RiskPolicy(
        profile=RiskProfile.BALANCED,
        max_single_position_pct=Decimal("20"),
        max_total_exposure_pct=Decimal("80"),
        max_concurrent_positions=5,
        min_cash_pct=Decimal("10"),
        risk_per_trade_pct=Decimal("1.0"),
        selection_top_n=10,
    ),
    RiskProfile.AGGRESSIVE: RiskPolicy(
        profile=RiskProfile.AGGRESSIVE,
        max_single_position_pct=Decimal("40"),
        max_total_exposure_pct=Decimal("100"),
        max_concurrent_positions=3,
        min_cash_pct=Decimal("0"),
        risk_per_trade_pct=Decimal("2.0"),
        selection_top_n=10,
    ),
}


def risk_policy_for(profile: RiskProfile) -> RiskPolicy:
    if profile is RiskProfile.CUSTOM:
        raise ValueError("custom risk profile requires an explicit risk_policy")
    return RISK_POLICY_PRESETS[profile].model_copy(deep=True)


class SelectionLink(StrictModel):
    """Explicit parent link between a selection trace and a confirmation trace.

    P0 never guesses this relation.  Missing links are excluded with
    ``missing_selection_link`` and remain visible in the input audit.
    """

    link_version: str = "selection-link.v1"
    selection_trace_ref: str = Field(min_length=1)
    confirmation_trace_ref: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    source: str = "explicit"


class ResearchConfig(StrictModel):
    """Frozen P3 historical-Agent research parameters.

    P3 does not consume recorded plans.  The selection/confirmation Agent is
    re-run as-of each decision date against an explicit universe; executable
    prices come from a deterministic research plan, never from free-form LLM
    text.
    """

    universe: list[str] = Field(default_factory=list, max_length=50)
    universe_mode: ResearchUniverseMode = ResearchUniverseMode.EXPLICIT
    auto_universe_limit: int = Field(default=100, ge=1, le=500)
    confirmation_mode: ResearchConfirmationMode = ResearchConfirmationMode.AUTO_ACCEPT
    entry_mode: ResearchEntryMode = ResearchEntryMode.ADAPTIVE
    order_valid_trading_days: int = Field(default=15, ge=1, le=30)
    decision_frequency: ResearchFrequency = ResearchFrequency.MONTHLY
    selection_top_n: int = Field(default=5, ge=1, le=50)
    max_confirmations_per_decision: int = Field(default=5, ge=1, le=20)
    max_llm_calls: int = Field(default=500, ge=1, le=5000)
    min_history_bars: int = Field(default=40, ge=20, le=720)
    model_ref: str | None = None
    prompt_version: str = "research-prompt.v1"
    plan_version: str = "research-plan.v1"
    llm_timeout_seconds: int = Field(default=90, ge=10, le=600)
    tool_policy: ResearchToolPolicy = ResearchToolPolicy.CRITICAL_FAIL_CLOSED
    allow_tool_gaps: bool = True
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_research(self) -> "ResearchConfig":
        normalized = []
        for item in self.universe:
            digits = "".join(ch for ch in str(item) if ch.isdigit())[-6:]
            if not digits:
                raise ValueError(f"invalid A-share symbol in research universe: {item}")
            if digits not in normalized:
                normalized.append(digits)
        self.universe = normalized
        if self.universe_mode is ResearchUniverseMode.EXPLICIT:
            if not normalized:
                raise ValueError("explicit research universe cannot be empty")
            if self.selection_top_n > len(normalized):
                self.selection_top_n = len(normalized)
        else:
            # Auto mode resolves the candidate set from local as-of coverage.
            self.universe = []
        return self


class BenchmarkConfig(StrictModel):
    symbol: str = Field(min_length=1)
    price_basis: Literal["price", "total_return"] = "price"
    label: str = ""


class ExperimentConfig(StrictModel):
    """Immutable experiment request.

    ``notes`` is deliberately excluded from :meth:`normalized` so prose changes
    never create a new experiment fingerprint.
    """

    schema_version: str = SCHEMA_VERSION
    mode: BacktestMode = BacktestMode.RECORDED_PLAN_REPLAY
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK
    research: ResearchConfig | None = None
    start_date: date
    end_date: date
    initial_cash: Decimal = Field(default=Decimal("100000"), gt=0)
    currency: Literal["CNY"] = "CNY"
    start_flat: Literal[True] = True
    # Experiment-level hard ceiling.  The resolved RiskPolicy normally supplies
    # the actual 10/20/40% caps; these guard custom policies that exceed them.
    max_single_position_pct: Decimal = Field(default=Decimal("100"), gt=0, le=100)
    max_total_exposure_pct: Decimal = Field(default=Decimal("100"), gt=0, le=100)
    risk_profile: RiskProfile = RiskProfile.BALANCED
    risk_policy: RiskPolicy | None = None
    fee_policy_ref: str = Field(default="cn-a-share-2023.v1", min_length=1)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, le=1000)
    participation_rate: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    granularity: Granularity = Granularity.MINUTE_5
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.STRICT
    scenario_selection: ScenarioSelection = ScenarioSelection.ACCOUNT_DECISION
    corporate_action_policy: CorporateActionPolicy = CorporateActionPolicy.STRICT
    corporate_action_version: str | None = None
    end_policy: EndPolicy = EndPolicy.MARK
    max_hold_trading_days: int = Field(default=30, gt=0, le=1000)
    default_target_position_pct: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    annualization_days: int = Field(default=252, gt=0, le=366)
    risk_free_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    price_basis: PriceBasis = PriceBasis.MINUTE_UNADJUSTED
    daily_warmup_sessions: int = Field(default=60, ge=0, le=520)
    trace_ids: list[str] = Field(default_factory=list, max_length=500)
    plan_ids: list[str] = Field(default_factory=list, max_length=2000)
    symbols: list[str] = Field(default_factory=list, max_length=2000)
    include_observation_scenarios: bool = False
    require_account_decision: bool = True
    selection_links: list[SelectionLink] = Field(default_factory=list, max_length=2000)
    benchmark: BenchmarkConfig | None = None
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_config(self) -> "ExperimentConfig":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.mode is BacktestMode.HISTORICAL_RULE_STRATEGY:
            raise ValueError("historical_rule_strategy (P2) is not implemented yet")
        if self.mode is BacktestMode.RECORDED_PLAN_REPLAY:
            if self.source_pipeline not in {
                SourcePipeline.SELECTION_CONFIRMED,
                SourcePipeline.SINGLE_STOCK,
            }:
                raise ValueError("recorded_plan_replay requires selection_confirmed or single_stock")
            if self.research is not None:
                raise ValueError("research config is only valid for historical_agent_research")
        if self.mode is BacktestMode.HISTORICAL_AGENT_RESEARCH:
            if self.source_pipeline is not SourcePipeline.RESEARCH:
                raise ValueError("historical_agent_research requires source_pipeline=research")
            if self.research is None:
                raise ValueError("historical_agent_research requires research config")
            if self.plan_ids or self.trace_ids or self.selection_links:
                raise ValueError("research mode does not consume recorded plans, traces or selection links")
        if self.source_pipeline is SourcePipeline.ANY:
            raise ValueError("P0/P3 requires a single source_pipeline; do not mix samples")
        if self.max_single_position_pct > self.max_total_exposure_pct:
            raise ValueError("max_single_position_pct must not exceed max_total_exposure_pct")
        if self.risk_policy is None:
            if self.risk_profile is RiskProfile.CUSTOM:
                raise ValueError("custom risk profile requires an explicit risk_policy")
            self.risk_policy = risk_policy_for(self.risk_profile)
        else:
            self.risk_profile = self.risk_policy.profile
        if self.risk_policy.max_single_position_pct > self.max_single_position_pct:
            self.risk_policy = self.risk_policy.model_copy(
                update={"max_single_position_pct": self.max_single_position_pct}
            )
        if self.risk_policy.max_total_exposure_pct > self.max_total_exposure_pct:
            self.risk_policy = self.risk_policy.model_copy(
                update={"max_total_exposure_pct": self.max_total_exposure_pct}
            )
        if self.granularity is Granularity.DAILY:
            if self.missing_data_policy is MissingDataPolicy.DAILY_DOWNGRADE:
                raise ValueError("daily granularity cannot use daily_downgrade")
        if self.corporate_action_policy is CorporateActionPolicy.STRICT:
            if not self.corporate_action_version:
                # The run remains runnable but is flagged partial by precheck.
                pass
        return self

    def normalized(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude={"notes"})
        return _normalize_fingerprint_value(payload)


class ReplayPlan(StrictModel):
    """Normalized, machine-executable plan derived from one price artifact."""

    plan_key: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    source_type: Literal["price_plan_artifact", "research_rule"] = "price_plan_artifact"
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK
    selection_trace_ref: str | None = None
    confirmation_trace_ref: str | None = None
    link_quality: Literal["explicit", "single_stock", "missing"] = "single_stock"
    source_revision: int = Field(ge=1)
    source_hash: str = Field(min_length=1)
    trace_refs: list[str] = Field(default_factory=list)
    symbol: str = Field(min_length=1)
    market: Literal["cn"] = "cn"
    decision_id: str | None = None
    decision_status: str = ""
    scenario_id: str = Field(min_length=1)
    scenario_role: str = ""
    action: Literal["open_long"] = "open_long"
    available_at: datetime
    available_at_inferred: bool = False
    valid_from: date
    valid_until: date
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    entry_reference: Decimal = Field(gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    target_1: Decimal | None = Field(default=None, gt=0)
    target_2: Decimal | None = Field(default=None, gt=0)
    target_1_exit_ratio: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    order_valid_trading_days: int = Field(default=2, gt=0, le=60)
    max_hold_trading_days: int = Field(default=30, gt=0, le=1000)
    target_position_pct: Decimal = Field(default=Decimal("20"), gt=0, le=100)
    rank: int = Field(default=999, ge=1)
    requires_confirmation: bool = False
    confirmation_valid_trading_days: int = Field(default=0, ge=0, le=60)
    confirmation_deadline: date | None = None
    scenario_json: dict[str, Any] = Field(default_factory=dict)
    quality_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self) -> "ReplayPlan":
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")
        if self.stop_loss is not None and self.stop_loss >= self.entry_low:
            raise ValueError("stop_loss must be below entry_low")
        if self.valid_until < self.valid_from:
            raise ValueError("valid_until must not precede valid_from")
        return self

    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(
            json.dumps(
                _normalize_fingerprint_value(
                    self.model_dump(mode="python", exclude={"scenario_json"})
                ),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


class PlanInputRecord(StrictModel):
    """One considered input, included or excluded, for auditability."""

    input_id: str
    source_type: str = "price_plan_artifact"
    source_id: str
    source_pipeline: SourcePipeline | None = None
    selection_trace_ref: str | None = None
    confirmation_trace_ref: str | None = None
    link_quality: str | None = None
    plan_id: str | None = None
    revision: int | None = None
    scenario_id: str | None = None
    decision_id: str | None = None
    trace_refs: list[str] = Field(default_factory=list)
    symbol: str | None = None
    available_at: datetime | None = None
    valid_from: date | None = None
    included: bool
    exclusion_reason: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    payload_hash: str | None = None


class CoverageRow(StrictModel):
    symbol: str
    sessions_expected: int = 0
    sessions_full: int = 0
    sessions_partial: int = 0
    sessions_missing: int = 0
    sessions_suspended: int = 0
    sessions_pre_listing: int = 0
    missing_bars: int = 0
    expected_bars_per_session: int = 48
    first_session: date | None = None
    last_session: date | None = None
    quality: DataQuality = DataQuality.PARTIAL
    flags: list[str] = Field(default_factory=list)


class GapRecord(StrictModel):
    symbol: str
    session: date
    status: SessionStatus
    actual_bars: int
    expected_bars: int
    first_bar_at: datetime | None = None
    last_bar_at: datetime | None = None


class SnapshotManifest(StrictModel):
    schema_version: str = "backtest-snapshot-manifest.v1"
    run_id: str
    created_at: datetime
    granularity: Granularity
    start_date: date
    end_date: date
    sessions: list[date] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    bar_count: int = 0
    daily_bar_count: int = 0
    minute_bar_count: int = 0
    first_bar_at: datetime | None = None
    last_bar_at: datetime | None = None
    providers: list[str] = Field(default_factory=list)
    price_basis: PriceBasis
    volume_unit: str = "shares"
    content_hash: str = ""
    file_sha256: str | None = None
    coverage: list[CoverageRow] = Field(default_factory=list)
    gaps: list[GapRecord] = Field(default_factory=list)
    rules_version: str = "cn-market-rules.v1"
    calendar_version: str = "cn-calendar.v1"
    corporate_action_policy: CorporateActionPolicy
    corporate_action_version: str | None = None
    corporate_action_count: int = 0
    benchmark_status: Literal["available", "unavailable", "not_requested"] = "not_requested"
    benchmark_symbol: str | None = None
    quality: DataQuality = DataQuality.PARTIAL
    quality_flags: list[str] = Field(default_factory=list)


class PrecheckRequest(StrictModel):
    config: ExperimentConfig


class PrecheckResult(StrictModel):
    schema_version: str = "backtest-precheck.v1"
    config_hash: str
    generated_at: datetime
    status: Literal["ok", "warning", "blocked"] = "ok"
    included_plans: int = 0
    excluded_plans: int = 0
    symbols: list[str] = Field(default_factory=list)
    sessions: int = 0
    expected_bars_per_session: int = 48
    coverage: list[CoverageRow] = Field(default_factory=list)
    gaps: list[GapRecord] = Field(default_factory=list)
    inputs: list[PlanInputRecord] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    quality: DataQuality = DataQuality.PARTIAL


class MetricValue(StrictModel):
    value: float | None = None
    unit: str = ""
    reason: str | None = None


class BacktestMetrics(StrictModel):
    metric_version: str = METRIC_VERSION
    quality: DataQuality = DataQuality.PARTIAL
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK
    risk_profile: RiskProfile = RiskProfile.BALANCED
    total_return_pct: float | None = None
    annualized_return_pct: float | None = None
    volatility_pct: float | None = None
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_start: date | None = None
    max_drawdown_trough: date | None = None
    max_drawdown_recovery: date | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    cash_ratio_pct: float | None = None
    average_position_pct: float | None = None
    turnover_pct: float | None = None
    fee_drag_pct: float | None = None
    slippage_drag_pct: float | None = None
    trading_days: int = 0
    daily_return_count: int = 0
    not_applicable: dict[str, str] = Field(default_factory=dict)
    single_trade: dict[str, Any] = Field(default_factory=dict)


class RunSummary(StrictModel):
    run_id: str
    mode: BacktestMode
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK
    risk_profile: RiskProfile = RiskProfile.BALANCED
    status: RunStatus
    attempt: int = 1
    request_fingerprint: str
    experiment_fingerprint: str | None = None
    reused_from_run_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: float = 0.0
    phase: str = ""
    data_quality: DataQuality = DataQuality.PARTIAL
    error_code: str | None = None
    error_message: str | None = None
    result_dir: str | None = None
    metrics: BacktestMetrics | None = None
    config: ExperimentConfig | None = None
    quality_flags: list[str] = Field(default_factory=list)


class RunDetail(RunSummary):
    manifest: SnapshotManifest | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    coverage: list[CoverageRow] = Field(default_factory=list)
    inputs: list[PlanInputRecord] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


def _normalize_fingerprint_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_fingerprint_value(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize_fingerprint_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_fingerprint_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_fingerprint_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def fingerprint_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
