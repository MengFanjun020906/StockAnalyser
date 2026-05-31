# -*- coding: utf-8 -*-
"""
Data tools — wraps DataFetcherManager methods as agent-callable tools.

Tools:
- get_realtime_quote: real-time stock quote
- get_daily_history: historical OHLCV data
- get_chip_distribution: chip distribution analysis
- get_analysis_context: historical analysis context from DB
"""

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)

_fetcher_manager_singleton = None
_fetcher_manager_lock = Lock()
_tushare_trade_date_cache: Dict[Tuple[int, int, int, int, str, bool], List[str]] = {}
_tushare_trade_date_cache_lock = Lock()
_DAILY_HISTORY_DEFAULT_DAYS = 60
_DAILY_HISTORY_MAX_DAYS = 365
_TUSHARE_NEWS_SOURCES = {
    "sina",
    "wallstreetcn",
    "10jqka",
    "eastmoney",
    "yuncaijing",
    "fenghuang",
    "jinrongjie",
    "cls",
    "yicai",
}


def _run_manager_task_with_timeout(
    manager: Any,
    task: Callable[[], Any],
    timeout_seconds: float,
    task_name: str,
) -> Tuple[Any, Optional[str], int]:
    """Run a manager-backed task with its bounded timeout helper when available."""
    timeout_value = max(0.0, float(timeout_seconds or 0.0))
    if hasattr(manager, "_run_with_timeout"):
        return manager._run_with_timeout(task, timeout_value, task_name)

    start = time.time()
    try:
        return task(), None, int((time.time() - start) * 1000)
    except Exception as exc:
        return None, str(exc), int((time.time() - start) * 1000)


def _get_agent_timeout_attr(attr_name: str, default: float) -> float:
    try:
        from src.config import get_config

        return float(getattr(get_config(), attr_name, default))
    except Exception:
        return float(default)


def _get_fetcher_manager():
    """Return a module-level singleton DataFetcherManager.

    Re-creating the manager on every tool call causes Tushare re-init overhead
    (~2 s each) and prevents circuit-breaker cooldown from taking effect across
    consecutive tool calls within the same agent run.
    """
    from data_provider import DataFetcherManager
    global _fetcher_manager_singleton
    if _fetcher_manager_singleton is None:
        with _fetcher_manager_lock:
            if _fetcher_manager_singleton is None:
                _fetcher_manager_singleton = DataFetcherManager()
    return _fetcher_manager_singleton


def reset_fetcher_manager() -> None:
    """Clear the cached DataFetcherManager so runtime config reloads take effect."""
    global _fetcher_manager_singleton
    with _fetcher_manager_lock:
        _fetcher_manager_singleton = None


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db
    return get_db()


def _normalize_history_days(days: Any) -> Tuple[int, Dict[str, Any]]:
    """Normalize LLM-provided history window and return response metadata."""
    requested_days = days
    warning = None
    try:
        if isinstance(days, bool):
            raise ValueError("bool is not a valid days value")
        effective_days = int(days)
    except (TypeError, ValueError):
        effective_days = _DAILY_HISTORY_DEFAULT_DAYS
        warning = (
            f"Invalid days value {requested_days!r}; "
            f"using default {_DAILY_HISTORY_DEFAULT_DAYS}."
        )

    if effective_days < 1:
        effective_days = 1
        warning = f"days must be >= 1; using {effective_days}."
    elif effective_days > _DAILY_HISTORY_MAX_DAYS:
        effective_days = _DAILY_HISTORY_MAX_DAYS
        warning = f"days exceeds max {_DAILY_HISTORY_MAX_DAYS}; truncated."

    metadata: Dict[str, Any] = {}
    if warning is not None:
        metadata.update(
            {
                "warning": warning,
                "requested_days": requested_days,
                "effective_days": effective_days,
            }
        )
    return effective_days, metadata


def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    """Return cache lookup candidates plus canonical write code."""
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _append_history_metadata(response: dict, metadata: Dict[str, Any]) -> dict:
    if metadata:
        response.update(metadata)
    return response


def _compact_fundamental_context(fundamental_context: dict) -> dict:
    """Reduce token footprint for tool responses while keeping key semantics."""
    if not isinstance(fundamental_context, dict):
        return {}
    blocks = (
        "valuation",
        "growth",
        "earnings",
        "institution",
        "capital_flow",
        "dragon_tiger",
        "boards",
    )
    compact = {
        "market": fundamental_context.get("market"),
        "status": fundamental_context.get("status"),
        "coverage": fundamental_context.get("coverage", {}),
    }
    for block in blocks:
        payload = fundamental_context.get(block, {})
        if isinstance(payload, dict):
            compact[block] = {
                "status": payload.get("status"),
                "data": payload.get("data", {}),
            }
        else:
            compact[block] = {"status": "failed", "data": {}}
    return compact


def _compact_portfolio_snapshot(snapshot: dict, include_positions: bool = False, top_n: int = 5) -> dict:
    """Shrink portfolio snapshot payload for default tool responses."""
    if not isinstance(snapshot, dict):
        return {}
    compact_accounts = []
    for account in snapshot.get("accounts", []) or []:
        if not isinstance(account, dict):
            continue
        positions = list(account.get("positions") or [])
        positions = sorted(
            positions,
            key=lambda item: float((item or {}).get("market_value_base") or 0.0),
            reverse=True,
        )
        account_payload = {
            "account_id": account.get("account_id"),
            "account_name": account.get("account_name"),
            "market": account.get("market"),
            "base_currency": account.get("base_currency"),
            "total_equity": account.get("total_equity"),
            "total_market_value": account.get("total_market_value"),
            "total_cash": account.get("total_cash"),
            "realized_pnl": account.get("realized_pnl"),
            "unrealized_pnl": account.get("unrealized_pnl"),
            "fx_stale": account.get("fx_stale"),
        }
        if include_positions:
            account_payload["positions"] = positions
        else:
            account_payload["position_count"] = len(positions)
            account_payload["top_positions"] = positions[:top_n]
        compact_accounts.append(account_payload)

    return {
        "as_of": snapshot.get("as_of"),
        "cost_method": snapshot.get("cost_method"),
        "currency": snapshot.get("currency"),
        "account_count": snapshot.get("account_count"),
        "total_cash": snapshot.get("total_cash"),
        "total_market_value": snapshot.get("total_market_value"),
        "total_equity": snapshot.get("total_equity"),
        "realized_pnl": snapshot.get("realized_pnl"),
        "unrealized_pnl": snapshot.get("unrealized_pnl"),
        "fx_stale": snapshot.get("fx_stale"),
        "accounts": compact_accounts,
    }


def _compact_portfolio_risk(risk: dict, top_n: int = 10) -> dict:
    """Shrink portfolio risk payload for tool responses."""
    if not isinstance(risk, dict):
        return {}
    concentration = risk.get("concentration", {}) or {}
    top_positions = list(concentration.get("top_positions") or [])
    top_positions = sorted(
        top_positions,
        key=lambda item: float((item or {}).get("weight_pct") or 0.0),
        reverse=True,
    )[:top_n]
    stop_loss = risk.get("stop_loss", {}) or {}
    stop_items = list(stop_loss.get("items") or [])
    stop_items = sorted(
        stop_items,
        key=lambda item: float((item or {}).get("loss_pct") or 0.0),
        reverse=True,
    )[:top_n]
    drawdown = risk.get("drawdown", {}) or {}
    return {
        "as_of": risk.get("as_of"),
        "currency": risk.get("currency"),
        "cost_method": risk.get("cost_method"),
        "thresholds": risk.get("thresholds", {}),
        "concentration": {
            "alert": concentration.get("alert", False),
            "top_weight_pct": concentration.get("top_weight_pct"),
            "top_positions": top_positions,
        },
        "drawdown": {
            "alert": drawdown.get("alert", False),
            "max_drawdown_pct": drawdown.get("max_drawdown_pct"),
            "current_drawdown_pct": drawdown.get("current_drawdown_pct"),
            "fx_stale": drawdown.get("fx_stale", False),
        },
        "stop_loss": {
            "near_alert": stop_loss.get("near_alert", False),
            "triggered_count": stop_loss.get("triggered_count", 0),
            "near_count": stop_loss.get("near_count", 0),
            "items": stop_items,
        },
    }


# ============================================================
# get_realtime_quote
# ============================================================

def _to_iso_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_market_datetime(value: Any, tz_name: Optional[str]) -> datetime:
    """Convert exchange-calendar timestamps to a market-local datetime."""
    if hasattr(value, "tz_convert"):
        converted = value.tz_convert(tz_name) if tz_name else value
        return converted.to_pydatetime()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _resolve_realtime_market_phase(market: Optional[str], market_now: datetime) -> str:
    """Best-effort intraday phase for quote wording."""
    if not market:
        return "unknown"

    try:
        from src.core import trading_calendar

        if not getattr(trading_calendar, "_XCALS_AVAILABLE", False):
            return "unknown"

        exchange = trading_calendar.MARKET_EXCHANGE.get(market)
        tz_name = trading_calendar.MARKET_TIMEZONE.get(market)
        if not exchange or not tz_name:
            return "unknown"

        cal = trading_calendar.xcals.get_calendar(exchange)
        local_date = market_now.date()
        if not cal.is_session(local_date):
            return "closed_non_trading_day"

        session = cal.date_to_session(local_date, direction="previous")
        session_open = _to_market_datetime(cal.session_open(session), tz_name)
        session_close = _to_market_datetime(cal.session_close(session), tz_name)

        if market_now < session_open:
            return "pre_open"
        if market_now > session_close:
            return "post_close"
        return "open"
    except Exception as exc:
        logger.debug("Unable to resolve realtime market phase for %s: %s", market, exc)
        return "unknown"


def _market_detection_codes(stock_code: str, quote: Any = None) -> List[str]:
    candidates: List[str] = []
    for raw in (stock_code, getattr(quote, "code", None)):
        if raw is None:
            continue
        code = str(raw).strip()
        if code and code not in candidates:
            candidates.append(code)

    try:
        from data_provider.base import canonical_stock_code, normalize_stock_code

        for raw in list(candidates):
            for derived in (
                canonical_stock_code(raw),
                canonical_stock_code(normalize_stock_code(raw)),
            ):
                if derived and derived not in candidates:
                    candidates.append(derived)
    except Exception as exc:
        logger.debug("Unable to build normalized market detection codes: %s", exc)

    return candidates


def _infer_market_for_quote(stock_code: str, quote: Any = None) -> Optional[str]:
    from src.core import trading_calendar

    for candidate in _market_detection_codes(stock_code, quote):
        market = trading_calendar.get_market_for_stock(candidate)
        if market:
            return market
    return None


def _build_realtime_quote_session_metadata(stock_code: str, quote: Any = None) -> Dict[str, Any]:
    """Return market/session metadata so reports can label quote freshness."""
    try:
        from src.core import trading_calendar

        market = _infer_market_for_quote(stock_code, quote)
        market_now = trading_calendar.get_market_now(market)
        query_date = market_now.date()
        is_trading_day = (
            trading_calendar.is_market_open(market, query_date)
            if market
            else None
        )
        effective_trading_date = trading_calendar.get_effective_trading_date(
            market, current_time=market_now
        )
        market_phase = _resolve_realtime_market_phase(market, market_now)

        quote_trade_date = query_date
        price_label = "盘中最新价"
        change_pct_label = "今日盘中涨跌幅"
        freshness_note = "当前为交易时段，price/change_pct 按盘中行情理解。"

        if market_phase == "closed_non_trading_day" or is_trading_day is False:
            quote_trade_date = effective_trading_date
            price_label = "最新可用价"
            change_pct_label = "最近交易日涨跌幅"
            freshness_note = (
                "查询日市场休市，price/change_pct 为最近可用交易日行情，"
                "不代表查询日盘中涨跌。"
            )
        elif market_phase == "pre_open":
            quote_trade_date = effective_trading_date
            price_label = "开盘前最新可用价"
            change_pct_label = "最近交易日涨跌幅"
            freshness_note = (
                "查询时市场尚未开盘，price/change_pct 通常为开盘前最新可用行情。"
            )
        elif market_phase == "post_close":
            price_label = "收盘后最新价"
            change_pct_label = "当日涨跌幅"
            freshness_note = "查询时市场已收盘，price/change_pct 按当日收盘后行情理解。"
        elif market_phase == "unknown":
            price_label = "最新价"
            change_pct_label = "涨跌幅"
            freshness_note = (
                "无法确认当前市场会话，引用 price/change_pct 时必须说明行情时效不确定。"
            )

        return {
            "market": market,
            "market_time": market_now.isoformat(),
            "query_date": query_date.isoformat(),
            "is_trading_day": is_trading_day,
            "market_session": market_phase,
            "is_market_open_now": market_phase == "open",
            "effective_trading_date": _to_iso_date(effective_trading_date),
            "quote_trade_date": _to_iso_date(quote_trade_date),
            "price_label": price_label,
            "change_pct_label": change_pct_label,
            "freshness_note": freshness_note,
        }
    except Exception as exc:
        logger.debug("Unable to build realtime quote session metadata: %s", exc)
        return {
            "market": None,
            "market_session": "unknown",
            "is_market_open_now": None,
            "freshness_note": "无法确认行情会话，引用实时行情时需保守标注时效。",
        }


def _handle_get_realtime_quote(stock_code: str) -> dict:
    """Get real-time stock quote."""
    manager = _get_fetcher_manager()
    quote = manager.get_realtime_quote(stock_code)
    if quote is None:
        return {
            "error": f"No realtime quote available for {stock_code}",
            "retriable": False,
            "note": "All data sources unavailable (network or circuit-breaker). Skip this tool and proceed with historical data only.",
        }

    result = {
        "code": quote.code,
        "name": quote.name,
        "price": quote.price,
        "change_pct": quote.change_pct,
        "change_amount": quote.change_amount,
        "volume": quote.volume,
        "amount": quote.amount,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amplitude": quote.amplitude,
        "open": quote.open_price,
        "high": quote.high,
        "low": quote.low,
        "pre_close": quote.pre_close,
        "pe_ratio": quote.pe_ratio,
        "pb_ratio": quote.pb_ratio,
        "total_mv": quote.total_mv,
        "circ_mv": quote.circ_mv,
        "change_60d": quote.change_60d,
        "source": quote.source.value if hasattr(quote.source, 'value') else str(quote.source),
    }
    result.update(_build_realtime_quote_session_metadata(stock_code, quote))
    return result


get_realtime_quote_tool = ToolDefinition(
    name="get_realtime_quote",
    description="Get real-time stock quote including price, change%, volume ratio, "
                "turnover rate, PE, PB, market cap, and market session metadata "
                "for quote freshness wording.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' (A-share), 'AAPL' (US), 'hk00700' (HK)",
        ),
    ],
    handler=_handle_get_realtime_quote,
    category="data",
)


# ============================================================
# get_daily_history
# ============================================================

def _handle_get_daily_history(stock_code: str, days: int = 60) -> dict:
    """Get daily OHLCV history data."""
    effective_days, metadata = _normalize_history_days(days)

    from src.services.history_loader import load_history_df
    df, source = load_history_df(stock_code, days=effective_days)

    if df is None or df.empty:
        return _append_history_metadata(
            {"error": f"No historical data available for {stock_code}"},
            metadata,
        )

    if source != "db_cache":
        _, normalized_code = _history_code_candidates(stock_code)
        try:
            saved_count = _get_db().save_daily_data(df, normalized_code, source)
            logger.info(
                "Agent daily history persisted for %s (source=%s, new_records=%s)",
                normalized_code,
                source,
                saved_count,
            )
        except Exception as exc:
            logger.warning(
                "Agent daily history persistence failed for %s: %s",
                normalized_code,
                exc,
            )

    # Convert DataFrame to list of dicts (last N records)
    records = df.tail(min(effective_days, len(df))).to_dict(orient="records")
    # Ensure date is string
    for r in records:
        if "date" in r:
            r["date"] = str(r["date"])

    response_code = stock_code
    if source == "db_cache" and records:
        response_code = records[-1].get("code") or response_code

    return _append_history_metadata({
        "code": response_code,
        "source": source,
        "cache_hit": source == "db_cache",
        "requested_days": effective_days,
        "effective_days": effective_days,
        "actual_records": len(records),
        "partial_cache": source == "db_cache" and len(records) < effective_days,
        "total_records": len(records),
        "data": records,
    }, metadata)


get_daily_history_tool = ToolDefinition(
    name="get_daily_history",
    description="Get daily OHLCV (open, high, low, close, volume) historical data "
                "with MA5/MA10/MA20 indicators. Returns the last N trading days.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519' (A-share), 'AAPL' (US)",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="Number of trading days to fetch (default: 60)",
            required=False,
            default=60,
        ),
    ],
    handler=_handle_get_daily_history,
    category="data",
)


# ============================================================
# get_chip_distribution
# ============================================================

