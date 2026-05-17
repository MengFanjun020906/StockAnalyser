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
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)

_fetcher_manager_singleton = None
_fetcher_manager_lock = Lock()
_DAILY_HISTORY_DEFAULT_DAYS = 60
_DAILY_HISTORY_MAX_DAYS = 365


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

    timeout = _get_agent_timeout_attr("agent_chip_distribution_timeout_seconds", 3.0)
    if hasattr(manager, "get_chip_distribution_context"):
        ctx, err, cost_ms = _run_manager_task_with_timeout(
            manager,
            lambda: manager.get_chip_distribution_context(stock_code),
            timeout,
            "chip_distribution",
        )
        if err or not isinstance(ctx, dict):
            return {
                "stock_code": stock_code,
                "status": "timeout" if err and "timeout" in str(err).lower() else "failed",
                "error_summary": str(err or "chip distribution unavailable"),
                "errors": [str(err or "chip distribution unavailable")],
                "source_chain": [{
                    "provider": "chip_distribution",
                    "result": "timeout" if err and "timeout" in str(err).lower() else "failed",
                    "duration_ms": cost_ms,
                }],
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
        chip, err, cost_ms = _run_manager_task_with_timeout(
            manager,
            lambda: manager.get_chip_distribution(stock_code),
            timeout,
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

def _handle_get_capital_flow(stock_code: str) -> dict:
    """Get main-force capital flow data for a stock."""
    manager = _get_fetcher_manager()
    try:
        try:
            from src.config import get_config
            timeout = float(getattr(get_config(), "agent_capital_flow_timeout_seconds", 3.0))
        except Exception:
            timeout = 3.0
        ctx = manager.get_capital_flow_context(stock_code, budget_seconds=timeout)
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
    if not any(value is not None for value in stock_flow.values()):
        tushare_flow = _query_tushare_stock_moneyflow(stock_code)
        if tushare_flow.get("status") == "ok":
            stock_flow = {
                "main_net_inflow": tushare_flow.get("main_net_inflow"),
                "inflow_5d": tushare_flow.get("inflow_5d"),
                "inflow_10d": tushare_flow.get("inflow_10d"),
                "latest_date": tushare_flow.get("latest_date"),
                "source_update": tushare_flow.get("source_update"),
            }
            status = "ok"
            source_chain.extend(tushare_flow.get("source_chain", []))
            errors = []
        else:
            source_chain.extend(tushare_flow.get("source_chain", []))
            errors = list(errors) + list(tushare_flow.get("errors", []))
    error_summary = None
    if errors:
        joined_errors = " | ".join(str(item) for item in errors if str(item).strip())
        if "stockapi_codeFlow" in joined_errors:
            if "empty_data" in joined_errors:
                error_summary = "StockAPI codeFlow returned no capital-flow rows for the queried window"
            else:
                error_summary = "StockAPI codeFlow capital-flow endpoint failed"
        elif "push2his.eastmoney.com" in joined_errors or "push2.eastmoney.com" in joined_errors:
            error_summary = "Eastmoney capital-flow endpoint unreachable"
        elif "RemoteDisconnected" in joined_errors or "remote end closed" in joined_errors.lower():
            error_summary = "Eastmoney capital-flow endpoint disconnected"
        elif "timeout" in joined_errors.lower():
            error_summary = "capital-flow endpoint timeout"
        else:
            error_summary = str(errors[0])

    return {
        "stock_code": stock_code,
        "status": status,
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


def _safe_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _latest_tushare_trade_date(update_hour: int = 16, update_minute: int = 0) -> str:
    now = datetime.now()
    cutoff = now.replace(hour=update_hour, minute=update_minute, second=0, microsecond=0)
    end_day = now.date() if now >= cutoff else (now.date() - timedelta(days=1))
    start_day = end_day - timedelta(days=30)
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
    for row in reversed(cal.get("items") or []):
        if str(row.get("is_open")) in {"1", "1.0", "True", "true"}:
            return str(row.get("cal_date") or "").replace("-", "")[:8]
    return end_day.strftime("%Y%m%d")


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
        df = query_tushare_api(api_name, params=effective_params, fields=fields, timeout=int(max(1, request_timeout)))
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
        }],
        "errors": [],
    }


def _query_tushare_chip_distribution(stock_code: str) -> dict:
    """Fast-path chip distribution through Tushare cyq_chips."""
    started_at = time.time()
    trade_date = _latest_tushare_trade_date(update_hour=19, update_minute=0)
    ts_code = _to_tushare_ts_code(stock_code)
    timeout = _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)
    chips = _tushare_query(
        "cyq_chips",
        {"ts_code": ts_code, "start_date": trade_date, "end_date": trade_date},
        "ts_code,trade_date,price,percent",
        limit=200,
        timeout=timeout,
    )
    if chips.get("status") != "ok":
        chips["stock_code"] = stock_code
        return chips
    daily = _tushare_query(
        "daily",
        {"ts_code": ts_code, "start_date": trade_date, "end_date": trade_date},
        "ts_code,trade_date,close",
        limit=1,
        timeout=timeout,
    )
    if daily.get("status") != "ok" or not daily.get("items"):
        return {
            "stock_code": stock_code,
            "status": "failed",
            "error_summary": "Tushare daily close is unavailable for chip metrics",
            "errors": ["tushare:daily unavailable for cyq_chips"],
            "source_chain": list(chips.get("source_chain", [])) + list(daily.get("source_chain", [])),
        }

    current_price = _safe_number(daily["items"][0].get("close"))
    rows = [
        (_safe_number(row.get("price")), _safe_number(row.get("percent")))
        for row in chips.get("items") or []
    ]
    rows = [(price, weight) for price, weight in rows if price is not None and weight is not None and weight > 0]
    if not rows or current_price is None:
        return {
            "stock_code": stock_code,
            "status": "failed",
            "error_summary": "Tushare cyq_chips returned unusable chip rows",
            "errors": ["tushare:cyq_chips unusable rows"],
            "source_chain": list(chips.get("source_chain", [])) + list(daily.get("source_chain", [])),
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
        "source_chain": list(chips.get("source_chain", [])) + list(daily.get("source_chain", [])) + [{
            "provider": "tushare:cyq_chips_metrics",
            "result": "ok",
            "duration_ms": int((time.time() - started_at) * 1000),
        }],
        "errors": [],
    }


def _query_tushare_stock_moneyflow(stock_code: str) -> dict:
    """Fallback stock capital flow through Tushare moneyflow."""
    ts_code = _to_tushare_ts_code(stock_code)
    latest = _latest_tushare_trade_date(update_hour=15, update_minute=30)
    start = (datetime.strptime(latest, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    result = _tushare_query(
        "moneyflow",
        {"ts_code": ts_code, "start_date": start, "end_date": latest},
        (
            "ts_code,trade_date,buy_lg_amount,sell_lg_amount,"
            "buy_elg_amount,sell_elg_amount,net_mf_amount"
        ),
        limit=20,
        timeout=_get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0),
    )
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


ALL_DATA_TOOLS.extend([
    get_market_capital_flow_tool,
    get_northbound_capital_flow_tool,
    get_margin_trading_summary_tool,
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
