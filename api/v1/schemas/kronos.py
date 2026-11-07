# -*- coding: utf-8 -*-
"""Kronos API schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class KronosAnalyzeRequest(BaseModel):
    """Request for one on-demand Kronos daily A-share analysis."""

    stock_code: str = Field(..., min_length=1, description="A-share stock code, e.g. 600519 or SH600519")
    frequency: str = Field("1d", pattern="^(1d|daily)$", description="K-line frequency; v1 supports daily only")
    lookback: Optional[int] = Field(None, ge=30, le=2048, description="Daily history bars used as Kronos context")
    pred_len: Optional[int] = Field(None, ge=1, le=60, description="Future daily bars to forecast")
    target_date: Optional[date] = Field(None, description="Optional historical cutoff date for offline evaluation")
    fallback_to_network: bool = Field(True, description="Allow history_loader to fetch missing daily bars from network sources")
    persist: Optional[bool] = Field(None, description="Override KRONOS_CACHE_SIGNALS for this request")


class KronosAnalyzeResponse(BaseModel):
    """Kronos analysis response payload used by chart visualization."""

    status: str
    code: Optional[str] = None
    market: Optional[str] = None
    frequency: Optional[str] = None
    reason: Optional[str] = None
    run_id: Optional[str] = None
    as_of: Optional[str] = None
    bars: List[Dict[str, Any]] = Field(default_factory=list)
    forecast: List[Dict[str, Any]] = Field(default_factory=list)
    signal: Dict[str, Any] = Field(default_factory=dict)
    forecast_summary: Dict[str, Any] = Field(default_factory=dict)
    pattern_instances: List[Dict[str, Any]] = Field(default_factory=list)
    structure_context: Dict[str, Any] = Field(default_factory=dict)
    data_quality: Dict[str, Any] = Field(default_factory=dict)
    source_chain: List[str] = Field(default_factory=list)
    model_profile: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class KronosStatusResponse(BaseModel):
    """Kronos integration status."""

    status: str = "ok"
    enabled: bool = False
    market_scope: List[str] = Field(default_factory=lambda: ["cn"])
    frequency_scope: List[str] = Field(default_factory=lambda: ["1d"])
    repo_path: Optional[str] = None
    repo_available: Optional[bool] = None
    tokenizer_name: Optional[str] = None
    model_name: Optional[str] = None
    device: Optional[str] = None
    max_context: Optional[int] = None
    cache_enabled: Optional[bool] = None
