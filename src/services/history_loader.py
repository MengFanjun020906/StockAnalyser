"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional, Tuple

import pandas as pd

from src.services.sequoia_price_rebaser import (
    OHLC_COLUMNS,
    rebase_ohlc_value_for_date,
    resolve_amount_volume_cash_price_rebase,
    resolve_sequoia_cash_price_rebase,
)

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30
_CACHE_STALE_GRACE_DAYS = 4

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)
def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    candidates: List[str] = []
    for candidate in (canonical_stock_code(raw_code), normalized_code):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, normalized_code


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


def _sequoia_symbol_variants(stock_code: str) -> List[str]:
    raw_code = str(stock_code or "").strip()
    candidates, normalized_code = _history_code_candidates(raw_code)
    variants: List[str] = []
    for candidate in [raw_code, normalized_code, *candidates]:
        text = str(candidate or "").strip()
        if not text:
            continue
        if text not in variants:
            variants.append(text)
        if "." in text:
            parts = [part for part in text.split(".") if part]
            for part in parts:
                if part.isdigit() and part not in variants:
                    variants.append(part)
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 6 and digits not in variants:
            variants.append(digits)
    return variants


def _load_sequoia_history_df(stock_code: str, start: date, end: date) -> Optional[pd.DataFrame]:
    db_path = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "vendor/Sequoia-X/data/sequoia_v2.db"
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return None
    variants = _sequoia_symbol_variants(stock_code)
    if not variants:
        return None
    placeholders = ",".join("?" * len(variants))
    params = [*variants, start.isoformat(), end.isoformat()]
    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT symbol, date, open, high, low, close, volume, turnover
                    FROM stock_daily
                    WHERE symbol IN ({placeholders})
                      AND date >= ?
                      AND date <= ?
                    ORDER BY date""",
                params,
            ).fetchall()
    except Exception as exc:
        logger.debug("load_history_df(%s): Sequoia DB read failed: %s", stock_code, exc)
        return None
    if not rows:
        return None
    frame = pd.DataFrame(
        [
            {
                "code": stock_code,
                "date": row["date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["turnover"],
                "source": f"sequoia_stock_daily:{row['symbol']}",
            }
            for row in rows
        ]
    )
    rebase = resolve_sequoia_cash_price_rebase(stock_code, frame.to_dict("records"))
    if rebase.adjusted:
        frame = _apply_ohlc_rebase(frame, rebase)
    return frame


def _apply_ohlc_rebase(frame: pd.DataFrame, rebase: Any) -> pd.DataFrame:
    if not getattr(rebase, "adjusted", False):
        return frame
    adjusted = frame.copy()
    for column in OHLC_COLUMNS:
        if column not in adjusted.columns:
            continue
        adjusted[column] = [
            rebase_ohlc_value_for_date(value, date_value, rebase)
            for value, date_value in zip(adjusted[column], adjusted["date"])
        ]
    adjusted["price_adjustment_factor"] = rebase.factor
    adjusted["price_adjustment_basis"] = rebase.basis
    return adjusted


def _normalize_db_cache_price_scale(frame: pd.DataFrame) -> pd.DataFrame:
    rebase = resolve_amount_volume_cash_price_rebase(
        frame.to_dict("records"),
        adjusted_basis="db_amount_volume_vwap",
        already_cash_basis="db_amount_volume_already_cash_scale",
        unstable_basis="db_amount_volume_unstable_ratio",
    )
    if rebase is None or not rebase.adjusted:
        return frame
    return _apply_ohlc_rebase(frame, rebase)


def _effective_cache_end(stock_code: str, end: date) -> date:
    """Return the latest daily-bar date that is acceptable for cache hits."""
    try:
        from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

        market = get_market_for_stock(str(stock_code or "")) or "cn"
        effective = get_effective_trading_date(
            market,
            current_time=datetime(end.year, end.month, end.day, 23, 59, 59),
        )
        return effective if isinstance(effective, date) else end
    except Exception as exc:
        logger.debug("load_history_df(%s): effective trading date fallback: %s", stock_code, exc)
        if end.weekday() == 5:
            return end - timedelta(days=1)
        if end.weekday() == 6:
            return end - timedelta(days=2)
        return end


def _cache_latest_is_fresh(
    latest_date: date,
    effective_end: date,
    natural_end: date,
    *,
    require_target_date: bool = False,
) -> bool:
    """Accept latest completed trading data, with a small holiday fail-open grace."""
    if latest_date == date.min:
        return False
    if require_target_date and latest_date < effective_end:
        return False
    if latest_date >= effective_end:
        return True
    stale_days = (natural_end - latest_date).days
    return 0 <= stale_days <= _CACHE_STALE_GRACE_DAYS and latest_date.weekday() < 5


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
    fallback_to_network: bool = True,
    minimum_records: Optional[int] = None,
    require_target_date: bool = False,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    ``minimum_records`` lets callers with a strict feature contract reject a
    short cache and continue to the provider fallback. The default preserves
    the historical cache behavior used by general analysis tools.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback. Returns ``(None, "db_cache_miss")``
    when network fallback is disabled and cache misses, or ``(None, "none")``
    when both paths fail.
    """
    from src.storage import get_db

    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else date.today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    # --- 1. DB lookup (canonical code, then prefix-stripped fallback) ------
    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, end)
        required_records = max(
            min(days, minimum_records if minimum_records is not None else _CACHE_MIN_RECORDS),
            1,
        )
        latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
        effective_end = _effective_cache_end(stock_code, end)
        if (
            bars
            and _cache_latest_is_fresh(
                latest_date,
                effective_end,
                end,
                require_target_date=require_target_date,
            )
            and len(bars) >= required_records
        ):
            df = _normalize_db_cache_price_scale(pd.DataFrame([b.to_dict() for b in bars]))
            logger.debug(
                "load_history_df(%s): %d bars from DB (requested %d, latest=%s, effective_end=%s)",
                stock_code, len(df), days, latest_date, effective_end,
            )
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # --- 1b. Local Sequoia stock_daily fallback ---------------------------
    try:
        required_records = max(
            min(days, minimum_records if minimum_records is not None else _CACHE_MIN_RECORDS),
            1,
        )
        df = _load_sequoia_history_df(stock_code, start, end)
        if df is not None and not df.empty:
            latest_date = max((_coerce_bar_date(value) for value in df["date"]), default=date.min)
            effective_end = _effective_cache_end(stock_code, end)
            if (
                _cache_latest_is_fresh(
                    latest_date,
                    effective_end,
                    end,
                    require_target_date=require_target_date,
                )
                and len(df) >= required_records
            ):
                logger.debug(
                    "load_history_df(%s): %d bars from Sequoia DB (requested %d, latest=%s, effective_end=%s)",
                    stock_code, len(df), days, latest_date, effective_end,
                )
                return df, "sequoia_stock_daily"
    except Exception as e:
        logger.debug("load_history_df(%s): Sequoia fallback failed: %s", stock_code, e)

    if not fallback_to_network:
        return None, "db_cache_miss"

    # --- 2. Network fallback via singleton DataFetcherManager -------------
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            return df, source
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"