def _handle_get_chip_distribution(stock_code: str) -> dict:
    """Get chip distribution data."""
    manager = _get_fetcher_manager()
    fast_result: Optional[dict] = None
    timeout = _get_agent_timeout_attr("agent_chip_distribution_timeout_seconds", 3.0)
    started_at = time.time()
    if len("".join(ch for ch in str(stock_code or "") if ch.isdigit())) == 6:
        try:
            from src.config import get_config

            config = get_config()
            fast_path_enabled = bool(getattr(config, "enable_chip_distribution", True)) and bool(
                getattr(config, "tushare_token", None)
            )
        except Exception:
            fast_path_enabled = False
    else:
        fast_path_enabled = False
    if fast_path_enabled:
        fast_result = _query_tushare_chip_distribution(stock_code)
        if fast_result.get("status") == "ok":
            return fast_result

    remaining_timeout = max(0.0, timeout - (time.time() - started_at))
    if hasattr(manager, "get_chip_distribution_context"):
        ctx, err, cost_ms = _run_manager_task_with_timeout(
            manager,
            lambda: manager.get_chip_distribution_context(stock_code),
            remaining_timeout,
            "chip_distribution",
        )
        if err or not isinstance(ctx, dict):
            source_chain = list(fast_result.get("source_chain", [])) if fast_result else []
            errors = list(fast_result.get("errors", [])) if fast_result else []
            errors.append(str(err or "chip distribution unavailable"))
            source_chain.append({
                "provider": "chip_distribution",
                "result": "timeout" if err and "timeout" in str(err).lower() else "failed",
                "duration_ms": cost_ms,
            })
            return {
                "stock_code": stock_code,
                "status": "timeout" if err and "timeout" in str(err).lower() else "failed",
                "error_summary": str(err or "chip distribution unavailable"),
                "errors": errors,
                "source_chain": source_chain,
                "profit_ratio": None,
                "avg_cost": None,
                "cost_90_low": None,
                "cost_90_high": None,
                "concentration_90": None,
                "cost_70_low": None,
                "cost_70_high": None,
                "concentration_70": None,
            }
        chip = ctx.get("data")
        if chip is None:
            source_chain = list(fast_result.get("source_chain", [])) if fast_result else []
            source_chain.extend(ctx.get("source_chain", []))
            errors = list(fast_result.get("errors", [])) if fast_result else []
            errors.extend(ctx.get("errors", []))
            return {
                "stock_code": ctx.get("stock_code", stock_code),
                "status": ctx.get("status", "failed"),
                "error_summary": ctx.get("error_summary") or "chip distribution unavailable",
                "errors": errors,
                "source_chain": source_chain,
                "profit_ratio": None,
                "avg_cost": None,
                "cost_90_low": None,
                "cost_90_high": None,
                "concentration_90": None,
                "cost_70_low": None,
                "cost_70_high": None,
                "concentration_70": None,
            }
    else:
        remaining_timeout = max(0.0, timeout - (time.time() - started_at))
        chip, err, cost_ms = _run_manager_task_with_timeout(
            manager,
            lambda: manager.get_chip_distribution(stock_code),
            remaining_timeout,
            "chip_distribution",
        )
        if err:
            return {
                "stock_code": stock_code,
                "status": "timeout" if "timeout" in str(err).lower() else "failed",
                "error_summary": str(err),
                "errors": [str(err)],
                "source_chain": [{
                    "provider": "chip_distribution",
                    "result": "timeout" if "timeout" in str(err).lower() else "failed",
                    "duration_ms": cost_ms,
                }],
            }

    if chip is None:
        return {
            "stock_code": stock_code,
            "status": "failed",
            "error_summary": f"No chip distribution data available for {stock_code}",
            "errors": [f"No chip distribution data available for {stock_code}"],
        }

    return {
        "stock_code": chip.code,
        "status": "ok",
        "code": chip.code,
        "date": chip.date,
        "source": chip.source,
        "profit_ratio": chip.profit_ratio,
        "avg_cost": chip.avg_cost,
        "cost_90_low": chip.cost_90_low,
        "cost_90_high": chip.cost_90_high,
        "concentration_90": chip.concentration_90,
        "cost_70_low": chip.cost_70_low,
        "cost_70_high": chip.cost_70_high,
        "concentration_70": chip.concentration_70,
    }


get_chip_distribution_tool = ToolDefinition(
    name="get_chip_distribution",
    description="Get chip distribution analysis for a stock. Returns profit ratio, "
                "average cost, chip concentration at 90% and 70% levels. "
                "Useful for judging support/resistance and holding structure.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_chip_distribution,
    category="data",
)


# ============================================================
# get_analysis_context
# ============================================================

def _handle_get_analysis_context(stock_code: str) -> dict:
    """Get stored analysis context from database."""
    db = _get_db()
    context = db.get_analysis_context(stock_code)

    if context is None:
        return {"error": f"No analysis context in DB for {stock_code}"}

    # Return safely serializable version (remove raw_data to save tokens)
    safe_context = {}
    for k, v in context.items():
        if k == "raw_data":
            safe_context["has_raw_data"] = True
            safe_context["raw_data_count"] = len(v) if isinstance(v, list) else 0
        else:
            safe_context[k] = v

    return safe_context


get_analysis_context_tool = ToolDefinition(
    name="get_analysis_context",
    description="Get historical analysis context from the database for a stock. "
                "Returns today's and yesterday's OHLCV data, MA alignment status, "
                "volume and price changes. Provides the technical data foundation.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_analysis_context,
    category="data",
)


# ============================================================
# get_stock_info
# ============================================================

def _handle_get_stock_info(stock_code: str) -> dict:
    """Get stock fundamental information through unified fundamental context."""
    manager = _get_fetcher_manager()
    try:
        fundamental_context = manager.get_fundamental_context(stock_code)
    except Exception as e:
        logger.warning(f"get_stock_info via fundamental pipeline failed for {stock_code}: {e}")
        fundamental_context = manager.build_failed_fundamental_context(stock_code, str(e))

    compact_context = _compact_fundamental_context(fundamental_context)
    valuation = compact_context.get("valuation", {}).get("data", {})
    sector_rankings = compact_context.get("boards", {}).get("data", {})
    boards_timeout = _get_agent_timeout_attr("agent_stock_info_boards_timeout_seconds", 1.0)
    belong_boards, boards_err, boards_ms = _run_manager_task_with_timeout(
        manager,
        lambda: manager.get_belong_boards(stock_code),
        boards_timeout,
        "belong_boards",
    )
    belong_boards_source_chain = [{
        "provider": "belong_boards",
        "result": "ok" if boards_err is None else ("timeout" if "timeout" in str(boards_err).lower() else "failed"),
        "duration_ms": boards_ms,
    }]
    belong_boards_errors = [str(boards_err)] if boards_err else []
    if not isinstance(belong_boards, list):
        belong_boards = []

    stock_name = stock_code.upper()
    try:
        stock_name = manager.get_stock_name(stock_code, allow_realtime=False) or stock_name
    except TypeError:
        try:
            stock_name = manager.get_stock_name(stock_code) or stock_name
        except Exception:
            pass
    except Exception:
        pass

    stock_info_status = str(compact_context.get("status") or "partial")
    if belong_boards_errors and stock_info_status == "ok":
        stock_info_status = "partial"

    return {
        "status": stock_info_status,
        "code": stock_code.upper(),
        "name": stock_name,
        "pe_ratio": valuation.get("pe_ratio"),
        "pb_ratio": valuation.get("pb_ratio"),
        "total_mv": valuation.get("total_mv"),
        "circ_mv": valuation.get("circ_mv"),
        "fundamental_context": compact_context,
        "belong_boards": belong_boards,
        "belong_boards_source_chain": belong_boards_source_chain,
        "belong_boards_errors": belong_boards_errors,
        # Compatibility alias for existing callers; prefer belong_boards.
        # Planned for future deprecation in a major version.
        "boards": belong_boards,
        "sector_rankings": sector_rankings,
    }


get_stock_info_tool = ToolDefinition(
    name="get_stock_info",
    description="Get stock fundamental information: valuation, growth, earnings, institution flow, "
                "stock sector membership (belong_boards; boards is compatibility alias) and "
                "sector rankings. Returns a compact fundamental_context to reduce token usage.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
    ],
    handler=_handle_get_stock_info,
    category="data",
)


# ============================================================
# get_portfolio_snapshot
# ============================================================

def _handle_get_portfolio_snapshot(
    account_id: Optional[int] = None,
    cost_method: str = "fifo",
    include_positions: bool = False,
    include_risk: bool = True,
    as_of: Optional[str] = None,
) -> dict:
    """Get compact portfolio snapshot for account-aware suggestions."""
    method = (cost_method or "fifo").strip().lower()
    if method not in {"fifo", "avg"}:
        return {"error": "cost_method must be fifo or avg"}

    as_of_date = None
    if as_of:
        try:
            as_of_date = date.fromisoformat(str(as_of).strip())
        except ValueError:
            return {"error": "as_of must be YYYY-MM-DD"}

    try:
        from src.services.portfolio_service import PortfolioService
        from src.services.portfolio_risk_service import PortfolioRiskService
    except Exception as exc:
        logger.warning("get_portfolio_snapshot unavailable: %s", exc)
        return {"status": "not_supported", "error": f"portfolio module unavailable: {exc}"}

    try:
        portfolio_service = PortfolioService()
        snapshot = portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of_date,
            cost_method=method,
        )
        result = {
            "status": "ok",
            "snapshot": _compact_portfolio_snapshot(snapshot, include_positions=bool(include_positions)),
        }
        if include_risk:
            try:
                risk_service = PortfolioRiskService(portfolio_service=portfolio_service)
                risk = risk_service.get_risk_report(
                    account_id=account_id,
                    as_of=as_of_date,
                    cost_method=method,
                )
                result["risk"] = {"status": "ok", **_compact_portfolio_risk(risk)}
            except Exception as risk_exc:
                logger.warning("get_portfolio_snapshot risk block failed: %s", risk_exc)
                result["risk"] = {"status": "failed", "error": str(risk_exc)}
        return result
    except Exception as exc:
        logger.warning("get_portfolio_snapshot failed: %s", exc)
        return {"status": "failed", "error": f"failed to fetch portfolio snapshot: {exc}"}


get_portfolio_snapshot_tool = ToolDefinition(
    name="get_portfolio_snapshot",
    description="Get portfolio snapshot summary and optional risk blocks. "
                "Default returns compact summary for lower token usage; "
                "set include_positions=true to include full position details.",
    parameters=[
        ToolParameter(
            name="account_id",
            type="integer",
            description="Optional account id; omit to use all active accounts.",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="cost_method",
            type="string",
            description="Cost method: fifo or avg (default: fifo).",
            required=False,
            default="fifo",
            enum=["fifo", "avg"],
        ),
        ToolParameter(
            name="include_positions",
            type="boolean",
            description="Whether to include full positions in snapshot output (default: false).",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="include_risk",
            type="boolean",
            description="Whether to include risk summary block (default: true).",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="as_of",
            type="string",
            description="Optional snapshot date in YYYY-MM-DD format (default: today).",
            required=False,
            default=None,
        ),
    ],
    handler=_handle_get_portfolio_snapshot,
    category="data",
)


# ============================================================
# Export all data tools
# ============================================================

ALL_DATA_TOOLS = [
    get_realtime_quote_tool,
    get_daily_history_tool,
    get_chip_distribution_tool,
    get_analysis_context_tool,
    get_stock_info_tool,
    get_portfolio_snapshot_tool,
]


# ============================================================
# get_capital_flow
# ============================================================

def _normalize_stockapi_date_arg(value: Optional[str], field_name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"{field_name} must be YYYY-MM-DD or YYYYMMDD")


def _normalize_stockapi_page_arg(value: Any, default: int, minimum: int = 1, maximum: int = 200) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _handle_get_capital_flow(
    stock_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page_no: int = 1,
    page_size: int = 50,
) -> dict:
    """Get main-force capital flow data for a stock."""
    manager = _get_fetcher_manager()
    try:
        normalized_start_date = _normalize_stockapi_date_arg(start_date, "start_date")
        normalized_end_date = _normalize_stockapi_date_arg(end_date, "end_date")
        normalized_page_no = _normalize_stockapi_page_arg(page_no, default=1)
        normalized_page_size = _normalize_stockapi_page_arg(page_size, default=50)
        try:
            from src.config import get_config
            timeout = float(getattr(get_config(), "agent_capital_flow_timeout_seconds", 3.0))
        except Exception:
            timeout = 3.0
        ctx = manager.get_capital_flow_context(
            stock_code,
            budget_seconds=timeout,
            start_date=normalized_start_date,
            end_date=normalized_end_date,
            page_no=normalized_page_no,
            page_size=normalized_page_size,
        )
    except Exception as exc:
        logger.warning("get_capital_flow failed for %s: %s", stock_code, exc)
        return {
            "stock_code": stock_code,
            "status": "error",
            "error": f"capital flow fetch failed: {exc}",
        }

    status = ctx.get("status", "not_supported")
    if status == "not_supported":
        return {
            "stock_code": stock_code,
            "status": "not_supported",
            "note": "Capital flow data is only available for A-share stocks (not ETFs/indices).",
        }

    data = ctx.get("data", {})
    stock_flow = data.get("stock_flow") or {}
    sector_rankings = data.get("sector_rankings") or {}
    errors = ctx.get("errors") or []
    source_chain = list(ctx.get("source_chain", []))
    error_summary = None
    if errors:
        joined_errors = " | ".join(str(item) for item in errors if str(item).strip())
        if "timeout" in joined_errors.lower() or "timed out" in joined_errors.lower():
            error_summary = "capital-flow endpoint timeout"
        elif "tushare_moneyflow" in joined_errors:
            if "empty_data" in joined_errors:
                error_summary = "Tushare moneyflow returned no capital-flow rows for the queried window"
            elif "invalid_date" in joined_errors:
                error_summary = "Tushare moneyflow query window is invalid"
            else:
                error_summary = "Tushare moneyflow capital-flow endpoint failed"
        elif "stockapi_codeFlow" in joined_errors:
            if "empty_data" in joined_errors:
                error_summary = "StockAPI codeFlow returned no capital-flow rows for the queried window"
            else:
                error_summary = "StockAPI codeFlow capital-flow endpoint failed"
        elif "push2his.eastmoney.com" in joined_errors or "push2.eastmoney.com" in joined_errors:
            error_summary = "Eastmoney capital-flow endpoint unreachable"
        elif "RemoteDisconnected" in joined_errors or "remote end closed" in joined_errors.lower():
            error_summary = "Eastmoney capital-flow endpoint disconnected"
        else:
            error_summary = str(errors[0])

    return {
        "stock_code": stock_code,
        "status": status,
        "query": {
            "start_date": normalized_start_date,
            "end_date": normalized_end_date,
            "page_no": normalized_page_no,
            "page_size": normalized_page_size,
        },
        "main_net_inflow": stock_flow.get("main_net_inflow"),
        "inflow_5d": stock_flow.get("inflow_5d"),
        "inflow_10d": stock_flow.get("inflow_10d"),
        "latest_date": stock_flow.get("latest_date"),
        "source_update": stock_flow.get("source_update"),
        "source_chain": source_chain,
        "sector_rankings": {
            "top_inflow_sectors": sector_rankings.get("top", [])[:3],
            "top_outflow_sectors": sector_rankings.get("bottom", [])[:3],
        },
        "error_summary": error_summary,
        "errors": errors,
    }


get_capital_flow_tool = ToolDefinition(
    name="get_capital_flow",
    description=(
        "Get main-force (主力) capital flow data for an A-share stock. "
        "Returns the latest daily net inflow, 5-day and 10-day cumulative inflows. "
        "Only supported for A-share individual stocks (not ETFs, indices, HK, or US stocks)."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="start_date",
            type="string",
            description="Optional capital-flow start date, YYYY-MM-DD or YYYYMMDD. Tushare moneyflow runs first; if omitted, the tool uses the latest completed trading-day window.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="end_date",
            type="string",
            description="Optional capital-flow end date, YYYY-MM-DD or YYYYMMDD. Before 15:30 the default end date is the previous trading day.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="page_no",
            type="integer",
            description="Optional StockAPI codeFlow pageNo used only by fallback requests, default 1.",
            required=False,
            default=1,
        ),
        ToolParameter(
            name="page_size",
            type="integer",
            description="Optional StockAPI codeFlow pageSize used only by fallback requests, default 50 and clamped to 1-200.",
            required=False,
            default=50,
        ),
    ],
    handler=_handle_get_capital_flow,
    category="data",
)


ALL_DATA_TOOLS.append(get_capital_flow_tool)


# ============================================================
# market-level capital flow tools
# ============================================================

def _get_fundamental_adapter():
    from data_provider.fundamental_adapter import AkshareFundamentalAdapter

    return AkshareFundamentalAdapter()


def _to_tushare_ts_code(stock_code: str) -> str:
    raw = str(stock_code or "").strip().upper()
    if "." in raw and raw.endswith((".SH", ".SZ", ".BJ", ".HK")):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _normalize_tushare_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("-", "")[:8]


