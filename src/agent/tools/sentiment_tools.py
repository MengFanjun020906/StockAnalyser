# -*- coding: utf-8 -*-
"""Market sentiment and global-risk Agent tools.

These tools are deliberately deterministic.  They summarize existing market
microstructure/search sources for the Agent, without adding another LLM call.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.agent.tools.registry import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


_CRITICAL_RISK_TERMS = (
    "missile",
    "attack",
    "invasion",
    "war",
    "strike",
    "sanction",
    "制裁",
    "战争",
    "袭击",
    "导弹",
    "冲突升级",
)
_HIGH_RISK_TERMS = (
    "shipping disruption",
    "supply disruption",
    "oil spike",
    "energy crisis",
    "tariff",
    "export control",
    "航运中断",
    "供应中断",
    "油价",
    "关税",
    "出口管制",
)
_MEDIUM_RISK_TERMS = (
    "risk",
    "geopolitical",
    "tension",
    "rate hike",
    "inflation",
    "风险",
    "地缘",
    "紧张",
    "加息",
    "通胀",
)


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    if not math.isfinite(value):
        return lower
    return max(lower, min(upper, value))


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("items", "candidates", "data", "rows", "stocks", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _source_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "failed"
    return str(payload.get("status") or ("ok" if _extract_items(payload) else "empty")).lower()


def _source_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "invalid_payload"
    if isinstance(payload.get("errors"), list):
        return "; ".join(str(item) for item in payload.get("errors") if item)
    return str(payload.get("error") or payload.get("message") or "")


def _stock_code(item: Dict[str, Any]) -> str:
    raw = str(item.get("code") or item.get("stock_code") or item.get("symbol") or item.get("ts_code") or "").strip()
    digits = "".join(char for char in raw if char.isdigit())
    if 5 <= len(digits) <= 6:
        return digits.zfill(6)
    return raw


def _stock_name(item: Dict[str, Any], code: str) -> str:
    return str(item.get("name") or item.get("stock_name") or item.get("security_name") or code).strip() or code


def _fetch_limit_up_pool(limit: int = 30) -> Dict[str, Any]:
    from src.agent.tools.data_tools import _handle_get_stockapi_limit_up_pool

    return _handle_get_stockapi_limit_up_pool(limit=limit)


def _fetch_popularity_rank(limit: int = 30) -> Dict[str, Any]:
    from src.agent.tools.data_tools import _handle_get_stockapi_popularity_rank

    return _handle_get_stockapi_popularity_rank(limit=limit)


def _fetch_market_indices(region: str = "cn") -> Dict[str, Any]:
    from src.agent.tools.market_tools import _handle_get_market_indices

    return _handle_get_market_indices(region=region)


def _build_search_service():
    try:
        from src.config import get_config
        from src.search_service import SearchService

        config = get_config()
        return SearchService(
            anysearch_api_key=getattr(config, "anysearch_api_key", None),
            searxng_public_instances_enabled=False,
            news_max_age_days=getattr(config, "news_max_age_days", 3),
            news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
        )
    except Exception as exc:
        logger.warning("Market sentiment search service unavailable: %s", exc)
        return None


def _index_change_values(payload: Dict[str, Any]) -> List[float]:
    indices = payload.get("indices") if isinstance(payload.get("indices"), list) else []
    values: List[float] = []
    for item in indices:
        if not isinstance(item, dict):
            continue
        value = _safe_float(
            item.get("change_pct")
            or item.get("pct_chg")
            or item.get("change_percent")
            or item.get("change")
        )
        if value is not None:
            values.append(value)
    return values


def _risk_appetite(score: float) -> str:
    if score >= 68:
        return "risk_on"
    if score >= 45:
        return "neutral"
    if score >= 25:
        return "risk_off"
    return "panic"


def _merge_candidate(
    candidates_by_code: Dict[str, Dict[str, Any]],
    *,
    code: str,
    name: str,
    source: str,
    score: float,
    reason: str,
    detail: str,
    metrics: Dict[str, Any],
) -> None:
    if not code:
        return
    existing = candidates_by_code.get(code)
    if existing is None:
        candidates_by_code[code] = {
            "code": code,
            "name": name,
            "source": source,
            "candidate_source": "sentiment_heat",
            "signal_score": round(_clamp(score), 2),
            "reason": reason,
            "recall_sources": [source],
            "reason_dimensions": [
                {"dimension": "sentiment", "label": "情绪/热点", "detail": detail}
            ],
            "metrics": metrics,
            "data_quality": {"source_chain": []},
        }
        return

    existing["signal_score"] = round(max(float(existing.get("signal_score") or 0.0), _clamp(score)), 2)
    recall_sources = list(existing.get("recall_sources") or [])
    if source not in recall_sources:
        recall_sources.append(source)
    existing["recall_sources"] = recall_sources
    existing["source"] = "+".join(recall_sources)
    if reason and reason not in str(existing.get("reason") or ""):
        existing["reason"] = f"{existing.get('reason')}; {reason}"
    dimensions = existing.get("reason_dimensions")
    if isinstance(dimensions, list):
        dimensions.append({"dimension": "sentiment", "label": "情绪/热点", "detail": detail})
    existing_metrics = existing.get("metrics")
    if isinstance(existing_metrics, dict):
        existing_metrics.update(metrics)


def _handle_get_sentiment_heat_candidates(market: str = "cn", limit: int = 10) -> Dict[str, Any]:
    """Return deterministic sentiment/attention heat candidates."""
    if str(market or "cn").lower() != "cn":
        return {
            "status": "not_supported",
            "market": market,
            "candidate_source": "sentiment_heat",
            "candidates": [],
            "error": "sentiment heat candidates currently support cn A-shares only",
        }

    effective_limit = max(1, min(int(limit or 10), 30))
    popularity = _fetch_popularity_rank(limit=max(effective_limit, 10))
    limit_pool = _fetch_limit_up_pool(limit=max(effective_limit, 10))
    source_chain = [
        {
            "provider": "stockapi:popularity_rank",
            "status": _source_status(popularity),
            "count": len(_extract_items(popularity)),
            "error": _source_error(popularity),
        },
        {
            "provider": "stockapi:limit_up_pool",
            "status": _source_status(limit_pool),
            "count": len(_extract_items(limit_pool)),
            "error": _source_error(limit_pool),
        },
    ]

    candidates_by_code: Dict[str, Dict[str, Any]] = {}
    for rank_index, item in enumerate(_extract_items(popularity), start=1):
        code = _stock_code(item)
        rank = _safe_float(item.get("rank") or rank_index) or float(rank_index)
        score = 82.0 - min(32.0, max(0.0, rank - 1.0) * 1.6)
        reason = str(item.get("reason") or item.get("ai_reason") or f"人气榜 rank={int(rank)}").strip()
        _merge_candidate(
            candidates_by_code,
            code=code,
            name=_stock_name(item, code),
            source="sentiment_heat:hot_rank",
            score=score,
            reason=reason,
            detail=f"市场关注度排名 {int(rank)}",
            metrics={"hot_rank": rank, "hot_reason": reason},
        )

    for item in _extract_items(limit_pool):
        code = _stock_code(item)
        streak = _safe_float(
            item.get("limit_up_streak") or item.get("streak") or item.get("height") or 1
        ) or 1.0
        reason = str(item.get("stock_reason") or item.get("reason") or item.get("plate_reason") or "涨停池热度").strip()
        _merge_candidate(
            candidates_by_code,
            code=code,
            name=_stock_name(item, code),
            source="sentiment_heat:limit_up",
            score=76.0 + min(16.0, max(0.0, streak - 1.0) * 4.0),
            reason=reason,
            detail=f"涨停池命中，连板/高度={streak:g}",
            metrics={"limit_up_streak": streak, "limit_reason": reason},
        )

    candidates = sorted(
        candidates_by_code.values(),
        key=lambda item: (-float(item.get("signal_score") or 0.0), str(item.get("code") or "")),
    )[:effective_limit]
    for item in candidates:
        dq = item.get("data_quality")
        if isinstance(dq, dict):
            dq["source_chain"] = source_chain

    any_ok = any(item["status"] in {"ok", "partial"} for item in source_chain)
    status = "ok" if candidates else ("empty" if any_ok else "failed")
    return {
        "status": status,
        "market": market,
        "candidate_source": "sentiment_heat",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "source_chain": source_chain,
        "data_quality": "sufficient" if candidates else ("limited" if any_ok else "failed"),
        "note": "情绪热度候选只代表关注度/短线情绪上升，必须继续核验价格位置、流动性、资金和消息真因。",
    }


def _handle_scan_global_risk_events(region: str = "global", lookback_hours: int = 24, limit: int = 20) -> Dict[str, Any]:
    """Scan global macro/geopolitical risk headlines through SearchService."""
    effective_limit = max(1, min(int(limit or 20), 50))
    hours = max(1, min(int(lookback_hours or 24), 168))
    days = max(1, math.ceil(hours / 24))
    query = (
        "geopolitical risk war sanction missile shipping disruption oil supply market"
        if str(region or "global").lower() != "cn"
        else "地缘风险 战争 制裁 航运中断 油价 供应链 市场"
    )
    service = _build_search_service()
    if service is None:
        return {
            "status": "failed",
            "region": region,
            "events": [],
            "highest_severity": "unknown",
            "source_chain": [{"provider": "SearchService", "status": "failed", "error": "unavailable"}],
            "data_quality": "failed",
        }

    try:
        response = service.search_general_news(query, max_results=effective_limit, days=days)
    except Exception as exc:
        return {
            "status": "failed",
            "region": region,
            "events": [],
            "highest_severity": "unknown",
            "source_chain": [{"provider": "SearchService", "status": "failed", "error": f"{type(exc).__name__}: {exc}"}],
            "data_quality": "failed",
        }

    events: List[Dict[str, Any]] = []
    for result in getattr(response, "results", []) or []:
        title = str(getattr(result, "title", "") or "")
        snippet = str(getattr(result, "snippet", "") or "")
        text = f"{title} {snippet}".lower()
        severity = _risk_severity(text)
        if severity == "low":
            continue
        events.append(
            {
                "title": title,
                "snippet": snippet[:280],
                "url": str(getattr(result, "url", "") or ""),
                "source": str(getattr(result, "source", "") or ""),
                "published_date": getattr(result, "published_date", None),
                "severity": severity,
                "event_type": _risk_event_type(text),
                "affected_sectors": _affected_sectors(text),
            }
        )
    severity_rank = {"unknown": -1, "low": 0, "medium": 1, "high": 2, "critical": 3}
    highest = "unknown"
    for item in events:
        if severity_rank.get(str(item.get("severity")), -1) > severity_rank.get(highest, -1):
            highest = str(item.get("severity"))

    success = bool(getattr(response, "success", False))
    source_chain = [
        {
            "provider": str(getattr(response, "provider", "SearchService") or "SearchService"),
            "status": "ok" if success else "failed",
            "count": len(getattr(response, "results", []) or []),
            "error": str(getattr(response, "error_message", "") or ""),
        }
    ]
    return {
        "status": "ok" if events else ("empty" if success else "failed"),
        "region": region,
        "lookback_hours": hours,
        "query": query,
        "events": events[:effective_limit],
        "highest_severity": highest,
        "risk_level": highest if highest in {"medium", "high", "critical"} else "low",
        "source_chain": source_chain,
        "data_quality": "sufficient" if events else ("limited" if success else "failed"),
        "note": "全球风险扫描用于 risk-off 约束，不直接推出个股候选。",
    }


def _risk_severity(text: str) -> str:
    if any(term in text for term in _CRITICAL_RISK_TERMS):
        return "critical"
    if any(term in text for term in _HIGH_RISK_TERMS):
        return "high"
    if any(term in text for term in _MEDIUM_RISK_TERMS):
        return "medium"
    return "low"


def _risk_event_type(text: str) -> str:
    if any(term in text for term in ("oil", "energy", "shipping", "航运", "油价", "能源")):
        return "energy_transport"
    if any(term in text for term in ("war", "missile", "sanction", "geopolitical", "战争", "制裁", "导弹", "地缘")):
        return "geopolitical"
    return "macro_risk"


def _affected_sectors(text: str) -> List[str]:
    sectors: List[str] = []
    if any(term in text for term in ("oil", "energy", "油价", "能源")):
        sectors.extend(["能源", "化工", "航运"])
    if any(term in text for term in ("shipping", "route", "航运", "红海")):
        sectors.extend(["航运", "出口链"])
    if any(term in text for term in ("sanction", "export control", "制裁", "出口管制")):
        sectors.extend(["半导体", "国产替代", "军工"])
    return list(dict.fromkeys(sectors))


def _handle_get_market_sentiment_snapshot(
    region: str = "cn",
    include_global_risk: bool = True,
    limit: int = 30,
) -> Dict[str, Any]:
    """Build a compact market sentiment snapshot from existing sources."""
    effective_limit = max(5, min(int(limit or 30), 100))
    limit_pool = _fetch_limit_up_pool(limit=effective_limit)
    popularity = _fetch_popularity_rank(limit=effective_limit)
    indices = _fetch_market_indices(region=region or "cn")
    risk = (
        _handle_scan_global_risk_events(region="global", lookback_hours=24, limit=10)
        if include_global_risk
        else {"status": "skipped", "highest_severity": "unknown", "risk_level": "unknown"}
    )

    limit_items = _extract_items(limit_pool)
    popularity_items = _extract_items(popularity)
    index_changes = _index_change_values(indices if isinstance(indices, dict) else {})
    avg_index_change = sum(index_changes) / len(index_changes) if index_changes else 0.0
    limit_up_count = len(limit_items)
    heat_count = len(popularity_items)
    global_risk_penalty = {"critical": 28, "high": 18, "medium": 8}.get(
        str(risk.get("highest_severity") or risk.get("risk_level") or "").lower(),
        0,
    )
    sentiment_score = _clamp(
        50.0
        + avg_index_change * 10.0
        + min(limit_up_count, 80) * 0.28
        + min(heat_count, 50) * 0.12
        - global_risk_penalty
    )
    source_chain = [
        {
            "provider": "market_indices",
            "status": _source_status(indices),
            "count": len(index_changes),
            "error": _source_error(indices),
        },
        {
            "provider": "stockapi:limit_up_pool",
            "status": _source_status(limit_pool),
            "count": len(limit_items),
            "error": _source_error(limit_pool),
        },
        {
            "provider": "stockapi:popularity_rank",
            "status": _source_status(popularity),
            "count": len(popularity_items),
            "error": _source_error(popularity),
        },
        {
            "provider": "global_risk_scan",
            "status": str(risk.get("status") or "unknown"),
            "count": len(risk.get("events") or []) if isinstance(risk.get("events"), list) else 0,
            "error": str(risk.get("error") or ""),
        },
    ]
    ok_sources = [item for item in source_chain if item.get("status") in {"ok", "partial", "empty"}]
    status = "ok" if len(ok_sources) >= 2 else "limited"
    return {
        "status": status,
        "region": region,
        "sentiment_score": round(sentiment_score, 2),
        "risk_appetite": _risk_appetite(sentiment_score),
        "components": {
            "avg_index_change_pct": round(avg_index_change, 4),
            "limit_up_count": limit_up_count,
            "popularity_sample_count": heat_count,
            "global_risk_level": risk.get("highest_severity") or risk.get("risk_level"),
            "global_risk_penalty": global_risk_penalty,
        },
        "action_constraints": _action_constraints(_risk_appetite(sentiment_score), risk),
        "source_chain": source_chain,
        "data_quality": "sufficient" if status == "ok" else "limited",
        "note": "市场情绪快照只约束动作强度；不能替代个股技术、资金和消息核验。",
    }


def _action_constraints(risk_appetite: str, risk: Dict[str, Any]) -> List[str]:
    constraints: List[str] = []
    if risk_appetite in {"risk_off", "panic"}:
        constraints.append("降低主动开仓强度，优先等待确认或只保留强触发条件入场。")
    if risk_appetite == "panic":
        constraints.append("除非用户明确要求逆向策略，否则不新增追涨仓位。")
    if str(risk.get("highest_severity") or "").lower() in {"high", "critical"}:
        constraints.append("存在高等级全球风险，主题催化必须额外核验是否已价格反应。")
    return constraints


get_market_sentiment_snapshot_tool = ToolDefinition(
    name="get_market_sentiment_snapshot",
    description=(
        "Build a deterministic market sentiment snapshot from index moves, limit-up pool, "
        "popularity heat and optional global risk scan. Use it to constrain action strength."
    ),
    parameters=[
        ToolParameter(name="region", type="string", description="Market region, default cn.", required=False, default="cn", enum=["cn", "hk", "us", "global"]),
        ToolParameter(name="include_global_risk", type="boolean", description="Whether to include global risk scan.", required=False, default=True),
        ToolParameter(name="limit", type="integer", description="Rows to sample from heat sources.", required=False, default=30),
    ],
    handler=_handle_get_market_sentiment_snapshot,
    category="market",
)


get_sentiment_heat_candidates_tool = ToolDefinition(
    name="get_sentiment_heat_candidates",
    description=(
        "Discover A-share candidates from deterministic sentiment/attention heat sources "
        "such as popularity rank and limit-up pool. This is a candidate source only."
    ),
    parameters=[
        ToolParameter(name="market", type="string", description="Market, currently cn only.", required=False, default="cn", enum=["cn"]),
        ToolParameter(name="limit", type="integer", description="Maximum candidates to return.", required=False, default=10),
    ],
    handler=_handle_get_sentiment_heat_candidates,
    category="market",
)


scan_global_risk_events_tool = ToolDefinition(
    name="scan_global_risk_events",
    description=(
        "Scan global macro/geopolitical risk headlines and return severity, affected sectors "
        "and source diagnostics. Use for risk-off checks, not direct stock picking."
    ),
    parameters=[
        ToolParameter(name="region", type="string", description="global or cn query language.", required=False, default="global", enum=["global", "cn"]),
        ToolParameter(name="lookback_hours", type="integer", description="Lookback window in hours.", required=False, default=24),
        ToolParameter(name="limit", type="integer", description="Maximum headlines to inspect.", required=False, default=20),
    ],
    handler=_handle_scan_global_risk_events,
    category="market",
)


ALL_SENTIMENT_TOOLS = [
    get_market_sentiment_snapshot_tool,
    get_sentiment_heat_candidates_tool,
    scan_global_risk_events_tool,
]