def refresh_history_tail(
    stock_code: str,
    *,
    days: int = 520,
    target_date: Optional[date] = None,
    persist: bool = True,
) -> Tuple[Optional[pd.DataFrame], str, int]:
    """Fetch a recent provider window and optionally upsert it into ``stock_daily``.

    This helper intentionally fetches only the recent tail. Callers that render
    full history must keep the earliest local database row as their start point.
    """
    end = target_date or get_frozen_target_date() or date.today()
    manager = _get_fetcher_manager()
    try:
        frame, source = manager.get_daily_data(stock_code, days=max(1, int(days)))
    except Exception as exc:
        logger.warning("refresh_history_tail(%s): provider fetch failed: %s", stock_code, exc)
        return None, "none", 0
    if frame is None or frame.empty:
        return None, source or "none", 0

    normalized = frame.copy()
    if "date" not in normalized.columns:
        normalized = normalized.reset_index()
        if "date" not in normalized.columns:
            normalized = normalized.rename(columns={normalized.columns[0]: "date"})
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date"])
    normalized = normalized[normalized["date"].dt.date <= end]
    normalized = normalized.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    if normalized.empty or not persist:
        return normalized, source or "network", 0

    try:
        from data_provider.base import canonical_stock_code, normalize_stock_code
        from src.storage import get_db

        code = canonical_stock_code(normalize_stock_code(stock_code)) or str(stock_code).strip()
        saved = get_db().save_daily_data(normalized, code=code, data_source=source or "network")
        return normalized, source or "network", int(saved or 0)
    except Exception as exc:
        logger.warning("refresh_history_tail(%s): DB upsert failed: %s", stock_code, exc)
        return normalized, source or "network", 0