def _normalize_ts_code_to_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return "".join(ch for ch in text if ch.isdigit())


def _safe_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        if not text or text.lower() == "nan":
            return None
        return float(text)
    except Exception:
        return None


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() == "nan":
        return ""
    return text


def _latest_tushare_trade_date(update_hour: int = 16, update_minute: int = 0) -> str:
    dates = _recent_tushare_trade_dates(update_hour=update_hour, update_minute=update_minute, max_dates=1)
    if dates:
        return dates[0]


def _recent_tushare_trade_dates(
    update_hour: int = 16,
    update_minute: int = 0,
    max_dates: int = 4,
    lookback_days: int = 30,
) -> List[str]:
    now = datetime.now()
    cutoff = now.replace(hour=update_hour, minute=update_minute, second=0, microsecond=0)
    cache_key = (
        int(update_hour),
        int(update_minute),
        int(max_dates),
        int(lookback_days),
        now.date().isoformat(),
        bool(now >= cutoff),
    )
    with _tushare_trade_date_cache_lock:
        cached = _tushare_trade_date_cache.get(cache_key)
    if cached is not None:
        return list(cached)
    end_day = now.date() if now >= cutoff else (now.date() - timedelta(days=1))
    start_day = end_day - timedelta(days=max(lookback_days, max_dates * 3))
    cal = _tushare_query(
        "trade_cal",
        {
            "exchange": "SSE",
            "start_date": start_day.strftime("%Y%m%d"),
            "end_date": end_day.strftime("%Y%m%d"),
        },
        "cal_date,is_open",
        limit=40,
        timeout=_get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0),
    )
    dates: List[str] = []
    for row in cal.get("items") or []:
        if str(row.get("is_open")) in {"1", "1.0", "True", "true"}:
            trade_date = str(row.get("cal_date") or "").replace("-", "")[:8]
            if trade_date:
                dates.append(trade_date)
    if dates:
        resolved_dates = sorted(set(dates), reverse=True)[:max(1, int(max_dates))]
        with _tushare_trade_date_cache_lock:
            _tushare_trade_date_cache[cache_key] = list(resolved_dates)
        return resolved_dates

    fallback_dates: List[str] = []
    day = end_day
    while len(fallback_dates) < max(1, int(max_dates)) and day >= start_day:
        if day.weekday() < 5:
            fallback_dates.append(day.strftime("%Y%m%d"))
        day = day - timedelta(days=1)
    with _tushare_trade_date_cache_lock:
        _tushare_trade_date_cache[cache_key] = list(fallback_dates)
    return fallback_dates


def _latest_weekday_date(update_hour: int = 16, update_minute: int = 0) -> str:
    dates = _recent_weekday_dates(update_hour=update_hour, update_minute=update_minute, max_dates=1)
    return dates[0]


def _recent_weekday_dates(update_hour: int = 16, update_minute: int = 0, max_dates: int = 4) -> List[str]:
    now = datetime.now()
    cutoff = now.replace(hour=update_hour, minute=update_minute, second=0, microsecond=0)
    day = now.date() if now >= cutoff else (now.date() - timedelta(days=1))
    dates: List[str] = []
    while len(dates) < max(1, int(max_dates)):
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day = day - timedelta(days=1)
    return dates


def _tushare_query(
    api_name: str,
    params: Optional[Dict[str, Any]] = None,
    fields: str = "",
    limit: int = 30,
    timeout: Optional[float] = None,
) -> dict:
    from data_provider.tushare_client import get_tushare_http_url, query_tushare_api

    started_at = time.time()
    request_timeout = timeout
    if request_timeout is None:
        request_timeout = _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)
    effective_params = {
        key: value
        for key, value in (params or {}).items()
        if value is not None and str(value).strip() != ""
    }
    try:
        df = query_tushare_api(
            api_name,
            params=effective_params,
            fields=fields,
            timeout=int(max(1, math.ceil(float(request_timeout)))),
        )
    except Exception as exc:
        duration_ms = int((time.time() - started_at) * 1000)
        logger.warning("Tushare %s failed: %s", api_name, exc)
        return {
            "status": "timeout" if "timeout" in str(exc).lower() or "timed out" in str(exc).lower() else "failed",
            "api_name": api_name,
            "items": [],
            "source_chain": [{
                "provider": f"tushare:{api_name}",
                "result": "timeout" if "timeout" in str(exc).lower() or "timed out" in str(exc).lower() else "failed",
                "duration_ms": duration_ms,
                "endpoint": get_tushare_http_url(),
                "params": effective_params,
            }],
            "errors": [str(exc)],
        }

    duration_ms = int((time.time() - started_at) * 1000)
    if df is None or df.empty:
        return {
            "status": "empty",
            "api_name": api_name,
            "items": [],
            "source_chain": [{
                "provider": f"tushare:{api_name}",
                "result": "empty",
                "duration_ms": duration_ms,
                "endpoint": get_tushare_http_url(),
                "params": effective_params,
            }],
            "errors": [],
        }
    items = df.head(max(1, min(int(limit or 30), 200))).to_dict(orient="records")
    return {
        "status": "ok",
        "api_name": api_name,
        "items": items,
        "total_rows": int(len(df)),
        "source_chain": [{
            "provider": f"tushare:{api_name}",
            "result": "ok",
            "duration_ms": duration_ms,
            "endpoint": get_tushare_http_url(),
            "params": effective_params,
        }],
        "errors": [],
    }


def _tushare_query_all_rows(
    api_name: str,
    params: Optional[Dict[str, Any]] = None,
    fields: str = "",
    timeout: Optional[float] = None,
) -> dict:
    from data_provider.tushare_client import get_tushare_http_url, query_tushare_api

    started_at = time.time()
    request_timeout = timeout
    if request_timeout is None:
        request_timeout = _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)
    effective_params = {
        key: value
        for key, value in (params or {}).items()
        if value is not None and str(value).strip() != ""
    }
    try:
        df = query_tushare_api(
            api_name,
            params=effective_params,
            fields=fields,
            timeout=int(max(1, math.ceil(float(request_timeout)))),
        )
    except Exception as exc:
        duration_ms = int((time.time() - started_at) * 1000)
        logger.warning("Tushare %s failed: %s", api_name, exc)
        return {
            "status": "timeout" if "timeout" in str(exc).lower() or "timed out" in str(exc).lower() else "failed",
            "api_name": api_name,
            "items": [],
            "source_chain": [{
                "provider": f"tushare:{api_name}",
                "result": "timeout" if "timeout" in str(exc).lower() or "timed out" in str(exc).lower() else "failed",
                "duration_ms": duration_ms,
                "endpoint": get_tushare_http_url(),
                "params": effective_params,
            }],
            "errors": [str(exc)],
        }

    duration_ms = int((time.time() - started_at) * 1000)
    if df is None or df.empty:
        return {
            "status": "empty",
            "api_name": api_name,
            "items": [],
            "source_chain": [{
                "provider": f"tushare:{api_name}",
                "result": "empty",
                "duration_ms": duration_ms,
                "endpoint": get_tushare_http_url(),
                "params": effective_params,
            }],
            "errors": [],
        }
    items = df.to_dict(orient="records")
    return {
        "status": "ok",
        "api_name": api_name,
        "items": items,
        "total_rows": int(len(df)),
        "source_chain": [{
            "provider": f"tushare:{api_name}",
            "result": "ok",
            "duration_ms": duration_ms,
            "endpoint": get_tushare_http_url(),
            "params": effective_params,
        }],
        "errors": [],
    }


def _query_tushare_chip_distribution(stock_code: str) -> dict:
    """Fast-path chip distribution through Tushare cyq_chips."""
    started_at = time.time()
    # Avoid a blocking trade_cal preflight here: cyq_chips itself is enough to
    # tell whether the latest completed weekday has data, and private gateways
    # often make trade_cal slower than the actual target endpoint.
    trade_dates = _recent_weekday_dates(update_hour=19, update_minute=0, max_dates=4)
    ts_code = _to_tushare_ts_code(stock_code)
    timeout = max(2.0, _get_agent_timeout_attr("agent_chip_distribution_timeout_seconds", 3.0))
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_status = "failed"
    rows: List[Tuple[float, float]] = []
    current_price: Optional[float] = None
    trade_date = trade_dates[0] if trade_dates else _latest_weekday_date(update_hour=19, update_minute=0)

    for candidate_date in trade_dates:
        trade_date = candidate_date
        chips = _tushare_query(
            "cyq_chips",
            {"ts_code": ts_code, "start_date": candidate_date, "end_date": candidate_date},
            "ts_code,trade_date,price,percent",
            limit=200,
            timeout=timeout,
        )
        source_chain.extend(chips.get("source_chain", []))
        errors.extend(chips.get("errors", []))
        if chips.get("status") != "ok":
            last_status = chips.get("status") or "failed"
            if chips.get("status") in {"failed", "timeout"}:
                break
            continue

        daily = _tushare_query(
            "daily",
            {"ts_code": ts_code, "start_date": candidate_date, "end_date": candidate_date},
            "ts_code,trade_date,close",
            limit=1,
            timeout=timeout,
        )
        source_chain.extend(daily.get("source_chain", []))
        errors.extend(daily.get("errors", []))
        if daily.get("status") != "ok" or not daily.get("items"):
            errors.append(f"tushare:daily unavailable for cyq_chips:{candidate_date}")
            last_status = daily.get("status") or "failed"
            if daily.get("status") in {"failed", "timeout"}:
                break
            continue

        current_price = _safe_number(daily["items"][0].get("close"))
        rows = [
            (_safe_number(row.get("price")), _safe_number(row.get("percent")))
            for row in chips.get("items") or []
        ]
        rows = [(price, weight) for price, weight in rows if price is not None and weight is not None and weight > 0]
        if not rows or current_price is None:
            errors.append(f"tushare:cyq_chips unusable rows:{candidate_date}")
            last_status = "failed"
            continue
        break

    if not rows or current_price is None:
        error_summary = "Tushare cyq_chips returned no usable chip rows"
        if last_status == "timeout":
            error_summary = "Tushare cyq_chips timed out"
        elif last_status == "failed":
            error_summary = "Tushare cyq_chips failed"
        return {
            "stock_code": stock_code,
            "status": "timeout" if last_status == "timeout" else "failed",
            "error_summary": error_summary,
            "errors": errors or ["tushare:cyq_chips unavailable"],
            "source_chain": source_chain,
        }

    rows.sort(key=lambda item: item[0])
    total_weight = sum(weight for _, weight in rows) or 1.0
    profit_ratio = sum(weight for price, weight in rows if price <= current_price) / total_weight
    avg_cost = sum(price * weight for price, weight in rows) / total_weight

    def percentile_price(target: float) -> float:
        threshold = total_weight * target
        cumulative = 0.0
        for price, weight in rows:
            cumulative += weight
            if cumulative >= threshold:
                return price
        return rows[-1][0]

    cost_90_low = percentile_price(0.05)
    cost_90_high = percentile_price(0.95)
    cost_70_low = percentile_price(0.15)
    cost_70_high = percentile_price(0.85)

    def concentration(low: float, high: float) -> float:
        denominator = high + low
        if not denominator:
            return 0.0
        return (high - low) / denominator

    return {
        "stock_code": stock_code,
        "status": "ok",
        "code": stock_code,
        "date": datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d"),
        "source": "tushare:cyq_chips",
        "profit_ratio": round(profit_ratio, 4),
        "avg_cost": round(avg_cost, 4),
        "cost_90_low": round(cost_90_low, 4),
        "cost_90_high": round(cost_90_high, 4),
        "concentration_90": round(concentration(cost_90_low, cost_90_high), 4),
        "cost_70_low": round(cost_70_low, 4),
        "cost_70_high": round(cost_70_high, 4),
        "concentration_70": round(concentration(cost_70_low, cost_70_high), 4),
        "source_chain": source_chain + [{
            "provider": "tushare:cyq_chips_metrics",
            "result": "ok",
            "duration_ms": int((time.time() - started_at) * 1000),
            "trade_date": trade_date,
        }],
        "errors": [],
    }


