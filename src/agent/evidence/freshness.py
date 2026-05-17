# -*- coding: utf-8 -*-
"""Freshness and validity-window helpers for evidence cards."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from src.agent.evidence.schemas import Freshness


_DEFAULT_WINDOWS = {
    "quote": ("1-5m", "price_or_volume_update"),
    "technical": ("1d", "new_bar_or_key_level_break"),
    "price_structure": ("1-3d", "structure_break_or_center_change"),
    "capital_flow": ("1d", "capital_flow_update"),
    "chip": ("1-5d", "eod_chip_update"),
    "fundamental": ("7-30d", "filing_or_risk_event_update"),
    "news_event": ("1-3d", "new_event_node"),
    "sentiment": ("30-120m", "heat_or_limit_pool_update"),
    "sector": ("15-60m_or_1d_eod", "sector_ranking_update"),
    "macro": ("1-30d", "macro_policy_or_calendar_update"),
    "regime": ("1d", "volatility_breadth_or_flow_change"),
    "risk": ("until_resolved", "risk_event_update"),
}


def default_window_for_dimension(dimension: str) -> tuple[str, str]:
    """Return default ``(window, refresh_trigger)`` for one dimension."""
    return _DEFAULT_WINDOWS.get(str(dimension or ""), ("unknown", "refresh_when_source_updates"))


def infer_freshness(as_of: Optional[str], dimension: str = "") -> Freshness:
    """Infer coarse freshness from an as-of date/time string."""
    parsed = _parse_date(as_of)
    if parsed is None:
        return "unknown"
    today = datetime.now().date()
    age_days = (today - parsed).days
    if age_days <= 0:
        if dimension in {"quote", "sentiment", "sector"}:
            return "intraday"
        return "eod_current"
    if age_days <= 3:
        return "recent"
    return "stale"


def valid_until_for(as_of: Optional[str], dimension: str = "") -> Optional[str]:
    """Return a conservative valid-until date for a dimension."""
    parsed = _parse_date(as_of)
    if parsed is None:
        return None
    if dimension in {"quote", "sentiment", "sector"}:
        return parsed.isoformat()
    if dimension in {"technical", "capital_flow", "regime"}:
        return (parsed + timedelta(days=1)).isoformat()
    if dimension in {"price_structure", "news_event"}:
        return (parsed + timedelta(days=3)).isoformat()
    if dimension == "chip":
        return (parsed + timedelta(days=5)).isoformat()
    if dimension in {"fundamental", "macro"}:
        return (parsed + timedelta(days=30)).isoformat()
    return parsed.isoformat()


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10] if "-" in text or "/" in text else text[:8], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None
