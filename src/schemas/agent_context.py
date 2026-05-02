# -*- coding: utf-8 -*-
"""Agent user-context contracts for planning/execution analysis.

These schemas are intentionally passive in phase 1.  They document and validate
the shape that later Agent planning, account-aware reports, and UI/API payloads
should share without changing the current runtime analysis path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


RiskPreference = Literal["conservative", "balanced", "aggressive"]
TradingHorizon = Literal["intraday", "short_term", "swing", "medium_term", "long_term"]
MarketRegion = Literal["cn", "hk", "us", "mixed"]
AccountType = Literal["cash", "margin", "simulated", "retirement", "other"]
MarginMode = Literal["none", "margin", "short", "margin_and_short"]
CostMethod = Literal["fifo", "avg"]
ReportIntent = Literal[
    "auto",
    "position_review",
    "entry_analysis",
    "watchlist_scan",
    "risk_review",
    "event_impact",
    "qa",
]
AnalysisMode = Literal["normal", "planning_execute", "agent_react", "multi_agent"]


class InvestorProfile(BaseModel):
    """User-level investing preferences that should condition Agent decisions."""

    model_config = ConfigDict(extra="allow")

    profile_id: Optional[str] = Field(None, max_length=64)
    display_name: Optional[str] = Field(None, max_length=64)
    risk_preference: RiskPreference = "balanced"
    trading_horizon: TradingHorizon = "swing"
    preferred_markets: List[MarketRegion] = Field(default_factory=lambda: ["cn"])
    max_single_position_pct: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Maximum target exposure for one stock as a percentage of total equity.",
    )
    max_total_equity_exposure_pct: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Maximum total stock exposure as a percentage of total equity.",
    )
    max_acceptable_drawdown_pct: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Maximum acceptable portfolio drawdown before recommendations should become defensive.",
    )
    default_stop_loss_pct: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Default stop-loss percentage when no symbol-specific stop is provided.",
    )
    allow_margin: bool = False
    allow_short_selling: bool = False
    notes: Optional[str] = Field(None, max_length=1000)


class AccountContext(BaseModel):
    """Account constraints used by account-aware Agent reports.

    This is a lightweight decision context and should not replace the existing
    portfolio ledger in ``PortfolioService``.
    """

    model_config = ConfigDict(extra="allow")

    account_id: Optional[int] = None
    account_name: Optional[str] = Field(None, max_length=64)
    broker: Optional[str] = Field(None, max_length=64)
    account_type: AccountType = "cash"
    margin_mode: MarginMode = "none"
    market: MarketRegion = "cn"
    base_currency: str = Field("CNY", min_length=3, max_length=8)
    total_equity: Optional[float] = Field(None, ge=0)
    available_cash: Optional[float] = Field(None, ge=0)
    total_market_value: Optional[float] = Field(None, ge=0)
    margin_debt: Optional[float] = Field(None, ge=0)
    maintenance_ratio: Optional[float] = Field(
        None,
        ge=0,
        description="Broker maintenance or collateral ratio when available.",
    )
    risk_line_ratio: Optional[float] = Field(
        None,
        ge=0,
        description="Broker warning/liquidation reference ratio when available.",
    )
    cost_method: CostMethod = "fifo"
    notes: Optional[str] = Field(None, max_length=1000)


class PositionContext(BaseModel):
    """Symbol-level position context for position-aware decisions."""

    model_config = ConfigDict(extra="allow")

    symbol: str = Field(..., min_length=1, max_length=32)
    market: Optional[MarketRegion] = None
    account_id: Optional[int] = None
    quantity: float = Field(..., ge=0)
    avg_cost: Optional[float] = Field(None, ge=0)
    total_cost: Optional[float] = Field(None, ge=0)
    last_price: Optional[float] = Field(None, ge=0)
    market_value: Optional[float] = Field(None, ge=0)
    unrealized_pnl: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    position_pct: Optional[float] = Field(
        None,
        ge=0,
        le=100,
        description="Position weight within account or total portfolio.",
    )
    holding_days: Optional[int] = Field(None, ge=0)
    stop_loss: Optional[float] = Field(None, ge=0)
    take_profit: Optional[float] = Field(None, ge=0)
    thesis: Optional[str] = Field(None, max_length=1000)
    notes: Optional[str] = Field(None, max_length=1000)


class ReportContext(BaseModel):
    """Requested report intent and execution style."""

    model_config = ConfigDict(extra="allow")

    intent: ReportIntent = "auto"
    analysis_mode: AnalysisMode = "normal"
    target_symbols: List[str] = Field(default_factory=list)
    primary_symbol: Optional[str] = Field(None, max_length=32)
    language: Literal["zh", "en"] = "zh"
    include_entry_plan: bool = True
    include_position_plan: bool = True
    include_risk_review: bool = True
    include_watchlist_ranking: bool = False
    user_prompt: Optional[str] = Field(None, max_length=4000)


class AgentUserContext(BaseModel):
    """Top-level context contract for future account-aware Agent planning."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "2026-05-02"
    investor: InvestorProfile = Field(default_factory=InvestorProfile)
    accounts: List[AccountContext] = Field(default_factory=list)
    positions: List[PositionContext] = Field(default_factory=list)
    report: ReportContext = Field(default_factory=ReportContext)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def has_position_for(self, symbol: str) -> bool:
        """Return whether the context has a non-zero position for *symbol*."""
        normalized = (symbol or "").strip().lower()
        if not normalized:
            return False
        return any(
            (position.symbol or "").strip().lower() == normalized and position.quantity > 0
            for position in self.positions
        )


__all__ = [
    "AccountContext",
    "AccountType",
    "AgentUserContext",
    "AnalysisMode",
    "CostMethod",
    "InvestorProfile",
    "MarginMode",
    "MarketRegion",
    "PositionContext",
    "ReportContext",
    "ReportIntent",
    "RiskPreference",
    "TradingHorizon",
]
