# -*- coding: utf-8 -*-
"""Dimension-aware evidence selection and quality summaries for single-stock runs."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional


DIMENSION_ORDER = ("technical", "capital", "intel", "fundamental", "market", "other")

_TOOL_DIMENSIONS = {
    "calculate_ma": "technical",
    "analyze_trend": "technical",
    "get_volume_analysis": "technical",
    "analyze_pattern": "technical",
    "analyze_price_structure": "technical",
    "get_daily_history": "technical",
    "get_capital_flow": "capital",
    "get_board_capital_flow": "capital",
    "get_market_capital_flow": "capital",
    "get_northbound_capital_flow": "capital",
    "get_chip_distribution": "capital",
    "search_stock_news": "intel",
    "search_comprehensive_intel": "intel",
    "get_tushare_today_news": "intel",
    "get_tushare_announcements": "intel",
    "score_stock_news_sentiment": "intel",
    # get_stock_info is intentionally not classified: it is deprecated and
    # legacy failures must not lower current fundamental evidence quality.
    "get_stock_business_context": "fundamental",
    "get_tushare_daily_basic": "fundamental",
    "get_tushare_financial_indicators": "fundamental",
    "get_tushare_financial_statements": "fundamental",
    "get_tushare_forecast": "fundamental",
    "get_tushare_express": "fundamental",
    "search_stock_fundamentals": "fundamental",
    "get_realtime_quote": "market",
    "get_market_indices": "market",
    "get_sector_rankings": "market",
    "detect_market_regime": "market",
    "get_symbol_regime_probability": "market",
}

_QUALITY_THRESHOLDS = {
    "technical": 1,
    "capital": 1,
    "intel": 2,
    "fundamental": 2,
}


def infer_tool_dimension(tool_name: Any) -> str:
    """Return the report dimension owned by a tool call."""
    return _TOOL_DIMENSIONS.get(str(tool_name or ""), "other")


_FUNDAMENTAL_NUMBER_FIELDS = frozenset({
    "revenue",
    "total_revenue",
    "total_revenue_ps",
    "revenue_ps",
    "operate_income",
    "n_income",
    "n_income_attr_p",
    "net_profit",
    "netprofit_margin",
    "net_profit_margin",
    "grossprofit_margin",
    "gross_margin",
    "eps",
    "dt_eps",
    "roe",
    "roe_waa",
    "bps",
    "ocfps",
    "pe_ttm",
    "pb",
    "dv_ttm",
    "p_change_min",
    "p_change_max",
    "net_profit_min",
    "net_profit_max",
    "total_assets",
    "total_liab",
    "total_equity",
})
_FUNDAMENTAL_FIELD_SUFFIXES = ("_margin", "_revenue", "_profit", "_eps", "_roe", "_bps")
_ROW_FUNDAMENTAL_TOOLS = frozenset({
    "get_tushare_daily_basic",
    "get_tushare_financial_indicators",
    "get_tushare_financial_statements",
    "get_tushare_forecast",
    "get_tushare_express",
})


def is_unreadable_fundamental_payload(tool_name: Any, payload: Any) -> bool:
    """Return True when a fundamental tool reports success without real values.

    Covers both shapes of pseudo-success: financial fields that are all
    placeholder zeros, and row-based endpoints that answer ``ok`` with an empty
    row list. Non-fundamental tools and payloads with unknown shapes return
    False so their existing handling is untouched.
    """
    if not isinstance(payload, dict):
        return False
    if has_readable_fundamental_numbers(payload) is False:
        return True
    if str(tool_name or "") not in _ROW_FUNDAMENTAL_TOOLS:
        return False
    if str(payload.get("status") or "").strip().lower() != "ok":
        return False
    row_lists = [
        payload.get(key)
        for key in ("items", "rows", "data", "results")
        if isinstance(payload.get(key), list)
    ]
    if not row_lists:
        return False
    return all(not rows for rows in row_lists)


def has_readable_fundamental_numbers(payload: Any) -> Optional[bool]:
    """Report whether a fundamental payload carries at least one real number.

    ``None`` means the payload exposes no financial fields at all (for example a
    pure business-context description), so callers should keep their default
    handling. ``False`` means financial fields exist but every value is a
    placeholder zero/blank: that is a pseudo-success and must be treated as
    missing evidence instead of an available signal.
    """
    rows = list(_fundamental_rows(payload))
    if not rows:
        return None
    saw_field = False
    for row in rows:
        for key, value in row.items():
            field = str(key).lower()
            if field not in _FUNDAMENTAL_NUMBER_FIELDS and not field.endswith(_FUNDAMENTAL_FIELD_SUFFIXES):
                continue
            saw_field = True
            number = _number_or_none(value)
            if number is not None and abs(number) > 0:
                return True
    if not saw_field:
        return None
    return False


def _fundamental_rows(payload: Any, depth: int = 0) -> Iterable[Dict[str, Any]]:
    if depth > 3:
        return
    if isinstance(payload, dict):
        yield payload
        for key in (
            "items",
            "rows",
            "data",
            "results",
            "statements",
            "metrics",
            "signals",
            "income",
            "balance",
            "cashflow",
        ):
            yield from _fundamental_rows(payload.get(key), depth + 1)
    elif isinstance(payload, list):
        for item in payload[:50]:
            yield from _fundamental_rows(item, depth + 1)


def _number_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def select_balanced_tool_calls(
    tool_calls: Iterable[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    """Select bounded evidence without starving dimensions that ran later."""
    calls = [call for call in tool_calls or [] if isinstance(call, dict)]
    if limit <= 0 or not calls:
        return []

    grouped: Dict[str, List[Dict[str, Any]]] = {dimension: [] for dimension in DIMENSION_ORDER}
    duplicates: List[Dict[str, Any]] = []
    seen_tools: set[tuple[str, str]] = set()
    for call in calls:
        dimension = infer_tool_dimension(call.get("tool"))
        key = (dimension, str(call.get("tool") or ""))
        if key in seen_tools:
            duplicates.append(call)
            continue
        seen_tools.add(key)
        grouped[dimension].append(call)

    selected: List[Dict[str, Any]] = []
    while len(selected) < limit:
        added = False
        for dimension in DIMENSION_ORDER:
            bucket = grouped[dimension]
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break

    selected_ids = {id(call) for call in selected}
    for call in calls:
        if len(selected) >= limit:
            break
        if id(call) not in selected_ids:
            selected.append(call)
            selected_ids.add(id(call))
    return selected


def build_dimension_evidence_summary(
    tool_calls: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Build auditable four-dimension coverage from actual tool results."""
    grouped: Dict[str, List[Dict[str, Any]]] = {
        dimension: [] for dimension in ("technical", "capital", "intel", "fundamental")
    }
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        dimension = infer_tool_dimension(call.get("tool"))
        if dimension in grouped:
            grouped[dimension].append(call)

    return {
        dimension: _summarize_dimension(dimension, calls)
        for dimension, calls in grouped.items()
    }


