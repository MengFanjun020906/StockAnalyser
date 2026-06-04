# -*- coding: utf-8 -*-
"""
Search tools — wraps SearchService methods as agent-callable tools.

Tools:
- search_stock_news: search latest stock news
- score_stock_news_sentiment: score company-level news and announcement events
- search_comprehensive_intel: multi-dimensional intelligence search
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.agent.sentiment.news_events import score_news_items
from src.agent.tools.registry import ToolParameter, ToolDefinition
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)


def _run_search_task_with_timeout(
    task: Callable[[], Any],
    timeout_seconds: float,
    task_name: str,
) -> Tuple[Any, Optional[str], int]:
    timeout_value = max(0.0, float(timeout_seconds or 0.0))
    start = time.time()
    if timeout_value <= 0:
        return None, f"{task_name} timeout", 0
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(task)
        try:
            return future.result(timeout=timeout_value), None, int((time.time() - start) * 1000)
        except FuturesTimeoutError:
            future.cancel()
            return None, f"{task_name} timeout", int(timeout_value * 1000)
    except Exception as exc:
        return None, str(exc), int((time.time() - start) * 1000)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db
    return get_db()


def _get_search_service():
    """Return shared SearchService singleton."""
    from src.search_service import get_search_service
    return get_search_service()


def _canonical_search_code(stock_code: str) -> str:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    return canonical_stock_code(normalize_stock_code(str(stock_code or "").strip()))


def _resolve_stock_name(stock_code: str, stock_name: Optional[str] = None) -> str:
    code = str(stock_code or "").strip()
    current = str(stock_name or "").strip()
    if is_meaningful_stock_name(current, code):
        return current
    for name in (STOCK_NAME_MAP.get(code), get_index_stock_name(code)):
        if is_meaningful_stock_name(name, code):
            return str(name)
    return current or code


def _to_tushare_ts_code(stock_code: str) -> str:
    code = str(stock_code or "").strip().upper()
    if "." in code and code.endswith((".SH", ".SZ", ".BJ")):
        return code
    digits = re.sub(r"\D", "", code)
    if not digits:
        return code
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _persist_news_response(
    *,
    stock_code: str,
    stock_name: str,
    dimension: str,
    response,
) -> None:
    """Best-effort news persistence for Agent search tools."""
    if not response or not getattr(response, "success", False) or not getattr(response, "results", None):
        return

    code = _canonical_search_code(stock_code)
    try:
        saved_count = _get_db().save_news_intel(
            code=code,
            name=stock_name,
            dimension=dimension,
            query=response.query,
            response=response,
            query_context=None,
        )
        logger.info(
            "Agent news intel persisted for %s (dimension=%s, new_records=%s)",
            code,
            dimension,
            saved_count,
        )
    except Exception as exc:
        logger.warning(
            "Agent news intel persistence failed for %s (dimension=%s): %s",
            code,
            dimension,
            exc,
        )


def _fetch_tushare_announcements_result(stock_code: str, *, lookback_hours: int) -> Dict[str, Any]:
    """Best-effort Tushare announcement fetch using the HTTP API directly."""
    try:
        from src.config import get_config
        token = str(getattr(get_config(), "tushare_token", "") or "").strip()
    except Exception:
        token = ""
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(hours=max(1, int(lookback_hours or 72)))
    date_window = {
        "start_date": start_dt.strftime("%Y%m%d"),
        "end_date": end_dt.strftime("%Y%m%d"),
    }
    if not token:
        return {
            "status": "disabled",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": "TUSHARE_TOKEN is not configured",
        }
    payload = {
        "api_name": "anns_d",
        "token": token,
        "params": {
            "ts_code": _to_tushare_ts_code(stock_code),
            "start_date": date_window["start_date"],
            "end_date": date_window["end_date"],
        },
        "fields": "ann_date,ts_code,name,title,url",
    }
    try:
        from data_provider.tushare_client import get_tushare_http_url

        response = requests.post(get_tushare_http_url(), json=payload, timeout=5)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.debug("Tushare announcement fetch skipped for %s: %s", stock_code, exc)
        return {
            "status": "error",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": str(exc),
        }

    if body.get("code") not in (0, "0", None):
        logger.debug("Tushare announcement fetch failed for %s: %s", stock_code, body.get("msg"))
        return {
            "status": "error",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": str(body.get("msg") or body.get("code")),
        }
    data = body.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    announcements: List[Dict[str, Any]] = []
    for row in items:
        if not isinstance(row, list):
            continue
        record = dict(zip(fields, row))
        title = str(record.get("title") or "").strip()
        if not title:
            continue
        announcements.append({
            "title": title,
            "snippet": title,
            "url": str(record.get("url") or "").strip(),
            "source": "Tushare.anns_d",
            "published_date": str(record.get("ann_date") or "").strip(),
        })
    return {
        "status": "ok" if announcements else "empty",
        "items": announcements,
        "provider": "Tushare.anns_d",
        "date_window": date_window,
    }


def _fetch_tushare_announcements(stock_code: str, *, lookback_hours: int) -> List[Dict[str, Any]]:
    return list(
        _fetch_tushare_announcements_result(stock_code, lookback_hours=lookback_hours).get("items")
        or []
    )


def _response_to_news_items(response) -> List[Dict[str, Any]]:
    if not response or not getattr(response, "success", False):
        return []
    return [
        {
            "title": getattr(item, "title", ""),
            "snippet": getattr(item, "snippet", ""),
            "url": getattr(item, "url", ""),
            "source": getattr(item, "source", ""),
            "published_date": getattr(item, "published_date", None),
        }
        for item in getattr(response, "results", []) or []
    ]


def _handle_search_stock_news(stock_code: str, stock_name: str) -> dict:
    """Search latest news for a stock."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    response = service.search_stock_news(stock_code, stock_name, max_results=5)

    if not response.success:
        return {
            "query": response.query,
            "success": False,
            "error": response.error_message,
        }

    _persist_news_response(
        stock_code=stock_code,
        stock_name=stock_name,
        dimension="latest_news",
        response=response,
    )

    return {
        "query": response.query,
        "provider": response.provider,
        "success": True,
        "results_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "source": r.source,
                "published_date": r.published_date,
            }
            for r in response.results
        ],
    }