def _query_tushare_stock_moneyflow(stock_code: str, timeout_seconds: Optional[float] = None) -> dict:
    """Fallback stock capital flow through Tushare moneyflow."""
    ts_code = _to_tushare_ts_code(stock_code)
    latest = _latest_weekday_date(update_hour=15, update_minute=30)
    start = (datetime.strptime(latest, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    timeout = max(0.0, float(
        timeout_seconds
        if timeout_seconds is not None
        else 3.0
    ))
    fields = (
        "ts_code,trade_date,buy_lg_amount,sell_lg_amount,"
        "buy_elg_amount,sell_elg_amount,net_mf_amount"
    )
    params = {"ts_code": ts_code, "start_date": start, "end_date": latest}
    if timeout <= 0:
        return {
            "stock_code": stock_code,
            "status": "timeout",
            "api_name": "moneyflow",
            "errors": ["tushare:moneyflow fallback budget exhausted"],
            "source_chain": [{
                "provider": "tushare:moneyflow",
                "result": "timeout",
                "duration_ms": 0,
                "params": params,
            }],
        }
    result = _tushare_query("moneyflow", params, fields, limit=20, timeout=timeout)
    if result.get("status") == "empty":
        # The private gateway can occasionally return an empty frame under load.
        # Retry once on the same serialized Tushare path before declaring missing data.
        retry_result = _tushare_query("moneyflow", params, fields, limit=20, timeout=timeout)
        retry_result["source_chain"] = list(result.get("source_chain", [])) + list(retry_result.get("source_chain", []))
        retry_result["errors"] = list(result.get("errors", [])) + list(retry_result.get("errors", []))
        result = retry_result
    if result.get("status") != "ok":
        result["stock_code"] = stock_code
        return result

    dated_amounts: List[Tuple[str, float]] = []
    for row in result.get("items") or []:
        trade_date = str(row.get("trade_date") or "").replace("-", "")[:8]
        amount = _safe_number(row.get("net_mf_amount"))
        if amount is None:
            buy_lg = _safe_number(row.get("buy_lg_amount")) or 0.0
            sell_lg = _safe_number(row.get("sell_lg_amount")) or 0.0
            buy_elg = _safe_number(row.get("buy_elg_amount")) or 0.0
            sell_elg = _safe_number(row.get("sell_elg_amount")) or 0.0
            amount = (buy_lg + buy_elg) - (sell_lg + sell_elg)
        if trade_date and amount is not None:
            # Tushare moneyflow amount columns are in 10k yuan; keep tool output in yuan.
            dated_amounts.append((trade_date, float(amount) * 10000.0))

    dated_amounts.sort(key=lambda item: item[0])
    if not dated_amounts:
        return {
            "stock_code": stock_code,
            "status": "empty",
            "api_name": "moneyflow",
            "errors": ["tushare:moneyflow empty usable rows"],
            "source_chain": result.get("source_chain", []),
        }
    amounts = [item[1] for item in dated_amounts]
    latest_date, latest_amount = dated_amounts[-1]
    return {
        "stock_code": stock_code,
        "status": "ok",
        "main_net_inflow": latest_amount,
        "inflow_5d": float(sum(amounts[-5:])),
        "inflow_10d": float(sum(amounts[-10:])),
        "latest_date": f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}",
        "source_update": "tushare_moneyflow_after_market_close",
        "source_chain": result.get("source_chain", []),
        "errors": [],
    }


def _handle_get_tushare_moneyflow_ths(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get THS stock money-flow ranking from Tushare without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    trade_dates = [requested_date] if requested_date else _recent_tushare_trade_dates(
        update_hour=15,
        update_minute=30,
        max_dates=4,
        lookback_days=20,
    )
    if not trade_dates:
        trade_dates = [_latest_weekday_date(update_hour=15, update_minute=30)]

    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = (
        "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,"
        "buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,"
        "buy_sm_amount,buy_sm_amount_rate"
    )
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_result: dict = {}

    for candidate_date in trade_dates:
        result = _tushare_query_all_rows(
            "moneyflow_ths",
            {"trade_date": candidate_date},
            fields,
            timeout=_get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0),
        )
        source_chain.extend(result.get("source_chain", []))
        errors.extend(result.get("errors", []))
        last_result = result
        if result.get("status") != "ok":
            if requested_date or result.get("status") in {"failed", "timeout"}:
                break
            continue

        raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        normalized_items: List[Dict[str, Any]] = []
        for item in raw_items:
            symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
            if target_symbol and symbol != target_symbol:
                continue
            net_amount = _safe_number(item.get("net_amount"))
            net_d5_amount = _safe_number(item.get("net_d5_amount"))
            buy_lg_amount = _safe_number(item.get("buy_lg_amount"))
            buy_md_amount = _safe_number(item.get("buy_md_amount"))
            buy_sm_amount = _safe_number(item.get("buy_sm_amount"))
            normalized_items.append({
                "trade_date": str(item.get("trade_date") or candidate_date),
                "code": symbol,
                "ts_code": str(item.get("ts_code") or "").strip(),
                "name": str(item.get("name") or symbol).strip(),
                "latest": _safe_number(item.get("latest")),
                "change_ratio": _safe_number(item.get("pct_change")),
                "pct_change": _safe_number(item.get("pct_change")),
                # Tushare moneyflow_ths amount fields are in 10k yuan; expose yuan.
                "net_inflow": net_amount * 10000.0 if net_amount is not None else None,
                "net_5d_inflow": net_d5_amount * 10000.0 if net_d5_amount is not None else None,
                "large_net_inflow": buy_lg_amount * 10000.0 if buy_lg_amount is not None else None,
                "large_net_inflow_rate": _safe_number(item.get("buy_lg_amount_rate")),
                "medium_net_inflow": buy_md_amount * 10000.0 if buy_md_amount is not None else None,
                "medium_net_inflow_rate": _safe_number(item.get("buy_md_amount_rate")),
                "small_net_inflow": buy_sm_amount * 10000.0 if buy_sm_amount is not None else None,
                "small_net_inflow_rate": _safe_number(item.get("buy_sm_amount_rate")),
                "source": "tushare:moneyflow_ths",
            })

        normalized_items.sort(
            key=lambda item: (
                float(item.get("net_inflow") or 0.0),
                float(item.get("net_5d_inflow") or 0.0),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        items = normalized_items[:effective_limit]
        return {
            "status": "ok" if items else "empty",
            "api_name": "moneyflow_ths",
            "trade_date": candidate_date,
            "stock_code": stock_code,
            "items": items,
            "total_rows": int(result.get("total_rows") or len(raw_items)),
            "source_chain": source_chain,
            "errors": errors,
        }

    return {
        "status": last_result.get("status") or "failed",
        "api_name": "moneyflow_ths",
        "trade_date": trade_dates[0] if trade_dates else requested_date,
        "stock_code": stock_code,
        "items": [],
        "source_chain": source_chain,
        "errors": errors or [f"tushare:moneyflow_ths unavailable for {trade_dates[0] if trade_dates else requested_date}"],
    }


get_tushare_moneyflow_ths_tool = ToolDefinition(
    name="get_tushare_moneyflow_ths",
    description=(
        "Get Tushare THS stock money-flow ranking without fallback. Returns top stocks by "
        "same-day main net inflow and 5-day net inflow. Amount fields are normalized to yuan."
    ),
    parameters=[
        ToolParameter(
            name="trade_date",
            type="string",
            description="Optional trade date YYYYMMDD or YYYY-MM-DD. Blank uses the latest completed trading date.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="stock_code",
            type="string",
            description="Optional A-share stock code to filter, e.g. 600519 or 600519.SH. Blank returns ranking rows.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max rows to return (default: 30, max: 200).",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_tushare_moneyflow_ths,
    category="data",
)


def _tushare_recent_date_query(
    api_name: str,
    *,
    requested_date: str,
    fields: str,
    param_builder: Optional[Callable[[str], Dict[str, Any]]] = None,
    max_dates: int = 4,
    lookback_days: int = 20,
    update_hour: int = 15,
    update_minute: int = 30,
    timeout: Optional[float] = None,
) -> Tuple[str, dict, List[Dict[str, Any]], List[str]]:
    trade_dates = [requested_date] if requested_date else _recent_tushare_trade_dates(
        update_hour=update_hour,
        update_minute=update_minute,
        max_dates=max_dates,
        lookback_days=lookback_days,
    )
    if not trade_dates:
        trade_dates = [_latest_weekday_date(update_hour=update_hour, update_minute=update_minute)]

    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_result: dict = {}
    selected_date = trade_dates[0]
    for candidate_date in trade_dates:
        selected_date = candidate_date
        params = {"trade_date": candidate_date}
        if param_builder is not None:
            params.update(param_builder(candidate_date))
        result = _tushare_query_all_rows(
            api_name,
            params,
            fields,
            timeout=timeout if timeout is not None else _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0),
        )
        source_chain.extend(result.get("source_chain", []))
        errors.extend(result.get("errors", []))
        last_result = result
        if result.get("status") == "ok":
            return candidate_date, result, source_chain, errors
        if requested_date or result.get("status") in {"failed", "timeout"}:
            break
    if not last_result:
        last_result = {"status": "failed", "items": []}
    return selected_date, last_result, source_chain, errors


def _parse_limit_streak(value: Any) -> Optional[float]:
    number = _safe_number(value)
    if number is not None:
        return number
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"(\d+)\s*天\s*(\d+)\s*板", text)
    if match:
        return float(match.group(2))
    if "首板" in text:
        return 1.0
    return None


def _normalize_tushare_concepts(value: Any) -> List[str]:
    text = _clean_text(value)
    if not text:
        return []
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            import ast

            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item or "").strip()]
        except Exception:
            pass
    return [part.strip() for part in re.split(r"[,，;；、]+", text) if part.strip()]


def _handle_get_tushare_moneyflow_dc(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Eastmoney stock money-flow ranking from Tushare without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = (
        "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,"
        "buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
        "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "moneyflow_dc",
        requested_date=requested_date,
        fields=fields,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "moneyflow_dc",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:moneyflow_dc unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        net_amount = _safe_number(item.get("net_amount"))
        buy_elg_amount = _safe_number(item.get("buy_elg_amount"))
        buy_lg_amount = _safe_number(item.get("buy_lg_amount"))
        buy_md_amount = _safe_number(item.get("buy_md_amount"))
        buy_sm_amount = _safe_number(item.get("buy_sm_amount"))
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("name")) or symbol,
            "latest": _safe_number(item.get("close")),
            "close": _safe_number(item.get("close")),
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            # Tushare moneyflow_dc amount fields are in 10k yuan; expose yuan.
            "net_inflow": net_amount * 10000.0 if net_amount is not None else None,
            "net_inflow_rate": _safe_number(item.get("net_amount_rate")),
            "extra_large_net_inflow": buy_elg_amount * 10000.0 if buy_elg_amount is not None else None,
            "extra_large_net_inflow_rate": _safe_number(item.get("buy_elg_amount_rate")),
            "large_net_inflow": buy_lg_amount * 10000.0 if buy_lg_amount is not None else None,
            "large_net_inflow_rate": _safe_number(item.get("buy_lg_amount_rate")),
            "medium_net_inflow": buy_md_amount * 10000.0 if buy_md_amount is not None else None,
            "medium_net_inflow_rate": _safe_number(item.get("buy_md_amount_rate")),
            "small_net_inflow": buy_sm_amount * 10000.0 if buy_sm_amount is not None else None,
            "small_net_inflow_rate": _safe_number(item.get("buy_sm_amount_rate")),
            "source": "tushare:moneyflow_dc",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("extra_large_net_inflow") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "moneyflow_dc",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_moneyflow_dc_tool = ToolDefinition(
    name="get_tushare_moneyflow_dc",
    description=(
        "Get Tushare Eastmoney stock money-flow ranking without fallback. Returns main-force "
        "net inflow plus order-size buckets. Amount fields are normalized to yuan."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_moneyflow_dc,
    category="data",
)


def _handle_get_tushare_dragon_tiger_list(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare dragon-tiger stock list without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = (
        "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,"
        "l_sell,l_buy,l_amount,net_amount,net_rate,amount_rate,reason"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "top_list",
        requested_date=requested_date,
        fields=fields,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "top_list",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:top_list unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("name")) or symbol,
            "latest": _safe_number(item.get("close")),
            "close": _safe_number(item.get("close")),
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            "turnover_ratio": _safe_number(item.get("turnover_rate")),
            "turnover_rate": _safe_number(item.get("turnover_rate")),
            "amount": _safe_number(item.get("amount")),
            "buy_amount": _safe_number(item.get("l_buy")),
            "sell_amount": _safe_number(item.get("l_sell")),
            "dragon_tiger_amount": _safe_number(item.get("l_amount")),
            "net_inflow": _safe_number(item.get("net_amount")),
            "net_rate": _safe_number(item.get("net_rate")),
            "amount_rate": _safe_number(item.get("amount_rate")),
            "reason": _clean_text(item.get("reason")),
            "source": "tushare:top_list",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("amount") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "top_list",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_dragon_tiger_list_tool = ToolDefinition(
    name="get_tushare_dragon_tiger_list",
    description=(
        "Get Tushare dragon-tiger daily stock list without fallback. Returns buy/sell/net "
        "amounts, turnover and listing reasons."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_dragon_tiger_list,
    category="data",
)


def _handle_get_tushare_dragon_tiger_inst(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare dragon-tiger institution/seat detail without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = "trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason"
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "top_inst",
        requested_date=requested_date,
        fields=fields,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "top_inst",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:top_inst unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        if not symbol:
            continue
        buy_amount = _safe_number(item.get("buy")) or 0.0
        sell_amount = _safe_number(item.get("sell")) or 0.0
        net_buy = _safe_number(item.get("net_buy"))
        if net_buy is None:
            net_buy = buy_amount - sell_amount
        bucket = grouped.setdefault(symbol, {
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": symbol,
            "seat_count": 0,
            "institution_seat_count": 0,
            "buy_amount": 0.0,
            "sell_amount": 0.0,
            "net_inflow": 0.0,
            "top_seats": [],
            "reason": _clean_text(item.get("reason")),
            "source": "tushare:top_inst",
        })
        exalter = _clean_text(item.get("exalter"))
        bucket["seat_count"] += 1
        if "机构专用" in exalter:
            bucket["institution_seat_count"] += 1
        bucket["buy_amount"] += buy_amount
        bucket["sell_amount"] += sell_amount
        bucket["net_inflow"] += float(net_buy)
        if len(bucket["top_seats"]) < 5:
            bucket["top_seats"].append({
                "exalter": exalter,
                "side": _clean_text(item.get("side")),
                "buy_amount": buy_amount,
                "sell_amount": sell_amount,
                "net_buy": float(net_buy),
                "reason": _clean_text(item.get("reason")),
            })
        if not bucket.get("reason") and _clean_text(item.get("reason")):
            bucket["reason"] = _clean_text(item.get("reason"))

    normalized_items = list(grouped.values())
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("buy_amount") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "top_inst",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_dragon_tiger_inst_tool = ToolDefinition(
    name="get_tushare_dragon_tiger_inst",
    description=(
        "Get Tushare dragon-tiger seat/institution detail without fallback. Rows are grouped "
        "by stock and expose aggregated buy/sell/net amounts plus top seats."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max stocks to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_dragon_tiger_inst,
    category="data",
)


def _handle_get_tushare_limit_list_ths(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare THS limit-up/down list without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = (
        "trade_date,ts_code,name,price,pct_chg,open_num,lu_desc,limit_type,tag,status,"
        "limit_order,limit_amount,turnover_rate,turnover"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "limit_list_ths",
        requested_date=requested_date,
        fields=fields,
        timeout=max(8.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "limit_list_ths",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:limit_list_ths unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        tag = _clean_text(item.get("tag"))
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("name")) or symbol,
            "latest": _safe_number(item.get("price")),
            "price": _safe_number(item.get("price")),
            "change_ratio": _safe_number(item.get("pct_chg")),
            "pct_change": _safe_number(item.get("pct_chg")),
            "open_times": _safe_number(item.get("open_num")),
            "bomb_num": _safe_number(item.get("open_num")),
            "reason": _clean_text(item.get("lu_desc")),
            "limit_type": _clean_text(item.get("limit_type")),
            "tag": tag,
            "status_label": _clean_text(item.get("status")),
            "limit_order": _safe_number(item.get("limit_order")),
            "ceiling_amount": _safe_number(item.get("limit_amount")),
            "turnover_rate": _safe_number(item.get("turnover_rate")),
            "turnover_ratio": _safe_number(item.get("turnover_rate")),
            "amount": _safe_number(item.get("turnover")),
            "limit_up_streak": _parse_limit_streak(tag),
            "source": "tushare:limit_list_ths",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("limit_up_streak") or 0.0),
            float(item.get("ceiling_amount") or 0.0),
            float(item.get("change_ratio") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "limit_list_ths",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_limit_list_ths_tool = ToolDefinition(
    name="get_tushare_limit_list_ths",
    description=(
        "Get Tushare THS limit-up/down list without fallback. Returns limit streak tag, "
        "sealing amount, open count, reason and turnover fields."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_limit_list_ths,
    category="data",
)


def _handle_get_tushare_limit_list_d(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare daily limit-up/down list without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = (
        "trade_date,ts_code,industry,name,close,pct_chg,amount,fd_amount,"
        "first_time,last_time,open_times,up_stat,limit_times,limit"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "limit_list_d",
        requested_date=requested_date,
        fields=fields,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "limit_list_d",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:limit_list_d unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("name")) or symbol,
            "industry": _clean_text(item.get("industry")),
            "latest": _safe_number(item.get("close")),
            "close": _safe_number(item.get("close")),
            "change_ratio": _safe_number(item.get("pct_chg")),
            "pct_change": _safe_number(item.get("pct_chg")),
            "amount": _safe_number(item.get("amount")),
            "ceiling_amount": _safe_number(item.get("fd_amount")),
            "first_limit_time": _clean_text(item.get("first_time")),
            "last_limit_time": _clean_text(item.get("last_time")),
            "open_times": _safe_number(item.get("open_times")),
            "bomb_num": _safe_number(item.get("open_times")),
            "up_stat": _clean_text(item.get("up_stat")),
            "limit_up_streak": _safe_number(item.get("limit_times")),
            "limit_status": _clean_text(item.get("limit")),
            "source": "tushare:limit_list_d",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("limit_up_streak") or 0.0),
            float(item.get("ceiling_amount") or 0.0),
            float(item.get("amount") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "limit_list_d",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_limit_list_d_tool = ToolDefinition(
    name="get_tushare_limit_list_d",
    description=(
        "Get Tushare daily limit-up/down list without fallback. Returns amount, sealing "
        "amount, open count, first/last limit time and streak fields."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_limit_list_d,
    category="data",
)


def _handle_get_tushare_limit_step(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare continuous limit-up ladder without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    fields = "ts_code,name,trade_date,nums"
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "limit_step",
        requested_date=requested_date,
        fields=fields,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "limit_step",
            "trade_date": selected_date,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:limit_step unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("name")) or symbol,
            "limit_up_streak": _safe_number(item.get("nums")),
            "source": "tushare:limit_step",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("limit_up_streak") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "limit_step",
        "trade_date": selected_date,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_limit_step_tool = ToolDefinition(
    name="get_tushare_limit_step",
    description="Get Tushare continuous limit-up ladder without fallback.",
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_limit_step,
    category="data",
)


def _handle_get_tushare_hot_rank(
    source: str = "ths",
    trade_date: str = "",
    data_type: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare THS/DC hot rank without fallback."""
    source_key = str(source or "ths").strip().lower()
    if source_key not in {"ths", "dc"}:
        return {"status": "failed", "api_name": "tushare_hot_rank", "items": [], "errors": [f"unsupported source: {source}"]}
    api_name = "dc_hot" if source_key == "dc" else "ths_hot"
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    requested_data_type = _clean_text(data_type)
    default_data_type = "A股市场" if api_name == "dc_hot" else "热股"
    target_data_type = requested_data_type or default_data_type
    fields = (
        "trade_date,data_type,ts_code,ts_name,rank,pct_change,current_price,"
        "hot,concept,rank_reason,rank_time"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        api_name,
        requested_date=requested_date,
        fields=fields,
        max_dates=5,
        lookback_days=30,
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": api_name,
            "trade_date": selected_date,
            "data_type": target_data_type,
            "stock_code": stock_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:{api_name} unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    latest_rank_time = ""
    for item in raw_items:
        item_data_type = _clean_text(item.get("data_type"))
        if target_data_type and item_data_type != target_data_type:
            continue
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        rank_time = _clean_text(item.get("rank_time"))
        if rank_time > latest_rank_time:
            latest_rank_time = rank_time

    by_symbol: Dict[str, Dict[str, Any]] = {}
    for item in raw_items:
        item_data_type = _clean_text(item.get("data_type"))
        if target_data_type and item_data_type != target_data_type:
            continue
        symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
        if target_symbol and symbol != target_symbol:
            continue
        if not symbol or not (_clean_text(item.get("ts_code")).upper().endswith((".SH", ".SZ", ".BJ"))):
            continue
        rank_time = _clean_text(item.get("rank_time"))
        rank = _safe_number(item.get("rank"))
        current = {
            "trade_date": str(item.get("trade_date") or selected_date),
            "data_type": item_data_type,
            "code": symbol,
            "ts_code": _clean_text(item.get("ts_code")),
            "name": _clean_text(item.get("ts_name")) or symbol,
            "rank": rank,
            "popularity": max(0.0, 101.0 - rank) if rank is not None else None,
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            "latest": _safe_number(item.get("current_price")),
            "current_price": _safe_number(item.get("current_price")),
            "hot": _safe_number(item.get("hot")),
            "concepts": _normalize_tushare_concepts(item.get("concept")),
            "reason": _clean_text(item.get("rank_reason")),
            "rank_time": rank_time,
            "source": f"tushare:{api_name}",
        }
        existing = by_symbol.get(symbol)
        if existing is None:
            by_symbol[symbol] = current
            continue
        current_rank = float(current.get("rank") or 10_000)
        existing_rank = float(existing.get("rank") or 10_000)
        if current_rank < existing_rank or (current_rank == existing_rank and rank_time > str(existing.get("rank_time") or "")):
            by_symbol[symbol] = current

    normalized_items = list(by_symbol.values())
    normalized_items.sort(
        key=lambda item: (
            -(float(item.get("rank") or 10_000.0)),
            float(item.get("hot") or 0.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": api_name,
        "trade_date": selected_date,
        "data_type": target_data_type,
        "rank_time": latest_rank_time,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_hot_rank_tool = ToolDefinition(
    name="get_tushare_hot_rank",
    description=(
        "Get Tushare THS or Eastmoney hot stock ranking without fallback. Defaults to "
        "THS 热股 or Eastmoney A股市场 and keeps the latest rank_time slice."
    ),
    parameters=[
        ToolParameter(name="source", type="string", description="ths or dc (default: ths).", required=False, default="ths"),
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="data_type", type="string", description="Optional data_type, e.g. 热股 or A股市场.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_hot_rank,
    category="data",
)


def _to_yuan_from_100m(value: Any) -> Optional[float]:
    number = _safe_number(value)
    return number * 100_000_000.0 if number is not None else None


def _handle_get_tushare_moneyflow_ind_ths(
    trade_date: str = "",
    ts_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare THS industry money-flow ranking without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_code = _clean_text(ts_code).upper()
    fields = (
        "trade_date,ts_code,industry,lead_stock,close,pct_change,company_num,"
        "pct_change_stock,close_price,net_buy_amount,net_sell_amount,net_amount"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "moneyflow_ind_ths",
        requested_date=requested_date,
        fields=fields,
        param_builder=lambda _date: {"ts_code": target_code},
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "moneyflow_ind_ths",
            "trade_date": selected_date,
            "ts_code": target_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:moneyflow_ind_ths unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        board_code = _clean_text(item.get("ts_code")).upper()
        if target_code and board_code != target_code:
            continue
        net_inflow = _to_yuan_from_100m(item.get("net_amount"))
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "ts_code": board_code,
            "name": _clean_text(item.get("industry")) or board_code,
            "industry": _clean_text(item.get("industry")),
            "lead_stock": _clean_text(item.get("lead_stock")),
            "close": _safe_number(item.get("close")),
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            "company_num": _safe_number(item.get("company_num")),
            "lead_stock_pct_change": _safe_number(item.get("pct_change_stock")),
            "lead_stock_price": _safe_number(item.get("close_price")),
            "net_buy_amount": _to_yuan_from_100m(item.get("net_buy_amount")),
            "net_sell_amount": _to_yuan_from_100m(item.get("net_sell_amount")),
            "net_inflow": net_inflow,
            "net_amount_billion": _safe_number(item.get("net_amount")),
            "source": "tushare:moneyflow_ind_ths",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("change_ratio") or 0.0),
            str(item.get("ts_code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "moneyflow_ind_ths",
        "trade_date": selected_date,
        "ts_code": target_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_moneyflow_ind_ths_tool = ToolDefinition(
    name="get_tushare_moneyflow_ind_ths",
    description=(
        "Get Tushare THS industry money-flow ranking without fallback. Amount fields from "
        "Tushare are converted from 100m yuan to yuan."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="ts_code", type="string", description="Optional THS board code, e.g. 881267.TI.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_moneyflow_ind_ths,
    category="data",
)


def _handle_get_tushare_moneyflow_ind_dc(
    trade_date: str = "",
    ts_code: str = "",
    content_type: str = "行业",
    limit: int = 30,
) -> dict:
    """Get Tushare Eastmoney industry/concept money-flow ranking without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_code = _clean_text(ts_code).upper()
    target_type = _clean_text(content_type)
    fields = (
        "trade_date,content_type,ts_code,name,pct_change,close,net_amount,net_amount_rate,"
        "buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
        "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate,"
        "buy_sm_amount_stock,rank"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "moneyflow_ind_dc",
        requested_date=requested_date,
        fields=fields,
        param_builder=lambda _date: {
            "ts_code": target_code,
            "content_type": target_type,
        },
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "moneyflow_ind_dc",
            "trade_date": selected_date,
            "ts_code": target_code,
            "content_type": target_type,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:moneyflow_ind_dc unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        board_code = _clean_text(item.get("ts_code")).upper()
        item_type = _clean_text(item.get("content_type"))
        if target_code and board_code != target_code:
            continue
        if target_type and item_type != target_type:
            continue
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "content_type": item_type,
            "ts_code": board_code,
            "name": _clean_text(item.get("name")) or board_code,
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            "close": _safe_number(item.get("close")),
            "net_inflow": _safe_number(item.get("net_amount")),
            "net_inflow_rate": _safe_number(item.get("net_amount_rate")),
            "extra_large_net_inflow": _safe_number(item.get("buy_elg_amount")),
            "extra_large_net_inflow_rate": _safe_number(item.get("buy_elg_amount_rate")),
            "large_net_inflow": _safe_number(item.get("buy_lg_amount")),
            "large_net_inflow_rate": _safe_number(item.get("buy_lg_amount_rate")),
            "medium_net_inflow": _safe_number(item.get("buy_md_amount")),
            "medium_net_inflow_rate": _safe_number(item.get("buy_md_amount_rate")),
            "small_net_inflow": _safe_number(item.get("buy_sm_amount")),
            "small_net_inflow_rate": _safe_number(item.get("buy_sm_amount_rate")),
            "top_net_inflow_stock": _clean_text(item.get("buy_sm_amount_stock")),
            "rank": _safe_number(item.get("rank")),
            "source": "tushare:moneyflow_ind_dc",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("net_inflow_rate") or 0.0),
            str(item.get("ts_code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "moneyflow_ind_dc",
        "trade_date": selected_date,
        "ts_code": target_code,
        "content_type": target_type,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_moneyflow_ind_dc_tool = ToolDefinition(
    name="get_tushare_moneyflow_ind_dc",
    description=(
        "Get Tushare Eastmoney industry/concept/region board money-flow ranking without fallback. "
        "Amount fields are exposed in yuan as returned by Tushare."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="ts_code", type="string", description="Optional DC board code.", required=False, default=""),
        ToolParameter(name="content_type", type="string", description="行业/概念/地域 (default: 行业).", required=False, default="行业"),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_moneyflow_ind_dc,
    category="data",
)


def _handle_get_tushare_moneyflow_cnt_ths(
    trade_date: str = "",
    ts_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare THS concept money-flow ranking without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 30), 200))
    target_code = _clean_text(ts_code).upper()
    fields = (
        "trade_date,ts_code,name,lead_stock,close_price,pct_change,industry_index,"
        "company_num,pct_change_stock,net_buy_amount,net_sell_amount,net_amount"
    )
    selected_date, result, source_chain, errors = _tushare_recent_date_query(
        "moneyflow_cnt_ths",
        requested_date=requested_date,
        fields=fields,
        param_builder=lambda _date: {"ts_code": target_code},
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "moneyflow_cnt_ths",
            "trade_date": selected_date,
            "ts_code": target_code,
            "items": [],
            "source_chain": source_chain,
            "errors": errors or [f"tushare:moneyflow_cnt_ths unavailable for {selected_date}"],
        }

    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items: List[Dict[str, Any]] = []
    for item in raw_items:
        board_code = _clean_text(item.get("ts_code")).upper()
        if target_code and board_code != target_code:
            continue
        normalized_items.append({
            "trade_date": str(item.get("trade_date") or selected_date),
            "ts_code": board_code,
            "name": _clean_text(item.get("name")) or board_code,
            "lead_stock": _clean_text(item.get("lead_stock")),
            "lead_stock_price": _safe_number(item.get("close_price")),
            "change_ratio": _safe_number(item.get("pct_change")),
            "pct_change": _safe_number(item.get("pct_change")),
            "industry_index": _safe_number(item.get("industry_index")),
            "company_num": _safe_number(item.get("company_num")),
            "lead_stock_pct_change": _safe_number(item.get("pct_change_stock")),
            "net_buy_amount": _to_yuan_from_100m(item.get("net_buy_amount")),
            "net_sell_amount": _to_yuan_from_100m(item.get("net_sell_amount")),
            "net_inflow": _to_yuan_from_100m(item.get("net_amount")),
            "net_amount_billion": _safe_number(item.get("net_amount")),
            "source": "tushare:moneyflow_cnt_ths",
        })
    normalized_items.sort(
        key=lambda item: (
            float(item.get("net_inflow") or 0.0),
            float(item.get("change_ratio") or 0.0),
            str(item.get("ts_code") or ""),
        ),
        reverse=True,
    )
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "moneyflow_cnt_ths",
        "trade_date": selected_date,
        "ts_code": target_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": source_chain,
        "errors": errors,
    }


get_tushare_moneyflow_cnt_ths_tool = ToolDefinition(
    name="get_tushare_moneyflow_cnt_ths",
    description=(
        "Get Tushare THS concept board money-flow ranking without fallback. Amount fields "
        "from Tushare are converted from 100m yuan to yuan."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="ts_code", type="string", description="Optional THS concept board code, e.g. 885748.TI.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_moneyflow_cnt_ths,
    category="data",
)


def _handle_get_tushare_ths_member(
    ts_code: str = "",
    stock_code: str = "",
    limit: int = 50,
) -> dict:
    """Get Tushare THS board members without fallback."""
    target_board = _clean_text(ts_code).upper()
    target_stock = _to_tushare_ts_code(stock_code) if stock_code else ""
    if not target_board and not target_stock:
        return {
            "status": "failed",
            "api_name": "ths_member",
            "ts_code": target_board,
            "stock_code": stock_code,
            "items": [],
            "source_chain": [],
            "errors": ["ths_member requires ts_code or stock_code"],
        }
    effective_limit = max(1, min(int(limit or 50), 200))
    result = _tushare_query_all_rows(
        "ths_member",
        {"ts_code": target_board, "con_code": target_stock},
        "ts_code,con_code,con_name,weight,in_date,out_date,is_new",
        timeout=_get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0),
    )
    if result.get("status") != "ok":
        return {
            "status": result.get("status") or "failed",
            "api_name": "ths_member",
            "ts_code": target_board,
            "stock_code": stock_code,
            "items": [],
            "source_chain": result.get("source_chain", []),
            "errors": result.get("errors") or [f"tushare:ths_member unavailable for {target_board or target_stock}"],
        }
    raw_items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    normalized_items = []
    for item in raw_items:
        con_code = _clean_text(item.get("con_code")).upper()
        if target_stock and con_code != target_stock:
            continue
        symbol = _normalize_ts_code_to_symbol(con_code)
        normalized_items.append({
            "ts_code": _clean_text(item.get("ts_code")).upper(),
            "code": symbol,
            "con_code": con_code,
            "name": _clean_text(item.get("con_name")) or symbol,
            "weight": _safe_number(item.get("weight")),
            "in_date": _clean_text(item.get("in_date")),
            "out_date": _clean_text(item.get("out_date")),
            "is_new": _clean_text(item.get("is_new")),
            "source": "tushare:ths_member",
        })
    items = normalized_items[:effective_limit]
    return {
        "status": "ok" if items else "empty",
        "api_name": "ths_member",
        "ts_code": target_board,
        "stock_code": stock_code,
        "items": items,
        "total_rows": int(result.get("total_rows") or len(raw_items)),
        "source_chain": result.get("source_chain", []),
        "errors": result.get("errors", []),
    }


get_tushare_ths_member_tool = ToolDefinition(
    name="get_tushare_ths_member",
    description="Get Tushare THS concept/industry board constituents without fallback.",
    parameters=[
        ToolParameter(name="ts_code", type="string", description="THS board index code, e.g. 885800.TI.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 50, max: 200).", required=False, default=50),
    ],
    handler=_handle_get_tushare_ths_member,
    category="data",
)


def _default_recent_date_range(days: int = 30) -> Tuple[str, str]:
    end = datetime.now().date()
    start = end - timedelta(days=max(1, int(days or 30)))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _today_news_window() -> Tuple[str, str]:
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_tushare_news_src(src: str) -> str:
    value = str(src or "").strip().lower()
    if value not in _TUSHARE_NEWS_SOURCES:
        raise ValueError(
            "unsupported Tushare news src: "
            f"{src!r}; expected one of {sorted(_TUSHARE_NEWS_SOURCES)}"
        )
    return value


def _compact_tushare_news_item(item: Dict[str, Any]) -> Dict[str, Any]:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    channels = item.get("channels")
    if isinstance(channels, str):
        channels_value: Any = channels.strip()
    else:
        channels_value = channels
    if len(title) > 160:
        title = title[:160] + "...[truncated]"
    if len(content) > 500:
        content = content[:500] + "...[truncated]"
    return {
        "datetime": item.get("datetime"),
        "title": title,
        "content": content,
        "channels": channels_value,
    }


def _handle_get_tushare_today_news(src: str = "sina", limit: int = 50) -> dict:
    """Get current-day Tushare flash news for one source.

    The tool intentionally does not expose arbitrary historical date windows:
    downstream agents use it as a same-day news snapshot, while Tushare token
    and endpoint resolution remain centralized in data_provider.tushare_client.
    """

    try:
        normalized_src = _normalize_tushare_news_src(src)
    except ValueError as exc:
        return {
            "status": "failed",
            "api_name": "news",
            "src": src,
            "items": [],
            "errors": [str(exc)],
        }

    row_limit = max(1, min(int(limit or 50), 1500))
    start_date, end_date = _today_news_window()
    result = _tushare_query_all_rows(
        "news",
        {
            "src": normalized_src,
            "start_date": start_date,
            "end_date": end_date,
        },
        "datetime,content,title,channels",
        timeout=max(5.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    result["src"] = normalized_src
    result["start_date"] = start_date
    result["end_date"] = end_date
    if isinstance(result.get("items"), list):
        result["items"] = [
            _compact_tushare_news_item(item)
            for item in result["items"][:row_limit]
            if isinstance(item, dict)
        ]
    result["limit"] = row_limit
    return result


get_tushare_today_news_tool = ToolDefinition(
    name="get_tushare_today_news",
    description=(
        "Get current-day flash news from Tushare Pro news API. "
        "The tool always queries today's 00:00:00 through now and never "
        "accepts historical windows."
    ),
    parameters=[
        ToolParameter(
            name="src",
            type="string",
            description=(
                "Tushare news source: sina, wallstreetcn, 10jqka, eastmoney, "
                "yuncaijing, fenghuang, jinrongjie, cls, or yicai."
            ),
            required=False,
            enum=sorted(_TUSHARE_NEWS_SOURCES),
            default="sina",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max current-day news rows to return (default: 50, max: 1500).",
            required=False,
            default=50,
        ),
    ],
    handler=_handle_get_tushare_today_news,
    category="data",
)


def _handle_get_tushare_announcements(
    stock_code: str = "",
    ann_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    start, end = _default_recent_date_range(7)
    result = _tushare_query(
        "anns_d",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "ann_date": _normalize_tushare_date(ann_date),
            "start_date": _normalize_tushare_date(start_date) or ("" if ann_date else start),
            "end_date": _normalize_tushare_date(end_date) or ("" if ann_date else end),
        },
        "ann_date,ts_code,name,title,url,rec_time",
        limit,
        timeout=max(5.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    result["stock_code"] = stock_code
    return result


get_tushare_announcements_tool = ToolDefinition(
    name="get_tushare_announcements",
    description="Get Tushare listed-company announcements (anns_d) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="ann_date", type="string", description="Optional announcement date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional announcement start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional announcement end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_announcements,
    category="data",
)


def _handle_get_tushare_stock_alerts(
    stock_code: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    start, end = _default_recent_date_range(30)
    result = _tushare_query(
        "stk_alert",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "trade_date": _normalize_tushare_date(trade_date),
            "start_date": _normalize_tushare_date(start_date) or ("" if trade_date else start),
            "end_date": _normalize_tushare_date(end_date) or ("" if trade_date else end),
        },
        "ts_code,name,start_date,end_date,type",
        limit,
        timeout=max(8.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    result["stock_code"] = stock_code
    return result


get_tushare_stock_alerts_tool = ToolDefinition(
    name="get_tushare_stock_alerts",
    description="Get Tushare exchange key-warning stock alerts (stk_alert) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_stock_alerts,
    category="data",
)


def _handle_get_tushare_stock_shock(
    stock_code: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    severity: str = "normal",
    limit: int = 30,
) -> dict:
    api_name = "stk_high_shock" if str(severity or "").strip().lower() in {"high", "serious", "severe"} else "stk_shock"
    start, end = _default_recent_date_range(30)
    result = _tushare_query(
        api_name,
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "trade_date": _normalize_tushare_date(trade_date),
            "start_date": _normalize_tushare_date(start_date) or ("" if trade_date else start),
            "end_date": _normalize_tushare_date(end_date) or ("" if trade_date else end),
        },
        "ts_code,trade_date,name,trade_market,reason,period",
        limit,
        timeout=max(8.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    result["stock_code"] = stock_code
    result["severity"] = severity
    return result


get_tushare_stock_shock_tool = ToolDefinition(
    name="get_tushare_stock_shock",
    description="Get Tushare abnormal or severe-abnormal stock movement records without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="severity", type="string", description="normal or high (default: normal).", required=False, default="normal"),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_stock_shock,
    category="data",
)


def _handle_get_tushare_pledge_stat(stock_code: str, end_date: str = "", limit: int = 30) -> dict:
    result = _tushare_query(
        "pledge_stat",
        {"ts_code": _to_tushare_ts_code(stock_code), "end_date": _normalize_tushare_date(end_date)},
        "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_pledge_stat_tool = ToolDefinition(
    name="get_tushare_pledge_stat",
    description="Get Tushare stock pledge summary (pledge_stat) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code, e.g. 000014.", required=True),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_pledge_stat,
    category="data",
)


def _handle_get_tushare_share_float(
    stock_code: str = "",
    ann_date: str = "",
    float_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    default_start, default_end = _default_recent_date_range(90)
    normalized_ann_date = _normalize_tushare_date(ann_date)
    normalized_float_date = _normalize_tushare_date(float_date)
    normalized_start_date = _normalize_tushare_date(start_date)
    normalized_end_date = _normalize_tushare_date(end_date)
    if not stock_code and not normalized_ann_date and not normalized_float_date and not normalized_start_date and not normalized_end_date:
        normalized_start_date = default_start
        normalized_end_date = default_end
    result = _tushare_query(
        "share_float",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "ann_date": normalized_ann_date,
            "float_date": normalized_float_date,
            "start_date": normalized_start_date,
            "end_date": normalized_end_date,
        },
        "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_share_float_tool = ToolDefinition(
    name="get_tushare_share_float",
    description="Get Tushare share-unlock records (share_float) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="ann_date", type="string", description="Optional announcement date.", required=False, default=""),
        ToolParameter(name="float_date", type="string", description="Optional unlock date.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_share_float,
    category="data",
)


def _handle_get_tushare_holder_trade(
    stock_code: str = "",
    ann_date: str = "",
    start_date: str = "",
    end_date: str = "",
    trade_type: str = "",
    holder_type: str = "",
    limit: int = 30,
) -> dict:
    default_start, default_end = _default_recent_date_range(180)
    normalized_ann_date = _normalize_tushare_date(ann_date)
    normalized_start_date = _normalize_tushare_date(start_date)
    normalized_end_date = _normalize_tushare_date(end_date)
    if not stock_code and not normalized_ann_date and not normalized_start_date and not normalized_end_date:
        normalized_start_date = default_start
        normalized_end_date = default_end
    result = _tushare_query(
        "stk_holdertrade",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "ann_date": normalized_ann_date,
            "start_date": normalized_start_date,
            "end_date": normalized_end_date,
            "trade_type": _clean_text(trade_type).upper(),
            "holder_type": _clean_text(holder_type).upper(),
        },
        "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share,begin_date,close_date",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_holder_trade_tool = ToolDefinition(
    name="get_tushare_holder_trade",
    description="Get Tushare stockholder increase/decrease records (stk_holdertrade) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="ann_date", type="string", description="Optional announcement date.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="trade_type", type="string", description="Optional trade type IN or DE.", required=False, default=""),
        ToolParameter(name="holder_type", type="string", description="Optional holder type C/P/G.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_holder_trade,
    category="data",
)


def _handle_get_tushare_repurchase(
    stock_code: str = "",
    ann_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    default_start, default_end = _default_recent_date_range(180)
    normalized_ann_date = _normalize_tushare_date(ann_date)
    normalized_start_date = _normalize_tushare_date(start_date)
    normalized_end_date = _normalize_tushare_date(end_date)
    if not stock_code and not normalized_ann_date and not normalized_start_date and not normalized_end_date:
        normalized_start_date = default_start
        normalized_end_date = default_end
    result = _tushare_query(
        "repurchase",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "ann_date": normalized_ann_date,
            "start_date": normalized_start_date,
            "end_date": normalized_end_date,
        },
        "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_repurchase_tool = ToolDefinition(
    name="get_tushare_repurchase",
    description="Get Tushare stock repurchase records (repurchase) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="ann_date", type="string", description="Optional announcement date.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_repurchase,
    category="data",
)


def _handle_get_tushare_pledge_detail(stock_code: str, limit: int = 30) -> dict:
    result = _tushare_query(
        "pledge_detail",
        {"ts_code": _to_tushare_ts_code(stock_code)},
        (
            "ts_code,ann_date,holder_name,pledge_amount,start_date,end_date,is_release,"
            "release_date,pledgor,holding_amount,pledged_amount,p_total_ratio,h_total_ratio,is_buyback"
        ),
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_pledge_detail_tool = ToolDefinition(
    name="get_tushare_pledge_detail",
    description="Get Tushare stock pledge-detail records without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code, e.g. 600519."),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_pledge_detail,
    category="data",
)


def _handle_get_tushare_daily_basic(
    stock_code: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    normalized_trade_date = _normalize_tushare_date(trade_date)
    normalized_start_date = _normalize_tushare_date(start_date)
    normalized_end_date = _normalize_tushare_date(end_date)
    if not stock_code and not normalized_trade_date and not normalized_start_date and not normalized_end_date:
        normalized_trade_date = _latest_tushare_trade_date(update_hour=19, update_minute=0)
    result = _tushare_query(
        "daily_basic",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "trade_date": normalized_trade_date,
            "start_date": normalized_start_date,
            "end_date": normalized_end_date,
        },
        (
            "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,"
            "pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
        ),
        limit,
        timeout=max(8.0, _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)),
    )
    result["stock_code"] = stock_code
    return result


get_tushare_daily_basic_tool = ToolDefinition(
    name="get_tushare_daily_basic",
    description="Get Tushare daily valuation/trading indicators (daily_basic) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="trade_date", type="string", description="Optional trade date YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_daily_basic,
    category="data",
)


def _handle_get_tushare_financial_indicators(stock_code: str, period: str = "", limit: int = 5) -> dict:
    result = _tushare_query(
        "fina_indicator",
        {"ts_code": _to_tushare_ts_code(stock_code), "period": _normalize_tushare_date(period)},
        (
            "ts_code,ann_date,end_date,eps,dt_eps,total_revenue_ps,revenue_ps,bps,ocfps,"
            "netprofit_margin,grossprofit_margin,roe,roe_waa,roe_dt,roa,roic,"
            "debt_to_assets,current_ratio,quick_ratio,assets_turn,inv_turn,ar_turn"
        ),
        limit,
    )
    result["stock_code"] = stock_code
    result["period"] = period
    return result


get_tushare_financial_indicators_tool = ToolDefinition(
    name="get_tushare_financial_indicators",
    description="Get Tushare financial indicator rows (fina_indicator) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code, e.g. 600519."),
        ToolParameter(name="period", type="string", description="Optional report period YYYYMMDD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 5, max: 200).", required=False, default=5),
    ],
    handler=_handle_get_tushare_financial_indicators,
    category="data",
)


def _handle_get_tushare_forecast(stock_code: str = "", period: str = "", limit: int = 30) -> dict:
    normalized_period = _normalize_tushare_date(period)
    if not stock_code:
        return {
            "status": "failed",
            "api_name": "forecast",
            "stock_code": stock_code,
            "period": period,
            "items": [],
            "source_chain": [],
            "errors": ["forecast requires stock_code for the current Tushare gateway"],
        }
    result = _tushare_query(
        "forecast",
        {"ts_code": _to_tushare_ts_code(stock_code), "period": normalized_period},
        "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,last_parent_net,first_ann_date,summary,change_reason",
        limit,
    )
    result["stock_code"] = stock_code
    result["period"] = period
    return result


get_tushare_forecast_tool = ToolDefinition(
    name="get_tushare_forecast",
    description="Get Tushare earnings forecast rows (forecast) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code.", required=True),
        ToolParameter(name="period", type="string", description="Optional report period YYYYMMDD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_forecast,
    category="data",
)


def _handle_get_tushare_express(stock_code: str = "", period: str = "", limit: int = 30) -> dict:
    normalized_period = _normalize_tushare_date(period)
    if not stock_code:
        return {
            "status": "failed",
            "api_name": "express",
            "stock_code": stock_code,
            "period": period,
            "items": [],
            "source_chain": [],
            "errors": ["express requires stock_code for the current Tushare gateway"],
        }
    result = _tushare_query(
        "express",
        {"ts_code": _to_tushare_ts_code(stock_code), "period": normalized_period},
        (
            "ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,total_assets,"
            "total_hldr_eqy_exc_min_int,diluted_eps,diluted_roe,yoy_net_profit,bps,yoy_sales,"
            "yoy_op,yoy_tp,yoy_dedu_np,yoy_eps,yoy_roe,growth_assets,yoy_equity,growth_bps,"
            "or_last_year,op_last_year,tp_last_year,np_last_year,eps_last_year,open_net_assets,"
            "open_bps,perf_summary,is_audit,remark"
        ),
        limit,
    )
    result["stock_code"] = stock_code
    result["period"] = period
    return result


get_tushare_express_tool = ToolDefinition(
    name="get_tushare_express",
    description="Get Tushare earnings express rows (express) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code.", required=True),
        ToolParameter(name="period", type="string", description="Optional report period YYYYMMDD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_express,
    category="data",
)


def _handle_get_tushare_dividend(stock_code: str = "", ann_date: str = "", record_date: str = "", limit: int = 30) -> dict:
    normalized_ann_date = _normalize_tushare_date(ann_date)
    normalized_record_date = _normalize_tushare_date(record_date)
    if not stock_code and not normalized_ann_date and not normalized_record_date:
        return {
            "status": "failed",
            "api_name": "dividend",
            "stock_code": stock_code,
            "items": [],
            "source_chain": [],
            "errors": ["dividend requires stock_code, ann_date, or record_date for the current Tushare gateway"],
        }
    result = _tushare_query(
        "dividend",
        {
            "ts_code": _to_tushare_ts_code(stock_code) if stock_code else "",
            "ann_date": normalized_ann_date,
            "record_date": normalized_record_date,
        },
        "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_dividend_tool = ToolDefinition(
    name="get_tushare_dividend",
    description="Get Tushare dividend and bonus-share records (dividend) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code.", required=False, default=""),
        ToolParameter(name="ann_date", type="string", description="Optional announcement date.", required=False, default=""),
        ToolParameter(name="record_date", type="string", description="Optional record date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_dividend,
    category="data",
)


def _handle_get_tushare_adj_factor(
    stock_code: str,
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    result = _tushare_query(
        "adj_factor",
        {
            "ts_code": _to_tushare_ts_code(stock_code),
            "trade_date": _normalize_tushare_date(trade_date),
            "start_date": _normalize_tushare_date(start_date),
            "end_date": _normalize_tushare_date(end_date),
        },
        "ts_code,trade_date,adj_factor",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_adj_factor_tool = ToolDefinition(
    name="get_tushare_adj_factor",
    description="Get Tushare adjustment factors (adj_factor) without fallback.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share stock code, e.g. 600519."),
        ToolParameter(name="trade_date", type="string", description="Optional trade date.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_adj_factor,
    category="data",
)


def _to_tushare_index_code(index_code: str) -> str:
    raw = str(index_code or "").strip().upper()
    if "." in raw:
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if digits.startswith(("399", "159")):
        return f"{digits}.SZ"
    return f"{digits}.SH"


def _handle_get_tushare_index_daily(
    index_code: str = "000300",
    ts_code: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    resolved_index_code = _clean_text(ts_code) or _clean_text(index_code) or "000300"
    result = _tushare_query(
        "index_daily",
        {
            "ts_code": _to_tushare_index_code(resolved_index_code),
            "trade_date": _normalize_tushare_date(trade_date),
            "start_date": _normalize_tushare_date(start_date),
            "end_date": _normalize_tushare_date(end_date),
        },
        "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount",
        limit,
    )
    result["index_code"] = resolved_index_code
    return result


get_tushare_index_daily_tool = ToolDefinition(
    name="get_tushare_index_daily",
    description="Get Tushare index daily bars (index_daily) without fallback.",
    parameters=[
        ToolParameter(name="index_code", type="string", description="Index code, e.g. 000300 or 000300.SH.", required=False, default="000300"),
        ToolParameter(name="ts_code", type="string", description="Optional Tushare index ts_code alias, e.g. 000300.SH.", required=False, default=""),
        ToolParameter(name="trade_date", type="string", description="Optional trade date.", required=False, default=""),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_index_daily,
    category="data",
)


def _handle_get_tushare_trade_calendar(
    exchange: str = "SSE",
    start_date: str = "",
    end_date: str = "",
    is_open: str = "",
    limit: int = 60,
) -> dict:
    start, end = _default_recent_date_range(30)
    result = _tushare_query(
        "trade_cal",
        {
            "exchange": _clean_text(exchange) or "SSE",
            "start_date": _normalize_tushare_date(start_date) or start,
            "end_date": _normalize_tushare_date(end_date) or end,
            "is_open": _clean_text(is_open),
        },
        "exchange,cal_date,is_open,pretrade_date",
        limit,
    )
    return result


get_tushare_trade_calendar_tool = ToolDefinition(
    name="get_tushare_trade_calendar",
    description="Get Tushare trading calendar rows (trade_cal) without fallback.",
    parameters=[
        ToolParameter(name="exchange", type="string", description="Exchange code, default SSE.", required=False, default="SSE"),
        ToolParameter(name="start_date", type="string", description="Optional start date.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="Optional end date.", required=False, default=""),
        ToolParameter(name="is_open", type="string", description="Optional open flag, 0 or 1.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 60, max: 200).", required=False, default=60),
    ],
    handler=_handle_get_tushare_trade_calendar,
    category="data",
)


def _handle_get_market_capital_flow(top_n: int = 5) -> dict:
    """Get market-level fund-flow rankings and broad money movement."""
    try:
        result = _get_fundamental_adapter().get_market_capital_flow(top_n=top_n)
    except Exception as exc:
        logger.warning("get_market_capital_flow failed: %s", exc)
        return {"status": "error", "error": f"market capital flow fetch failed: {exc}"}
    return result


get_market_capital_flow_tool = ToolDefinition(
    name="get_market_capital_flow",
    description=(
        "Get A-share market-level capital flow. Returns broad market fund-flow snapshot plus "
        "top/bottom individual-stock, industry, and concept fund-flow rankings. Useful for "
        "judging whether market liquidity supports entry or stock selection."
    ),
    parameters=[
        ToolParameter(
            name="top_n",
            type="integer",
            description="Number of top/bottom fund-flow ranking rows to return (default: 5, max: 20).",
            required=False,
            default=5,
        ),
    ],
    handler=_handle_get_market_capital_flow,
    category="data",
)


def _handle_get_northbound_capital_flow(limit: int = 10) -> dict:
    """Get northbound Stock Connect fund-flow summary and recent history."""
    try:
        result = _get_fundamental_adapter().get_northbound_capital_flow(limit=limit)
    except Exception as exc:
        logger.warning("get_northbound_capital_flow failed: %s", exc)
        return {"status": "error", "error": f"northbound capital flow fetch failed: {exc}"}
    return result


get_northbound_capital_flow_tool = ToolDefinition(
    name="get_northbound_capital_flow",
    description=(
        "Get northbound Stock Connect capital flow summary and recent history. Useful for "
        "checking foreign capital risk appetite toward A-shares and whether northbound flow "
        "confirms or weakens a market/sector signal."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Number of recent history rows to return (default: 10, max: 60).",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_get_northbound_capital_flow,
    category="data",
)


def _handle_get_tushare_moneyflow_hsgt(trade_date: str = "", limit: int = 10) -> dict:
    """Get Tushare Stock Connect aggregate money flow without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 10), 60))
    fields = "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money"
    if requested_date:
        result = _tushare_query(
            "moneyflow_hsgt",
            {"trade_date": requested_date},
            fields,
            limit=effective_limit,
        )
    else:
        end_date = _recent_tushare_trade_dates(
            update_hour=18,
            update_minute=0,
            max_dates=1,
            lookback_days=20,
        )[0]
        start_date = (
            datetime.strptime(end_date, "%Y%m%d") - timedelta(days=effective_limit * 3)
        ).strftime("%Y%m%d")
        result = _tushare_query(
            "moneyflow_hsgt",
            {"start_date": start_date, "end_date": end_date},
            fields,
            limit=effective_limit,
        )
    items: List[Dict[str, Any]] = []
    for row in result.get("items") or []:
        if not isinstance(row, dict):
            continue
        items.append({
            "trade_date": str(row.get("trade_date") or "").replace("-", "")[:8],
            "north_money": _safe_number(row.get("north_money")),
            "south_money": _safe_number(row.get("south_money")),
            "hgt": _safe_number(row.get("hgt")),
            "sgt": _safe_number(row.get("sgt")),
            "ggt_ss": _safe_number(row.get("ggt_ss")),
            "ggt_sz": _safe_number(row.get("ggt_sz")),
            "source": "tushare:moneyflow_hsgt",
        })
    items.sort(key=lambda item: str(item.get("trade_date") or ""), reverse=True)
    result.update({
        "api_name": "moneyflow_hsgt",
        "trade_date": requested_date,
        "items": items[:effective_limit],
    })
    return result


get_tushare_moneyflow_hsgt_tool = ToolDefinition(
    name="get_tushare_moneyflow_hsgt",
    description=(
        "Get Tushare Stock Connect aggregate money flow (moneyflow_hsgt) without fallback. "
        "Use it to judge northbound/southbound market liquidity context."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date, YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Recent rows to return when trade_date is omitted (default: 10, max: 60).", required=False, default=10),
    ],
    handler=_handle_get_tushare_moneyflow_hsgt,
    category="data",
)


def _handle_get_tushare_moneyflow_mkt_dc(trade_date: str = "", limit: int = 10) -> dict:
    """Get Tushare DC broad-market money flow without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    effective_limit = max(1, min(int(limit or 10), 60))
    fields = (
        "trade_date,close_sh,pct_change_sh,close_sz,pct_change_sz,"
        "net_amount,net_amount_rate,buy_elg_amount,buy_elg_amount_rate,"
        "buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,"
        "buy_sm_amount,buy_sm_amount_rate"
    )
    if requested_date:
        result = _tushare_query(
            "moneyflow_mkt_dc",
            {"trade_date": requested_date},
            fields,
            limit=effective_limit,
        )
    else:
        end_date = _recent_tushare_trade_dates(
            update_hour=18,
            update_minute=0,
            max_dates=1,
            lookback_days=20,
        )[0]
        start_date = (
            datetime.strptime(end_date, "%Y%m%d") - timedelta(days=effective_limit * 3)
        ).strftime("%Y%m%d")
        result = _tushare_query(
            "moneyflow_mkt_dc",
            {"start_date": start_date, "end_date": end_date},
            fields,
            limit=effective_limit,
        )
    items: List[Dict[str, Any]] = []
    for row in result.get("items") or []:
        if not isinstance(row, dict):
            continue
        items.append({
            "trade_date": str(row.get("trade_date") or "").replace("-", "")[:8],
            "close_sh": _safe_number(row.get("close_sh")),
            "pct_change_sh": _safe_number(row.get("pct_change_sh")),
            "close_sz": _safe_number(row.get("close_sz")),
            "pct_change_sz": _safe_number(row.get("pct_change_sz")),
            "net_amount": _safe_number(row.get("net_amount")),
            "net_amount_rate": _safe_number(row.get("net_amount_rate")),
            "extra_large_net_inflow": _safe_number(row.get("buy_elg_amount")),
            "extra_large_net_inflow_rate": _safe_number(row.get("buy_elg_amount_rate")),
            "large_net_inflow": _safe_number(row.get("buy_lg_amount")),
            "large_net_inflow_rate": _safe_number(row.get("buy_lg_amount_rate")),
            "medium_net_inflow": _safe_number(row.get("buy_md_amount")),
            "medium_net_inflow_rate": _safe_number(row.get("buy_md_amount_rate")),
            "small_net_inflow": _safe_number(row.get("buy_sm_amount")),
            "small_net_inflow_rate": _safe_number(row.get("buy_sm_amount_rate")),
            "source": "tushare:moneyflow_mkt_dc",
        })
    items.sort(key=lambda item: str(item.get("trade_date") or ""), reverse=True)
    result.update({
        "api_name": "moneyflow_mkt_dc",
        "trade_date": requested_date,
        "items": items[:effective_limit],
    })
    return result


get_tushare_moneyflow_mkt_dc_tool = ToolDefinition(
    name="get_tushare_moneyflow_mkt_dc",
    description=(
        "Get Tushare DC broad-market money flow (moneyflow_mkt_dc) without fallback. "
        "Use it to judge whether market-level main capital supports stock selection."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date, YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Recent rows to return when trade_date is omitted (default: 10, max: 60).", required=False, default=10),
    ],
    handler=_handle_get_tushare_moneyflow_mkt_dc,
    category="data",
)


def _handle_get_tushare_hsgt_top10(
    trade_date: str = "",
    stock_code: str = "",
    market_type: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare Stock Connect top traded stocks without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    trade_dates = [requested_date] if requested_date else _recent_tushare_trade_dates(
        update_hour=18,
        update_minute=0,
        max_dates=4,
        lookback_days=20,
    )
    effective_limit = max(1, min(int(limit or 30), 200))
    target_symbol = _normalize_ts_code_to_symbol(stock_code)
    requested_market = str(market_type or "").strip()
    market_types = [requested_market] if requested_market else ["1", "3"]
    fields = "trade_date,ts_code,name,close,change,rank,market_type,amount,net_amount,buy,sell"
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_status = "failed"

    for candidate_date in trade_dates:
        raw_items: List[Dict[str, Any]] = []
        for current_market_type in market_types:
            result = _tushare_query_all_rows(
                "hsgt_top10",
                {"trade_date": candidate_date, "market_type": current_market_type},
                fields,
            )
            source_chain.extend(result.get("source_chain", []))
            errors.extend(result.get("errors", []))
            last_status = str(result.get("status") or last_status)
            if result.get("status") == "ok":
                raw_items.extend([row for row in result.get("items") or [] if isinstance(row, dict)])
            elif requested_date and result.get("status") in {"failed", "timeout"}:
                break
        if not raw_items:
            if requested_date or last_status in {"failed", "timeout"}:
                break
            continue

        normalized_items: List[Dict[str, Any]] = []
        for item in raw_items:
            symbol = _normalize_ts_code_to_symbol(item.get("ts_code"))
            if target_symbol and symbol != target_symbol:
                continue
            amount = _safe_number(item.get("amount"))
            net_amount = _safe_number(item.get("net_amount"))
            buy = _safe_number(item.get("buy"))
            sell = _safe_number(item.get("sell"))
            normalized_items.append({
                "trade_date": str(item.get("trade_date") or candidate_date),
                "code": symbol,
                "ts_code": str(item.get("ts_code") or "").strip(),
                "name": str(item.get("name") or symbol).strip(),
                "close": _safe_number(item.get("close")),
                "change": _safe_number(item.get("change")),
                "rank": _safe_number(item.get("rank")),
                "market_type": str(item.get("market_type") or "").strip(),
                # Tushare hsgt_top10 amount fields are in 10k yuan; expose yuan.
                "amount": amount * 10000.0 if amount is not None else None,
                "net_amount": net_amount * 10000.0 if net_amount is not None else None,
                "buy": buy * 10000.0 if buy is not None else None,
                "sell": sell * 10000.0 if sell is not None else None,
                "source": "tushare:hsgt_top10",
            })
        normalized_items.sort(
            key=lambda item: (
                abs(float(item.get("net_amount") or 0.0)),
                float(item.get("amount") or 0.0),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        items = normalized_items[:effective_limit]
        return {
            "status": "ok" if items else "empty",
            "api_name": "hsgt_top10",
            "trade_date": candidate_date,
            "stock_code": stock_code,
            "market_type": market_type,
            "items": items,
            "total_rows": len(raw_items),
            "source_chain": source_chain,
            "errors": errors,
        }

    return {
        "status": last_status,
        "api_name": "hsgt_top10",
        "trade_date": trade_dates[0] if trade_dates else requested_date,
        "stock_code": stock_code,
        "market_type": market_type,
        "items": [],
        "source_chain": source_chain,
        "errors": errors or [f"tushare:hsgt_top10 unavailable for {trade_dates[0] if trade_dates else requested_date}"],
    }


get_tushare_hsgt_top10_tool = ToolDefinition(
    name="get_tushare_hsgt_top10",
    description=(
        "Get Tushare Stock Connect top traded stocks (hsgt_top10) without fallback. "
        "Use it as stock-level northbound/southbound seed evidence."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date, YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code to filter.", required=False, default=""),
        ToolParameter(name="market_type", type="string", description="Optional Tushare market_type, e.g. 1 for 沪股通, 3 for 深股通.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_hsgt_top10,
    category="data",
)


def _handle_get_margin_trading_summary(limit: int = 10) -> dict:
    """Get market-level margin financing and securities-lending summary."""
    effective_limit = max(1, min(int(limit or 10), 60))
    from datetime import timedelta

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=effective_limit * 3)).strftime("%Y%m%d")
    fields = "trade_date,exchange_id,rzye,rzmre,rqye,rqmcl,rqyl,rzrqye"
    timeout = _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)

    def _query_exchange(exchange_id: str) -> dict:
        first = _tushare_query(
            "margin",
            {"trade_date": end_date, "exchange_id": exchange_id},
            fields,
            effective_limit,
            timeout=timeout,
        )
        if first.get("status") != "empty":
            return first
        fallback = _tushare_query(
            "margin",
            {"start_date": start_date, "end_date": end_date, "exchange_id": exchange_id},
            fields,
            effective_limit,
            timeout=timeout,
        )
        fallback["source_chain"] = list(first.get("source_chain", [])) + list(fallback.get("source_chain", []))
        return fallback

    results: Dict[str, dict] = {}
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = {
            "sse": executor.submit(_query_exchange, "SSE"),
            "szse": executor.submit(_query_exchange, "SZSE"),
        }
        for key, future in futures.items():
            try:
                results[key] = future.result(timeout=timeout + 0.5)
            except FuturesTimeoutError:
                future.cancel()
                results[key] = {
                    "status": "timeout",
                    "api_name": "margin",
                    "items": [],
                    "source_chain": [{
                        "provider": "tushare:margin",
                        "result": "timeout",
                        "duration_ms": int(timeout * 1000),
                    }],
                    "errors": [f"tushare margin {key.upper()} timeout"],
                }
            except Exception as exc:
                results[key] = {
                    "status": "failed",
                    "api_name": "margin",
                    "items": [],
                    "source_chain": [{"provider": "tushare:margin", "result": "failed", "duration_ms": 0}],
                    "errors": [str(exc)],
                }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    sse = results.get("sse", {"status": "failed", "items": [], "source_chain": [], "errors": ["SSE result missing"]})
    szse = results.get("szse", {"status": "failed", "items": [], "source_chain": [], "errors": ["SZSE result missing"]})
    return {
        "status": "ok" if sse.get("status") == "ok" or szse.get("status") == "ok" else "failed",
        "sse": sse.get("items", []),
        "szse": szse.get("items", []),
        "exchange_status": {
            "sse": sse.get("status"),
            "szse": szse.get("status"),
        },
        "source_chain": list(sse.get("source_chain", [])) + list(szse.get("source_chain", [])),
        "errors": list(sse.get("errors", [])) + list(szse.get("errors", [])),
    }


get_margin_trading_summary_tool = ToolDefinition(
    name="get_margin_trading_summary",
    description=(
        "Get A-share margin financing / securities-lending summary. Useful for judging "
        "leveraged liquidity, risk appetite, and whether a market move is supported by "
        "margin financing or vulnerable to deleveraging."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Number of recent exchange summary rows to return (default: 10, max: 60).",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_get_margin_trading_summary,
    category="data",
)


def _handle_get_tushare_margin_detail(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare stock-level margin detail without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    trade_dates = [requested_date] if requested_date else _recent_tushare_trade_dates(
        update_hour=18,
        update_minute=0,
        max_dates=4,
        lookback_days=20,
    )
    effective_limit = max(1, min(int(limit or 30), 200))
    target_ts_code = _to_tushare_ts_code(stock_code) if stock_code else ""
    fields = "trade_date,ts_code,name,rzye,rqye,rzmre,rqyl,rzche,rqchl,rzrqye"
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_status = "failed"

    for candidate_date in trade_dates:
        result = _tushare_query_all_rows(
            "margin_detail",
            {"trade_date": candidate_date, "ts_code": target_ts_code},
            fields,
        )
        source_chain.extend(result.get("source_chain", []))
        errors.extend(result.get("errors", []))
        last_status = str(result.get("status") or last_status)
        if result.get("status") != "ok":
            if requested_date or result.get("status") in {"failed", "timeout"}:
                break
            continue

        items: List[Dict[str, Any]] = []
        for row in result.get("items") or []:
            if not isinstance(row, dict):
                continue
            symbol = _normalize_ts_code_to_symbol(row.get("ts_code"))
            items.append({
                "trade_date": str(row.get("trade_date") or candidate_date),
                "code": symbol,
                "ts_code": str(row.get("ts_code") or "").strip(),
                "name": str(row.get("name") or symbol).strip(),
                "financing_balance": _safe_number(row.get("rzye")),
                "short_balance": _safe_number(row.get("rqye")),
                "financing_buy": _safe_number(row.get("rzmre")),
                "short_volume": _safe_number(row.get("rqyl")),
                "financing_repay": _safe_number(row.get("rzche")),
                "short_repay_volume": _safe_number(row.get("rqchl")),
                "margin_balance": _safe_number(row.get("rzrqye")),
                "source": "tushare:margin_detail",
            })
        items.sort(
            key=lambda item: (
                float(item.get("financing_buy") or 0.0),
                float(item.get("financing_balance") or 0.0),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        items = items[:effective_limit]
        return {
            "status": "ok" if items else "empty",
            "api_name": "margin_detail",
            "trade_date": candidate_date,
            "stock_code": stock_code,
            "items": items,
            "total_rows": int(result.get("total_rows") or len(items)),
            "source_chain": source_chain,
            "errors": errors,
        }

    return {
        "status": last_status,
        "api_name": "margin_detail",
        "trade_date": trade_dates[0] if trade_dates else requested_date,
        "stock_code": stock_code,
        "items": [],
        "source_chain": source_chain,
        "errors": errors or [f"tushare:margin_detail unavailable for {trade_dates[0] if trade_dates else requested_date}"],
    }


get_tushare_margin_detail_tool = ToolDefinition(
    name="get_tushare_margin_detail",
    description=(
        "Get Tushare stock-level margin financing detail (margin_detail) without fallback. "
        "Use it for leveraged-liquidity seed discovery and risk checks."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date, YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code to filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_margin_detail,
    category="data",
)


def _handle_get_tushare_block_trade(
    trade_date: str = "",
    stock_code: str = "",
    limit: int = 30,
) -> dict:
    """Get Tushare block trades without fallback."""
    requested_date = _normalize_tushare_date(trade_date)
    trade_dates = [requested_date] if requested_date else _recent_tushare_trade_dates(
        update_hour=18,
        update_minute=0,
        max_dates=4,
        lookback_days=20,
    )
    effective_limit = max(1, min(int(limit or 30), 200))
    target_ts_code = _to_tushare_ts_code(stock_code) if stock_code else ""
    fields = "trade_date,ts_code,name,price,vol,amount,buyer,seller"
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    last_status = "failed"

    for candidate_date in trade_dates:
        result = _tushare_query_all_rows(
            "block_trade",
            {"trade_date": candidate_date, "ts_code": target_ts_code},
            fields,
        )
        source_chain.extend(result.get("source_chain", []))
        errors.extend(result.get("errors", []))
        last_status = str(result.get("status") or last_status)
        if result.get("status") != "ok":
            if requested_date or result.get("status") in {"failed", "timeout"}:
                break
            continue

        items: List[Dict[str, Any]] = []
        for row in result.get("items") or []:
            if not isinstance(row, dict):
                continue
            symbol = _normalize_ts_code_to_symbol(row.get("ts_code"))
            amount = _safe_number(row.get("amount"))
            items.append({
                "trade_date": str(row.get("trade_date") or candidate_date),
                "code": symbol,
                "ts_code": str(row.get("ts_code") or "").strip(),
                "name": str(row.get("name") or symbol).strip(),
                "price": _safe_number(row.get("price")),
                "volume": _safe_number(row.get("vol")),
                # Tushare block_trade amount is in 10k yuan; expose yuan.
                "amount": amount * 10000.0 if amount is not None else None,
                "buyer": str(row.get("buyer") or "").strip(),
                "seller": str(row.get("seller") or "").strip(),
                "source": "tushare:block_trade",
            })
        items.sort(
            key=lambda item: (
                float(item.get("amount") or 0.0),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        items = items[:effective_limit]
        return {
            "status": "ok" if items else "empty",
            "api_name": "block_trade",
            "trade_date": candidate_date,
            "stock_code": stock_code,
            "items": items,
            "total_rows": int(result.get("total_rows") or len(items)),
            "source_chain": source_chain,
            "errors": errors,
        }

    return {
        "status": last_status,
        "api_name": "block_trade",
        "trade_date": trade_dates[0] if trade_dates else requested_date,
        "stock_code": stock_code,
        "items": [],
        "source_chain": source_chain,
        "errors": errors or [f"tushare:block_trade unavailable for {trade_dates[0] if trade_dates else requested_date}"],
    }


get_tushare_block_trade_tool = ToolDefinition(
    name="get_tushare_block_trade",
    description=(
        "Get Tushare block-trade records (block_trade) without fallback. "
        "Use it to find high-conviction block transaction seed candidates."
    ),
    parameters=[
        ToolParameter(name="trade_date", type="string", description="Optional trade date, YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="stock_code", type="string", description="Optional A-share stock code to filter.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Rows to return (default: 30, max: 200).", required=False, default=30),
    ],
    handler=_handle_get_tushare_block_trade,
    category="data",
)


ALL_DATA_TOOLS.extend([
    get_tushare_moneyflow_ind_ths_tool,
    get_tushare_moneyflow_ind_dc_tool,
    get_tushare_moneyflow_cnt_ths_tool,
    get_tushare_ths_member_tool,
    get_tushare_today_news_tool,
    get_tushare_announcements_tool,
    get_tushare_stock_alerts_tool,
    get_tushare_stock_shock_tool,
    get_tushare_pledge_stat_tool,
    get_tushare_pledge_detail_tool,
    get_tushare_share_float_tool,
    get_tushare_holder_trade_tool,
    get_tushare_repurchase_tool,
    get_tushare_daily_basic_tool,
    get_tushare_financial_indicators_tool,
    get_tushare_forecast_tool,
    get_tushare_express_tool,
    get_tushare_dividend_tool,
    get_tushare_adj_factor_tool,
    get_tushare_index_daily_tool,
    get_tushare_trade_calendar_tool,
    get_tushare_moneyflow_ths_tool,
    get_tushare_moneyflow_dc_tool,
    get_tushare_dragon_tiger_list_tool,
    get_tushare_dragon_tiger_inst_tool,
    get_tushare_limit_list_ths_tool,
    get_tushare_limit_list_d_tool,
    get_tushare_limit_step_tool,
    get_tushare_hot_rank_tool,
    get_market_capital_flow_tool,
    get_northbound_capital_flow_tool,
    get_tushare_moneyflow_hsgt_tool,
    get_tushare_moneyflow_mkt_dc_tool,
    get_tushare_hsgt_top10_tool,
    get_margin_trading_summary_tool,
    get_tushare_margin_detail_tool,
    get_tushare_block_trade_tool,
])


# ============================================================
# StockAPI market microstructure / sentiment tools
# ============================================================

def _handle_get_stockapi_limit_up_pool(date: str = "", limit: int = 30) -> dict:
    """Get StockAPI A-share limit-up pool."""
    try:
        return _get_fundamental_adapter().get_stockapi_limit_up_pool(date=date or None, limit=limit)
    except Exception as exc:
        logger.warning("get_stockapi_limit_up_pool failed: %s", exc)
        return {"status": "error", "error": f"StockAPI limit-up pool fetch failed: {exc}"}


get_stockapi_limit_up_pool_tool = ToolDefinition(
    name="get_stockapi_limit_up_pool",
    description=(
        "Get A-share limit-up pool from StockAPI. Returns stock code/name, limit-up streak, "
        "sealing strength, turnover, concepts and reasons. Useful for discovering short-term "
        "hot-money candidates and market sentiment."
    ),
    parameters=[
        ToolParameter(
            name="date",
            type="string",
            description="Trade date in YYYY-MM-DD. Blank uses latest completed StockAPI daily date.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max rows to return (default: 30, max: 100).",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_stockapi_limit_up_pool,
    category="data",
)


def _handle_get_stockapi_hot_sectors(date: str = "", limit: int = 20) -> dict:
    """Get StockAPI hot sector rankings."""
    try:
        return _get_fundamental_adapter().get_stockapi_hot_sectors(date=date or None, limit=limit)
    except Exception as exc:
        logger.warning("get_stockapi_hot_sectors failed: %s", exc)
        return {"status": "error", "error": f"StockAPI hot sectors fetch failed: {exc}"}


get_stockapi_hot_sectors_tool = ToolDefinition(
    name="get_stockapi_hot_sectors",
    description=(
        "Get StockAPI hot sector/concept ranking with net inflow, strength and trend fields. "
        "Useful for capital-flow candidate discovery and sector confirmation."
    ),
    parameters=[
        ToolParameter(
            name="date",
            type="string",
            description="Trade date in YYYY-MM-DD. Blank uses latest completed StockAPI daily date.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max sectors to return (default: 20, max: 100).",
            required=False,
            default=20,
        ),
    ],
    handler=_handle_get_stockapi_hot_sectors,
    category="data",
)


def _handle_get_stockapi_sector_constituents(bk_code: str, page_no: int = 1, page_size: int = 50) -> dict:
    """Get StockAPI sector/concept constituents."""
    try:
        return _get_fundamental_adapter().get_stockapi_sector_constituents(
            bk_code=bk_code,
            page_no=page_no,
            page_size=page_size,
        )
    except Exception as exc:
        logger.warning("get_stockapi_sector_constituents failed: %s", exc)
        return {"status": "error", "error": f"StockAPI sector constituents fetch failed: {exc}"}


get_stockapi_sector_constituents_tool = ToolDefinition(
    name="get_stockapi_sector_constituents",
    description=(
        "Get StockAPI sector/concept constituents by bkCode, including per-stock main-force "
        "capital-flow fields. Useful for expanding a hot sector into stock candidates."
    ),
    parameters=[
        ToolParameter(
            name="bk_code",
            type="string",
            description="StockAPI sector/concept code, e.g. a bkCode returned by get_stockapi_hot_sectors.",
        ),
        ToolParameter(
            name="page_no",
            type="integer",
            description="Page number (default: 1).",
            required=False,
            default=1,
        ),
        ToolParameter(
            name="page_size",
            type="integer",
            description="Page size (default: 50, max: 100).",
            required=False,
            default=50,
        ),
    ],
    handler=_handle_get_stockapi_sector_constituents,
    category="data",
)


def _handle_get_stockapi_sector_flow_history(bk_code: str, limit: int = 10) -> dict:
    """Get StockAPI sector/concept historical fund flow."""
    try:
        return _get_fundamental_adapter().get_stockapi_sector_flow_history(bk_code=bk_code, limit=limit)
    except Exception as exc:
        logger.warning("get_stockapi_sector_flow_history failed: %s", exc)
        return {"status": "error", "error": f"StockAPI sector flow history fetch failed: {exc}"}


get_stockapi_sector_flow_history_tool = ToolDefinition(
    name="get_stockapi_sector_flow_history",
    description=(
        "Get StockAPI sector/concept historical capital flow by bkCode. Useful for checking "
        "whether sector money inflow is persistent instead of one-day noise."
    ),
    parameters=[
        ToolParameter(
            name="bk_code",
            type="string",
            description="StockAPI sector/concept code.",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Recent history rows to return (default: 10, max: 60).",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_get_stockapi_sector_flow_history,
    category="data",
)


def _handle_get_stockapi_popularity_rank(limit: int = 30) -> dict:
    """Get StockAPI popularity ranking."""
    try:
        return _get_fundamental_adapter().get_stockapi_popularity_rank(limit=limit)
    except Exception as exc:
        logger.warning("get_stockapi_popularity_rank failed: %s", exc)
        return {"status": "error", "error": f"StockAPI popularity rank fetch failed: {exc}"}


get_stockapi_popularity_rank_tool = ToolDefinition(
    name="get_stockapi_popularity_rank",
    description=(
        "Get StockAPI stock popularity ranking with AI-generated reasons/tags. Useful for "
        "sentiment and attention-flow candidate discovery."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Max rows to return (default: 30, max: 100).",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_stockapi_popularity_rank,
    category="data",
)


def _handle_get_stockapi_hot_money_activity(
    stock_code: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    """Get StockAPI hot-money rank or stock-level activity."""
    try:
        return _get_fundamental_adapter().get_stockapi_hot_money_activity(
            stock_code=stock_code or None,
            start_date=start_date or None,
            end_date=end_date or None,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("get_stockapi_hot_money_activity failed: %s", exc)
        return {"status": "error", "error": f"StockAPI hot-money activity fetch failed: {exc}"}


get_stockapi_hot_money_activity_tool = ToolDefinition(
    name="get_stockapi_hot_money_activity",
    description=(
        "Get StockAPI hot-money / dragon-tiger style activity. With stock_code it returns "
        "recent brokerage-seat activity for a stock; without stock_code it returns hot-money "
        "rankings. Useful for short-term capital confirmation."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Optional A-share stock code. Blank returns hot-money rank.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="start_date",
            type="string",
            description="Start date in YYYY-MM-DD. Blank defaults to 30 days ago.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="end_date",
            type="string",
            description="End date in YYYY-MM-DD. Blank defaults to today.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max rows to return (default: 30, max: 100).",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_stockapi_hot_money_activity,
    category="data",
)


ALL_DATA_TOOLS.extend([
    get_stockapi_limit_up_pool_tool,
    get_stockapi_hot_sectors_tool,
    get_stockapi_sector_constituents_tool,
    get_stockapi_sector_flow_history_tool,
    get_stockapi_popularity_rank_tool,
    get_stockapi_hot_money_activity_tool,
])


# ============================================================
# Tushare structured data tools
# ============================================================

def _handle_get_tushare_basic_data(asset_type: str = "stock", limit: int = 30) -> dict:
    kind = str(asset_type or "stock").strip().lower()
    if kind in {"stock", "a", "a_stock", "ashare"}:
        return _tushare_query(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            "ts_code,symbol,name,area,industry,market,list_date",
            limit,
        )
    if kind in {"fund", "etf"}:
        return _tushare_query(
            "fund_basic",
            {"market": "E"},
            "ts_code,name,management,custodian,fund_type,found_date,due_date,list_date,issue_amount,m_fee,c_fee",
            limit,
        )
    if kind in {"index"}:
        return _tushare_query("index_basic", {"market": "SSE"}, "ts_code,name,market,publisher,category,base_date,list_date", limit)
    if kind in {"hk", "hongkong"}:
        return _tushare_query("hk_basic", {}, "ts_code,name,fullname,enname,cn_spell,market,list_status,list_date", limit)
    if kind in {"us", "us_stock"}:
        return _tushare_query("us_basic", {}, "ts_code,name,enname,classify,list_date,delist_date", limit)
    if kind in {"future", "fut"}:
        return _tushare_query("fut_basic", {"exchange": "DCE"}, "ts_code,symbol,exchange,name,fut_code,multiplier,trade_unit", limit)
    if kind in {"option", "opt"}:
        return _tushare_query("opt_basic", {}, "ts_code,name,exchange,call_put,exercise_price,list_date,delist_date", limit)
    return {"status": "failed", "api_name": "tushare_basic_data", "items": [], "errors": [f"unsupported asset_type: {asset_type}"]}


get_tushare_basic_data_tool = ToolDefinition(
    name="get_tushare_basic_data",
    description=(
        "Get Tushare basic lists for A-shares, funds/ETFs, indices, HK/US stocks, futures or options. "
        "Use it to verify stock names/codes and build candidate universes."
    ),
    parameters=[
        ToolParameter(
            name="asset_type",
            type="string",
            description="stock/fund/index/hk/us/future/option (default: stock).",
            required=False,
            default="stock",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Max rows to return (default: 30, max: 200).",
            required=False,
            default=30,
        ),
    ],
    handler=_handle_get_tushare_basic_data,
    category="data",
)


def _handle_get_tushare_daily_bars(
    stock_code: str,
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    limit: int = 30,
) -> dict:
    api_name = {"daily": "daily", "week": "weekly", "weekly": "weekly", "month": "monthly", "monthly": "monthly"}.get(
        str(period or "daily").strip().lower(),
        "daily",
    )
    result = _tushare_query(
        api_name,
        {
            "ts_code": _to_tushare_ts_code(stock_code),
            "start_date": _normalize_tushare_date(start_date),
            "end_date": _normalize_tushare_date(end_date),
        },
        "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        limit,
    )
    result["stock_code"] = stock_code
    return result


get_tushare_daily_bars_tool = ToolDefinition(
    name="get_tushare_daily_bars",
    description="Get Tushare low-frequency OHLCV bars for A-shares: daily, weekly or monthly.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share code, e.g. 603418 or 603418.SH."),
        ToolParameter(name="period", type="string", description="daily/weekly/monthly (default: daily).", required=False, default="daily"),
        ToolParameter(name="start_date", type="string", description="YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Max rows to return (default: 30).", required=False, default=30),
    ],
    handler=_handle_get_tushare_daily_bars,
    category="data",
)


def _handle_get_tushare_financial_statements(stock_code: str, period: str = "", limit: int = 5) -> dict:
    ts_code = _to_tushare_ts_code(stock_code)
    params = {"ts_code": ts_code, "period": _normalize_tushare_date(period)}
    blocks = {
        "income": _tushare_query("income", params, "ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,revenue,total_cogs,operate_profit,total_profit,n_income", limit),
        "balancesheet": _tushare_query("balancesheet", params, "ts_code,ann_date,f_ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int,money_cap,accounts_receiv,inventories", limit),
        "cashflow": _tushare_query("cashflow", params, "ts_code,ann_date,f_ann_date,end_date,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,c_cash_equ_end_period", limit),
    }
    status = "ok" if any(block.get("status") == "ok" for block in blocks.values()) else "failed"
    return {
        "status": status,
        "stock_code": stock_code,
        "ts_code": ts_code,
        "period": period,
        "blocks": blocks,
        "source_chain": [chain for block in blocks.values() for chain in block.get("source_chain", [])],
        "errors": [err for block in blocks.values() for err in block.get("errors", [])],
    }


get_tushare_financial_statements_tool = ToolDefinition(
    name="get_tushare_financial_statements",
    description="Get Tushare income statement, balance sheet and cashflow summary for one stock.",
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share code, e.g. 603418."),
        ToolParameter(name="period", type="string", description="Report period YYYYMMDD, optional.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Rows per statement block (default: 5).", required=False, default=5),
    ],
    handler=_handle_get_tushare_financial_statements,
    category="data",
)


def _handle_get_tushare_reference_events(
    stock_code: str,
    event_type: str = "all",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
) -> dict:
    ts_code = _to_tushare_ts_code(stock_code)
    start = _normalize_tushare_date(start_date)
    end = _normalize_tushare_date(end_date)
    selected = str(event_type or "all").strip().lower()
    specs = {
        "dragon_tiger": ("top_list", {"start_date": start, "end_date": end}, "trade_date,ts_code,name,close,pct_change,amount,l_sell,l_buy,net_amount,reason"),
        "margin_detail": ("margin_detail", {"ts_code": ts_code, "start_date": start, "end_date": end}, "trade_date,ts_code,name,rzye,rqye,rzmre,rqyl,rzche,rqchl,rzrqye"),
        "unlock": ("share_float", {"ts_code": ts_code, "start_date": start, "end_date": end}, "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"),
        "holdertrade": ("stk_holdertrade", {"ts_code": ts_code, "start_date": start, "end_date": end}, "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share,begin_date,close_date"),
        "pledge": ("pledge_stat", {"ts_code": ts_code}, "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio"),
        "repurchase": ("repurchase", {"ts_code": ts_code, "start_date": start, "end_date": end}, "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit"),
        "st": ("namechange", {"ts_code": ts_code, "start_date": start, "end_date": end}, "ts_code,name,start_date,end_date,ann_date,change_reason"),
    }
    keys = list(specs) if selected == "all" else [selected]
    blocks: Dict[str, Any] = {}
    for key in keys:
        spec = specs.get(key)
        if not spec:
            blocks[key] = {"status": "failed", "items": [], "errors": [f"unsupported event_type: {key}"]}
            continue
        api_name, params, fields = spec
        blocks[key] = _tushare_query(api_name, params, fields, limit)
    status = "ok" if any(block.get("status") == "ok" for block in blocks.values()) else "failed"
    return {
        "status": status,
        "stock_code": stock_code,
        "ts_code": ts_code,
        "event_type": selected,
        "blocks": blocks,
        "source_chain": [chain for block in blocks.values() for chain in block.get("source_chain", [])],
        "errors": [err for block in blocks.values() for err in block.get("errors", [])],
    }


get_tushare_reference_events_tool = ToolDefinition(
    name="get_tushare_reference_events",
    description=(
        "Get Tushare reference/risk events for one A-share: dragon-tiger, margin detail, "
        "unlock, holder increase/decrease, pledge, repurchase and ST/name-change records."
    ),
    parameters=[
        ToolParameter(name="stock_code", type="string", description="A-share code, e.g. 603418."),
        ToolParameter(name="event_type", type="string", description="all/dragon_tiger/margin_detail/unlock/holdertrade/pledge/repurchase/st.", required=False, default="all"),
        ToolParameter(name="start_date", type="string", description="YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="end_date", type="string", description="YYYYMMDD or YYYY-MM-DD.", required=False, default=""),
        ToolParameter(name="limit", type="integer", description="Rows per event block (default: 20).", required=False, default=20),
    ],
    handler=_handle_get_tushare_reference_events,
    category="data",
)


ALL_DATA_TOOLS.extend([
    get_tushare_basic_data_tool,
    get_tushare_daily_bars_tool,
    get_tushare_financial_statements_tool,
    get_tushare_reference_events_tool,
])
