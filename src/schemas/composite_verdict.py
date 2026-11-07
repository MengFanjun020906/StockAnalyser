"""Contracts for deterministic composite stock scoring."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChanSignal(BaseModel):
    """One deterministic Chan evidence label produced by the structure layer."""

    model_config = ConfigDict(extra="allow")

    name: str
    timeframe: str = "1d"
    triggered: bool = False
    metrics: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[str] = Field(default_factory=list)


class ChanFeatureSnapshot(BaseModel):
    """Chan signals available at one as-of boundary."""

    status: Literal["ok", "partial", "insufficient"] = "ok"
    signals: List[ChanSignal] = Field(default_factory=list)


class S4FeatureSnapshot(BaseModel):
    """Strategy-4 features; unavailable semantics remain explicitly missing."""

    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "partial", "not_implemented"] = "not_implemented"
    region_pass: Optional[bool] = None
    resonance_count: Optional[int] = Field(None, ge=0, le=4)
    volume_ratio_5d: Optional[float] = None
    volatility_pct: Optional[float] = None
    gain_10d_pct: Optional[float] = None
    golden_cross_today: Optional[bool] = None
    all_ok: Optional[bool] = None
    guide_line: Optional[float] = None
    ma57: Optional[float] = None
    ma57_rising: Optional[bool] = None
    buy_line: Optional[float] = None
    sell_line: Optional[float] = None
    daily_bull: Optional[bool] = None
    daily_strong: Optional[bool] = None
    weekly_bull: Optional[bool] = None
    weekly_strong: Optional[bool] = None
    weekly_guide_line: Optional[float] = None
    weekly_ma57: Optional[float] = None
    weekly_ma57_rising: Optional[bool] = None
    weekly_buy_line: Optional[float] = None
    weekly_sell_line: Optional[float] = None
    weekly_provisional: bool = False
    missing_features: List[str] = Field(default_factory=list)


class BacktestWinRate(BaseModel):
    """Signal-level Strategy-4 backtest result."""

    status: Literal["ok", "insufficient_sample", "insufficient_data", "unavailable"] = "unavailable"
    sample_count: int = Field(0, ge=0)
    win_count: int = Field(0, ge=0)
    loss_count: int = Field(0, ge=0)
    win_rate_pct: Optional[float] = None
    reason: Optional[str] = None
    rule_version: str = "backtest_win_rate.v1"


class CompositeScoreSnapshot(BaseModel):
    """Normalized, provenance-carrying input consumed by the pure evaluator."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    as_of: str
    quote_basis: Literal["intraday", "eod_current", "latest_trading_day", "unknown"] = "unknown"
    data_quality: Literal["sufficient", "limited", "insufficient", "unknown"] = "unknown"
    holding: bool = False
    s4: S4FeatureSnapshot = Field(default_factory=S4FeatureSnapshot)
    chan: ChanFeatureSnapshot = Field(default_factory=ChanFeatureSnapshot)
    backtest: BacktestWinRate = Field(
        default_factory=lambda: BacktestWinRate(
            status="unavailable",
            reason="backtest_loader_not_configured",
        )
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CompositeScoreComponent(BaseModel):
    """One component's score and audit breakdown."""

    status: Literal["ok", "partial", "not_implemented", "not_evaluable"]
    score: Optional[int] = None
    provisional_score: Optional[int] = None
    scale_max: int
    breakdown: Dict[str, int] = Field(default_factory=dict)
    provisional_breakdown: Dict[str, int] = Field(default_factory=dict)
    missing_features: List[str] = Field(default_factory=list)


class CompositeScoreComponents(BaseModel):
    s4: CompositeScoreComponent
    chan: CompositeScoreComponent


class ExitWarning(BaseModel):
    """Independent escape-warning result derived from deterministic signals."""

    level: int = Field(0, ge=0, le=3)
    items: List[str] = Field(default_factory=list)


class CompositeVerdict(BaseModel):
    """Shadow-mode mathematical verdict, ready for Trace persistence."""

    symbol: str
    as_of: str
    verdict_status: Literal["evaluable", "not_evaluable"]
    score: Optional[int] = None
    provisional_score: Optional[int] = None
    scale_max: int = 20
    components: CompositeScoreComponents
    signal_level: Literal[
        "strong_confirmed",
        "buy_valid",
        "buy_watch",
        "buy_transition",
        "observe_or_exit",
        "not_evaluable",
    ] = "not_evaluable"
    shadow_mode: bool = True
    entry_permission: Literal["blocked", "conditional", "allowed", "not_evaluable"] = "not_evaluable"
    target_position_cap_pct: Optional[float] = None
    target_position_cap_ratio: Optional[float] = Field(None, ge=0, le=1)
    position_tier: Optional[Literal["S", "A", "B"]] = None
    market_coefficient: Optional[float] = Field(None, ge=0.25, le=1.0)
    required_holding_action: Literal["none", "reduce", "exit", "not_evaluable"] = "not_evaluable"
    reduce_to_ratio: Optional[float] = Field(None, ge=0, le=1)
    backtest: Optional[Dict[str, Any]] = None
    adjustments: List[Dict[str, Any]] = Field(default_factory=list)
    exit_warning: ExitWarning = Field(default_factory=ExitWarning)
    missing_features: List[str] = Field(default_factory=list)
    formalization_reason_codes: List[str] = Field(default_factory=list)
    quality_warning_codes: List[str] = Field(default_factory=list)
    strategy4_rule_version: str = "strategy4.v1"
    rule_version: str = "stock-composite-shadow-v1"


__all__ = [
    "BacktestWinRate",
    "ChanFeatureSnapshot",
    "ChanSignal",
    "CompositeScoreComponent",
    "CompositeScoreComponents",
    "CompositeScoreSnapshot",
    "CompositeVerdict",
    "ExitWarning",
    "S4FeatureSnapshot",
]