def compact_dimension_evidence_summary(
    summary: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Drop repeated previews before placing coverage in an LLM prompt."""
    keys = (
        "quality",
        "available_tools",
        "partial_tools",
        "failed_tools",
        "missing_evidence",
    )
    return {
        dimension: {key: details.get(key) for key in keys}
        for dimension, details in (summary or {}).items()
        if isinstance(details, dict)
    }


def _summarize_dimension(dimension: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    available: List[str] = []
    partial: List[str] = []
    failed: List[str] = []
    evidence: List[Dict[str, Any]] = []
    for call in calls:
        tool = str(call.get("tool") or "unknown")
        state = _tool_evidence_state(call)
        if state == "available":
            available.append(tool)
        elif state == "partial":
            partial.append(tool)
        else:
            failed.append(tool)
        evidence.append({
            "tool": tool,
            "status": state,
            "success": call.get("success") is True,
            "arguments": call.get("arguments") or {},
            "result_preview": str(call.get("result_preview") or "")[:800],
        })

    usable_count = len(set(available + partial))
    threshold = _QUALITY_THRESHOLDS[dimension]
    if not calls or usable_count == 0:
        quality = "unavailable"
    elif len(set(available)) >= threshold and not failed and not partial:
        quality = "ok"
    else:
        quality = "partial"

    missing: List[str] = []
    if quality == "unavailable":
        missing.append(f"{dimension}_evidence_unavailable")
    elif quality == "partial":
        if failed:
            missing.extend(f"{tool}_failed_or_unavailable" for tool in dict.fromkeys(failed))
        if usable_count < threshold:
            missing.append(f"{dimension}_coverage_incomplete")

    return {
        "dimension": dimension,
        "quality": quality,
        "available_tools": list(dict.fromkeys(available)),
        "partial_tools": list(dict.fromkeys(partial)),
        "failed_tools": list(dict.fromkeys(failed)),
        "missing_evidence": missing,
        "evidence": evidence,
    }


def _tool_evidence_state(call: Dict[str, Any]) -> str:
    if call.get("success") is not True:
        return "failed"
    preview = str(call.get("result_preview") or "").strip()
    if not preview:
        return "partial"
    payload = _parse_preview(preview)
    if payload is None:
        lowered = preview.lower()
        if any(marker in lowered for marker in ('"status": "failed"', '"status":"failed"', '"status": "unavailable"', '"timeout": true')):
            return "failed"
        return "available"
    state = _payload_evidence_state(payload)
    if state == "available":
        dimension = infer_tool_dimension(call.get("tool"))
        if dimension == "intel":
            return _intel_evidence_state(payload)
        if dimension == "fundamental" and is_unreadable_fundamental_payload(call.get("tool"), payload):
            return "failed"
    return state


def _parse_preview(preview: str) -> Optional[Any]:
    try:
        return json.loads(preview)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _payload_evidence_state(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "available"
    status = str(payload.get("status") or "").lower()
    if status in {"failed", "error", "timeout", "unavailable", "tool_failed"}:
        return "failed"
    if payload.get("success") is False or payload.get("timeout") is True:
        return "failed"
    if status == "partial":
        return "partial"
    return "available"


def _intel_evidence_state(payload: Dict[str, Any]) -> str:
    """Treat transport success without current, dated events as partial evidence."""
    items = _collect_news_items(payload)
    if not items:
        return "partial"
    parsed_dates = [parsed for item in items if (parsed := _item_date(item)) is not None]
    if not parsed_dates:
        return "partial"
    latest = max(parsed_dates)
    if (date.today() - latest).days > 30:
        return "partial"
    return "available"


def _collect_news_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for key in ("results", "items", "news", "events", "announcements", "alerts"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    intel = payload.get("intel")
    if isinstance(intel, dict):
        value = intel.get("items")
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def _item_date(item: Dict[str, Any]) -> Optional[date]:
    for key in ("date", "publish_date", "published_at", "datetime", "time", "ann_date"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        normalized = raw[:10].replace("/", "-")
        if len(normalized) == 8 and normalized.isdigit():
            normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
        try:
            return datetime.strptime(normalized, "%Y-%m-%d").date()
        except ValueError:
            continue
    return None