def _handle_score_stock_news_sentiment(
    stock_code: str,
    stock_name: Optional[str] = None,
    lookback_hours: int = 72,
    include_announcements: bool = True,
) -> dict:
    """Score one stock's message/news state from search news and announcements."""
    service = _get_search_service()
    resolved_name = _resolve_stock_name(stock_code, stock_name)
    news_items: List[Dict[str, Any]] = []
    search_payload: Dict[str, Any] = {"success": False, "results_count": 0}

    if getattr(service, "is_available", False):
        focus_keywords = [
            f"{resolved_name} {stock_code} 公告 业绩 订单 合作 减持 监管 问询 回购 增持 最近"
        ]
        try:
            response = service.search_stock_news(
                stock_code,
                resolved_name,
                max_results=8,
                focus_keywords=focus_keywords,
                max_provider_attempts=2,
            )
        except TypeError as exc:
            if "max_provider_attempts" not in str(exc):
                raise
            response = service.search_stock_news(
                stock_code,
                resolved_name,
                max_results=8,
                focus_keywords=focus_keywords,
            )
        if getattr(response, "success", False):
            _persist_news_response(
                stock_code=stock_code,
                stock_name=resolved_name,
                dimension="message_sentiment",
                response=response,
            )
        news_items.extend(_response_to_news_items(response))
        search_payload = {
            "query": getattr(response, "query", ""),
            "provider": getattr(response, "provider", ""),
            "success": bool(getattr(response, "success", False)),
            "results_count": len(getattr(response, "results", []) or []),
            **({"error": getattr(response, "error_message", None)} if getattr(response, "error_message", None) else {}),
        }
    else:
        search_payload = {"success": False, "error": "No search engine available"}

    announcements: List[Dict[str, Any]] = []
    announcements_payload: Dict[str, Any] = {
        "provider": "Tushare.anns_d",
        "enabled": include_announcements,
        "count": 0,
        "status": "disabled",
    }
    if include_announcements:
        announcements_result = _fetch_tushare_announcements_result(stock_code, lookback_hours=lookback_hours)
        announcements = list(announcements_result.get("items") or [])
        announcements_payload = {
            "provider": announcements_result.get("provider") or "Tushare.anns_d",
            "enabled": include_announcements,
            "count": len(announcements),
            "status": announcements_result.get("status") or ("ok" if announcements else "empty"),
            "date_window": announcements_result.get("date_window"),
            **({"error": announcements_result.get("error")} if announcements_result.get("error") else {}),
        }
        news_items.extend(announcements)

    scored = score_news_items(news_items)
    return {
        "status": "ok" if scored["news_count"] else "empty",
        "code": _canonical_search_code(stock_code),
        "name": resolved_name,
        "lookback_hours": lookback_hours,
        "include_announcements": include_announcements,
        "message_score": scored["message_score"],
        "message_state": scored["message_state"],
        "news_count": scored["news_count"],
        "positive_count": scored["positive_count"],
        "negative_count": scored["negative_count"],
        "uncertain_count": scored["uncertain_count"],
        "event_tags": scored["event_tags"],
        "events": scored["events"],
        "risk_flags": scored["risk_flags"],
        "summary": scored["summary"],
        "sources": {
            "search": search_payload,
            "announcements": announcements_payload,
        },
    }


search_stock_news_tool = ToolDefinition(
    name="search_stock_news",
    description="Search for the latest news articles about a specific stock. "
                "Requires both stock_code and stock_name for accurate search. "
                "Returns news titles, snippets, sources, and URLs.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_stock_news,
    category="search",
)


