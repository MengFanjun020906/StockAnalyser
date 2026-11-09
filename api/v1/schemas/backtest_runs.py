# -*- coding: utf-8 -*-
"""API schemas for the portfolio backtest run resource."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from src.backtest.schemas import ExperimentConfig


class CreateBacktestRunRequest(BaseModel):
    config: ExperimentConfig
    allow_incomplete: bool = Field(
        False,
        description="允许在不完整数据上运行并标记 partial；默认由 precheck 阻断",
    )


class PrecheckJobRequest(BaseModel):
    config: ExperimentConfig


class SyncMinuteBarsRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list, max_length=2000)
    start_date: date
    end_date: date
    frequency: str = Field("5", pattern="^(5|15|30|60)$")
    adjustflag: str = Field("3", pattern="^[123]$")
    config: Optional[ExperimentConfig] = Field(
        None,
        description="可选：未显式给 symbols 时从该实验的纳入计划推导",
    )


class RunSummaryResponse(BaseModel):
    model_config = {"extra": "allow"}

    run_id: str
    mode: str
    source_pipeline: Optional[str] = None
    risk_profile: Optional[str] = None
    status: str
    attempt: int = 1
    request_fingerprint: Optional[str] = None
    experiment_fingerprint: Optional[str] = None
    reused_from_run_id: Optional[str] = None
    data_quality: str = "partial"
    realized_pnl: Optional[str] = None
    unrealized_pnl: Optional[str] = None
    final_equity: Optional[str] = None
    cash: Optional[str] = None
    quality_flags: List[str] = Field(default_factory=list)
    progress: float = 0.0
    phase: str = ""
    metrics: Optional[Dict[str, Any]] = None
    counts: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool = False
    result_available: bool = False
    config: Optional[Dict[str, Any]] = None
    research: Optional[Dict[str, Any]] = None
    labels: List[str] = Field(default_factory=list)
    llm_calls: int = 0
    cache_hits: int = 0
    deduplicated: bool = False
    precheck_status: Optional[str] = None
    precheck_warnings: List[str] = Field(default_factory=list)


class RunListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[RunSummaryResponse] = Field(default_factory=list)


class RunDetailResponse(RunSummaryResponse):
    manifest: Optional[Dict[str, Any]] = None
    coverage: List[Dict[str, Any]] = Field(default_factory=list)
    inputs: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class EquityPointResponse(BaseModel):
    session: str
    timestamp: Optional[str] = None
    cash: float
    reserved_cash: float
    available_cash: float
    market_value: float
    equity: float
    peak_equity: float
    drawdown_pct: float
    stale: bool
    benchmark_value: Optional[float] = None
    granularity: Optional[str] = None
    quality: Optional[str] = None


class OrderResponse(BaseModel):
    order_id: str
    plan_key: Optional[str] = None
    symbol: str
    side: str
    order_type: str
    quantity: int
    remaining: int
    filled_quantity: int
    avg_fill_price: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str
    reason: Optional[str] = None
    pre_registered_exit: bool = False
    exit_reason: Optional[str] = None
    submitted_at: Optional[str] = None
    active_from: Optional[str] = None
    expires_on_session: Optional[str] = None


class FillResponse(BaseModel):
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    gross_amount: float
    commission: float
    transfer_fee: float
    stamp_duty: float
    slippage_cost: float
    session: str
    bar_end_at: Optional[str] = None
    quality: Optional[str] = None
    reason: Optional[str] = None


class TradeResponse(BaseModel):
    trade_id: str
    symbol: str
    entry_order_id: str
    exit_order_id: str
    quantity: int
    entry_price: float
    exit_price: float
    cost_basis: float
    net_proceeds: float
    realized_pnl: float
    return_pct: float
    entry_session: str
    exit_session: str
    holding_days: int
    exit_reason: str
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None


class DataJobResponse(BaseModel):
    job_id: str
    job_type: str
    status: str
    request: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    progress: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class RiskPolicyPresetResponse(BaseModel):
    profile: str
    policy_version: str
    max_single_position_pct: float
    max_total_exposure_pct: float
    max_concurrent_positions: int
    min_cash_pct: float
    risk_per_trade_pct: float
    require_stop_loss: bool
    selection_top_n: Optional[int] = None


class FeePolicyResponse(BaseModel):
    version: str
    description: str
    rules: List[Dict[str, Any]] = Field(default_factory=list)
