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
from datetime import date, datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)

_fetcher_manager_singleton = None
_fetcher_manager_lock = Lock()
_DAILY_HISTORY_DEFAULT_DAYS = 60
_DAILY_HISTORY_MAX_DAYS = 365


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
    if hasattr(manager, "get_chip_distribution_context"):
        ctx = manager.get_chip_distribution_context(stock_code)
        chip = ctx.get("data")
        if chip is None:
            return {
                "stock_code": ctx.get("stock_code", stock_code),
                "status": ctx.get("status", "failed"),
                "error_summary": ctx.get("error_summary") or "chip distribution unavailable",
                "errors": ctx.get("errors", []),
                "source_chain": ctx.get("source_chain", []),
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
        chip = manager.get_chip_distribution(stock_code)

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
    belong_boards = manager.get_belong_boards(stock_code)

    stock_name = stock_code.upper()
    try:
        stock_name = manager.get_stock_name(stock_code) or stock_name
    except Exception:
        pass

    return {
        "code": stock_code.upper(),
        "name": stock_name,
        "pe_ratio": valuation.get("pe_ratio"),
        "pb_ratio": valuation.get("pb_ratio"),
        "total_mv": valuation.get("total_mv"),
        "circ_mv": valuation.get("circ_mv"),
        "fundamental_context": compact_context,
        "belong_boards": belong_boards,
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
    error_summary = None
    if errors:
        joined_errors = " | ".join(str(item) for item in errors if str(item).strip())
        if "push2his.eastmoney.com" in joined_errors or "push2.eastmoney.com" in joined_errors:
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
        "Returns today's net inflow, 5-day and 10-day cumulative inflows, "
        "and top sector-level capital flow rankings. "
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
    try:
        result = _get_fundamental_adapter().get_margin_trading_summary(limit=limit)
    except Exception as exc:
        logger.warning("get_margin_trading_summary failed: %s", exc)
        return {"status": "error", "error": f"margin trading summary fetch failed: {exc}"}
    return result


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