score_stock_news_sentiment_tool = ToolDefinition(
    name="score_stock_news_sentiment",
    description=(
        "Score one stock's message/news sentiment from recent news and optional Tushare announcements. "
        "Classifies earnings, orders/contracts, policy tailwinds, buybacks/increases, reductions, "
        "regulatory risks, litigation/defaults, and rumor/abnormal-trading uncertainty."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g. '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Optional stock name in Chinese. If omitted, the local stock-name index is used.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="lookback_hours",
            type="integer",
            description="Lookback window for announcements/news in hours (default: 72).",
            required=False,
            default=72,
        ),
        ToolParameter(
            name="include_announcements",
            type="boolean",
            description="Whether to include Tushare announcement hard events when TUSHARE_TOKEN is configured.",
            required=False,
            default=True,
        ),
    ],
    handler=_handle_score_stock_news_sentiment,
    category="search",
)


# ============================================================
# search_comprehensive_intel
# ============================================================

def _preprocess_intel_with_llm(
    raw_items: List[Dict[str, Any]],
    stock_name: str,
    timeout_seconds: float = 4.0,
) -> Dict[str, Any]:
    """Lightweight LLM pass to produce a fixed-schema intel summary."""
    import json as _json

    if not raw_items:
        return {"items": [], "key_signals": [], "overall_sentiment": "unknown"}

    compact = [
        {
            "dim": item.get("dim", ""),
            "title": (item.get("title") or "")[:120],
            "date": item.get("date") or "",
            "source": item.get("source") or "",
        }
        for item in raw_items
    ]

    prompt = (
        f'你是股票新闻解析助手。对以下关于"{stock_name}"的新闻标题列表，'
        "输出JSON（无其他内容）：\n"
        '{"items":[{"title":"原标题","date":"日期","source":"来源","dim":"维度",'
        '"signal_type":"capital_flow|earnings|risk|announcement|policy|general",'
        '"sentiment":"positive|negative|neutral","one_line":"一句话要点"}],'
        '"key_signals":["最关键信号"],"overall_sentiment":"positive|negative|neutral|mixed"}\n\n'
        f"新闻列表：\n{_json.dumps(compact, ensure_ascii=False)}"
    )

    try:
        from src.agent.llm_adapter import LLMToolAdapter

        resp = LLMToolAdapter().call_text(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=500,
            timeout=max(1.0, float(timeout_seconds or 4.0)),
        )
        text = (resp.content or "").strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
        return _json.loads(text)
    except Exception as exc:
        logger.warning("Intel LLM preprocessing failed: %s", exc)

    return {
        "items": compact,
        "key_signals": [],
        "overall_sentiment": "unknown",
    }


def _handle_search_comprehensive_intel(stock_code: str, stock_name: str) -> dict:
    """Multi-dimensional intelligence search with LLM-preprocessed structured output."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    intel_results, search_err, search_ms = _run_search_task_with_timeout(
        lambda: service.search_comprehensive_intel(
            stock_code=stock_code,
            stock_name=stock_name,
            max_searches=2,
        ),
        24.0,
        "search_comprehensive_intel",
    )
    if search_err or not isinstance(intel_results, dict):
        status = "timeout" if search_err and "timeout" in str(search_err).lower() else "error"
        return {
            "status": status,
            "stock_code": _canonical_search_code(stock_code),
            "stock_name": stock_name,
            "intel": {"items": [], "key_signals": [], "overall_sentiment": "unknown"},
            "dimensions_searched": [],
            "dimensions_empty": [],
            "source_chain": [{
                "provider": "search_comprehensive_intel",
                "result": status,
                "duration_ms": search_ms,
            }],
            "errors": [str(search_err or "invalid search result")],
        }

    if not intel_results:
        return {"error": "Comprehensive intel search returned no results"}

    # Collect raw items across all dimensions for LLM preprocessing
    raw_items: List[Dict[str, Any]] = []
    for dim_name, response in intel_results.items():
        if response and response.success:
            _persist_news_response(
                stock_code=stock_code,
                stock_name=stock_name,
                dimension=dim_name,
                response=response,
            )
            for r in response.results[:4]:
                raw_items.append({
                    "dim": dim_name,
                    "title": r.title,
                    "date": r.published_date or "",
                    "source": r.source,
                    "snippet": (r.snippet or "")[:80],
                })

    # Lightweight LLM pass → fixed schema; fallback to raw compact list on failure
    intel = _preprocess_intel_with_llm(raw_items, stock_name, timeout_seconds=4.0)

    return {
        "status": "ok" if raw_items else "empty",
        "stock_code": _canonical_search_code(stock_code),
        "stock_name": stock_name,
        "intel": intel,
        "dimensions_searched": [
            dim for dim, resp in intel_results.items() if resp and resp.success
        ],
        "dimensions_empty": [
            dim for dim, resp in intel_results.items() if not (resp and resp.success)
        ],
        "source_chain": [{
            "provider": "search_comprehensive_intel",
            "result": "ok",
            "duration_ms": search_ms,
            "max_searches": 2,
        }],
    }


search_comprehensive_intel_tool = ToolDefinition(
    name="search_comprehensive_intel",
    description="Multi-dimensional intelligence search: latest news, market analysis, "
                "risk checking, earnings outlook, and industry trends for a stock. "
                "Returns a formatted report and structured results.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_comprehensive_intel,
    category="search",
)


ALL_SEARCH_TOOLS = [
    search_stock_news_tool,
    score_stock_news_sentiment_tool,
    search_comprehensive_intel_tool,
]
