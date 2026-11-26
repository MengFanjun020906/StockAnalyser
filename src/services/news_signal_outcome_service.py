# -*- coding: utf-8 -*-
"""Event-level settlement for news events (T+1 / T+5 excess return).

This module is data-only: it reads news events and local daily bars, computes
forward-adjusted returns against a benchmark, and writes ``news_signal_outcomes``.
It never changes selection, scoring, prompts or thresholds.

Conventions (documented in ``docs/architecture/news-event-foundation.md``):

* event day = ``news_extracted_events.trade_date``
* entry = next trading day open (T+1)
* 1-day exit = entry day close; 5-day exit = event day + 5 trading days close
* stock leg is forward-adjusted (``data/adj_factors.db``); benchmark is not
* ``excess_return_pct = stock_return_pct - benchmark_return_pct - cost_bps / 100``
* unavailable rows are persisted with a reason instead of being dropped
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select

from src.backtest.factor_eval.data import load_benchmark_series, normalize_benchmark_code
from src.services.news_event_foundation import (
    SCOPE_NO_STOCK,
    TradingCalendar,
    _coerce_date,
    load_trading_calendar,
    parse_codes_from_refs,
)
from src.storage import DatabaseManager, NewsExtractedEvent, NewsSignalOutcome, StockDaily

DATASET_VERSION = "news_event_outcome.v2"
DEFAULT_BENCHMARK = "000985.CSI"
STATUS_SETTLED = "settled"
STATUS_UNAVAILABLE = "unavailable"
REASON_EXIT_NOT_REACHED = "exit_not_reached"
REASON_ENTRY_NOT_REACHED = "entry_not_reached"
REASON_TRADE_DATE_MISSING = "trade_date_missing"
REASON_STOCK_BAR_MISSING = "stock_bar_missing"
REASON_ADJ_FACTOR_MISSING = "adj_factor_missing"
REASON_BENCHMARK_BAR_MISSING = "benchmark_bar_missing"
REASON_BENCHMARK_UNAVAILABLE = "benchmark_unavailable"
REASON_ENTRY_LIMIT_LOCKED = "entry_limit_locked"
REASON_CALENDAR_UNAVAILABLE = "calendar_unavailable"

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ADJ_DB = _ROOT / "data" / "adj_factors.db"


@dataclass
class SettlementSummary:
    events_scanned: int = 0
    event_stock_pairs: int = 0
    rows_written: int = 0
    settled: int = 0
    unavailable: int = 0
    skipped_existing: int = 0
    skipped_no_stock: int = 0
    unavailable_reasons: Dict[str, int] = None
    benchmark_code: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "events_scanned": self.events_scanned,
            "event_stock_pairs": self.event_stock_pairs,
            "rows_written": self.rows_written,
            "settled": self.settled,
            "unavailable": self.unavailable,
            "skipped_existing": self.skipped_existing,
            "skipped_no_stock": self.skipped_no_stock,
            "unavailable_reasons": self.unavailable_reasons or {},
            "dataset_version": DATASET_VERSION,
            "benchmark_code": self.benchmark_code,
        }


def parse_horizons(value: Any, default: Sequence[int] = (1, 5)) -> Tuple[int, ...]:
    if isinstance(value, (list, tuple, set)):
        items: Iterable[Any] = value
    else:
        items = str(value or "").replace(" ", ",").split(",")
    horizons: List[int] = []
    for item in items:
        try:
            horizon = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if horizon > 0 and horizon not in horizons:
            horizons.append(horizon)
    return tuple(sorted(horizons)) or tuple(default)


def _default_benchmark() -> str:
    try:
        from src.config import get_config

        value = str(getattr(get_config(), "news_signal_outcome_benchmark", "") or "").strip()
    except Exception:  # pragma: no cover - config is optional for this data job
        value = ""
    return value or DEFAULT_BENCHMARK


def adj_db_path(value: Optional[str] = None) -> Path:
    raw = str(value or os.getenv("NEWS_SIGNAL_OUTCOME_ADJ_DB", "") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else _ROOT / candidate
    return _DEFAULT_ADJ_DB


def _load_adj_factors(path: Path, codes: Sequence[str]) -> Dict[str, Dict[date, float]]:
    if not codes or not path.exists():
        return {}
    factors: Dict[str, Dict[date, float]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return {}
    try:
        for chunk in _chunks(list(codes), 500):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT code, date, adj_factor FROM stock_adj_factors WHERE code IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for code, day, factor in rows:
                parsed = _coerce_date(day)
                if parsed is None:
                    continue
                try:
                    factors.setdefault(str(code), {})[parsed] = float(factor)
                except (TypeError, ValueError):
                    continue
    except sqlite3.Error:
        return factors
    finally:
        conn.close()
    return factors


def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _factor_on_or_before(factors: Dict[date, float], target: date) -> Optional[float]:
    if not factors:
        return None
    if target in factors:
        return factors[target]
    candidates = [day for day in factors if day <= target]
    if not candidates:
        return None
    return factors[max(candidates)]


def _load_bars(
    session,
    codes: Sequence[str],
    start: date,
    end: date,
) -> Dict[str, Dict[date, Dict[str, float]]]:
    bars: Dict[str, Dict[date, Dict[str, float]]] = {}
    if not codes:
        return bars
    rows = session.execute(
        select(
            StockDaily.code,
            StockDaily.date,
            StockDaily.open,
            StockDaily.high,
            StockDaily.low,
            StockDaily.close,
        ).where(
            StockDaily.code.in_(list(codes)),
            StockDaily.date >= start,
            StockDaily.date <= end,
        )
    ).all()
    for code, day, open_, high, low, close in rows:
        parsed = _coerce_date(day)
        if parsed is None:
            continue
        bars.setdefault(str(code), {})[parsed] = {
            "open": _float(open_),
            "high": _float(high),
            "low": _float(low),
            "close": _float(close),
        }
    return bars


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_events(
    session,
    *,
    limit: Optional[int],
    event_ids: Optional[Sequence[str]],
) -> List[NewsExtractedEvent]:
    stmt = select(NewsExtractedEvent).order_by(NewsExtractedEvent.trade_date, NewsExtractedEvent.event_id)
    if event_ids:
        stmt = stmt.where(NewsExtractedEvent.event_id.in_(list(event_ids)))
    if limit:
        stmt = stmt.limit(int(limit))
    events = session.execute(stmt).scalars().all()
    return [event for event in events if event.stock_scope != SCOPE_NO_STOCK]


def _existing_outcomes(
    session, event_ids: Sequence[str], dataset_version: str
) -> Dict[Tuple[str, str, str], NewsSignalOutcome]:
    if not event_ids:
        return {}
    mapping: Dict[Tuple[str, str, str], NewsSignalOutcome] = {}
    for chunk in _chunks(list(event_ids), 400):
        rows = session.execute(
            select(NewsSignalOutcome).where(
                NewsSignalOutcome.event_id.in_(chunk),
                NewsSignalOutcome.dataset_version == dataset_version,
            )
        ).scalars().all()
        for row in rows:
            mapping[(str(row.event_id), str(row.stock_code), str(row.horizon))] = row
    return mapping


def _apply_outcome(
    row: NewsSignalOutcome,
    *,
    event: NewsExtractedEvent,
    stock_code: str,
    horizon: int,
    trade_date: Optional[date],
    entry_date: Optional[date],
    exit_date: Optional[date],
    status: str,
    reason: str = "",
    stock_return_pct: Optional[float] = None,
    benchmark_code: str = "",
    benchmark_return_pct: Optional[float] = None,
    excess_return_pct: Optional[float] = None,
    cost_bps: float = 0.0,
) -> None:
    row.card_id = str(event.card_id or f"event:{event.event_id}")[:96]
    row.seed_item_id = None
    row.evaluation_date = exit_date or trade_date or _coerce_date(event.signal_date) or date.today()
    row.alpha_return_pct = excess_return_pct
    row.mfe_pct = None
    row.mae_pct = None
    row.liquidity_status = "SETTLED" if status == STATUS_SETTLED else "UNKNOWN"
    row.data_status = status
    row.event_id = str(event.event_id)[:128]
    row.stock_code = stock_code[:16]
    row.stock_scope = str(event.stock_scope or "")[:16] or None
    row.trade_date = trade_date
    row.entry_date = entry_date
    row.exit_date = exit_date
    row.horizon = str(horizon)
    row.stock_return_pct = stock_return_pct
    row.benchmark_code = (benchmark_code or "")[:16] or None
    row.benchmark_return_pct = benchmark_return_pct
    row.excess_return_pct = excess_return_pct
    row.cost_bps = float(cost_bps)
    row.reason = reason[:120] or None
    row.dataset_version = DATASET_VERSION


def settle_news_event_outcomes(
    *,
    db: Optional[DatabaseManager] = None,
    adj_path: Optional[str] = None,
    horizons: Any = (1, 5),
    benchmark: Optional[str] = None,
    cost_bps: float = 20.0,
    limit: Optional[int] = None,
    recompute: bool = False,
    event_ids: Optional[Sequence[str]] = None,
    cache_dir: Optional[str] = None,
    benchmark_bars: Optional[Dict[date, Dict[str, float]]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Settle news events into ``news_signal_outcomes``.

    Rows already ``settled`` are skipped unless ``recompute=True``; rows marked
    ``unavailable`` are retried so later bar/adj data can complete them.  The
    benchmark always comes from the ``index_daily`` cache (qualified index code),
    never from ``stock_daily``.  ``dataset_version`` isolates the corrected v2
    rows from the legacy v1 rows.
    """

    manager = db or DatabaseManager.get_instance()
    horizon_values = parse_horizons(horizons)
    raw_benchmark = str(benchmark or "").strip() or _default_benchmark()
    benchmark_code = normalize_benchmark_code(raw_benchmark) or raw_benchmark
    cost_bps = float(cost_bps or 0.0)
    summary = SettlementSummary(unavailable_reasons={}, benchmark_code=benchmark_code)

    with manager.get_session() as session:
        calendar_dates = session.execute(select(StockDaily.date).distinct()).scalars().all()
        calendar = load_trading_calendar(calendar_dates)
        if not calendar.dates:
            summary.unavailable_reasons[REASON_CALENDAR_UNAVAILABLE] = 1
            return summary.as_dict()

        events = _load_events(session, limit=limit, event_ids=event_ids)
        summary.events_scanned = len(events)

        codes: List[str] = []
        event_codes: Dict[str, List[str]] = {}
        for event in events:
            parsed = parse_codes_from_refs(_json_list(event.stock_codes_json))
            event_codes[str(event.event_id)] = parsed
            if not parsed:
                summary.skipped_no_stock += 1
                continue
            codes.extend(parsed)

        unique_codes = sorted(set(codes))
        if not unique_codes:
            return summary.as_dict()

        trade_dates = [
            _coerce_date(event.trade_date) or _coerce_date(event.signal_date)
            for event in events
            if event_codes.get(str(event.event_id))
        ]
        valid_dates = [item for item in trade_dates if item]
        start = min(valid_dates) if valid_dates else calendar.dates[0]
        end = calendar.dates[-1]
        bars = _load_bars(session, unique_codes, start, end)
        if benchmark_bars is not None:
            bench_bars = {day: dict(bar) for day, bar in benchmark_bars.items()}
        else:
            bench_bars = load_benchmark_series(
                benchmark_code,
                start,
                end,
                cache_dir=cache_dir,
            ).bars
        factors = _load_adj_factors(adj_db_path(adj_path), unique_codes)
        calendar_index = {day: index for index, day in enumerate(calendar.dates)}

        existing = _existing_outcomes(
            session, [str(event.event_id) for event in events], DATASET_VERSION
        )
        for event in events:
            stock_codes = event_codes.get(str(event.event_id)) or []
            if not stock_codes:
                continue
            summary.event_stock_pairs += len(stock_codes)
            trade_date = _coerce_date(event.trade_date)
            for stock_code in stock_codes:
                for horizon in horizon_values:
                    key = (str(event.event_id), stock_code, str(horizon))
                    row = existing.get(key)
                    if row is not None and row.data_status == STATUS_SETTLED and not recompute:
                        summary.skipped_existing += 1
                        continue
                    if row is None:
                        row = NewsSignalOutcome(card_id=str(event.card_id or f"event:{event.event_id}")[:96])
                        session.add(row)
                        existing[key] = row

                    result = _settle_one(
                        event=event,
                        stock_code=stock_code,
                        horizon=horizon,
                        trade_date=trade_date,
                        calendar=calendar,
                        calendar_index=calendar_index,
                        bars=bars,
                        benchmark_bars=bench_bars,
                        factors=factors,
                        benchmark=benchmark_code,
                        cost_bps=cost_bps,
                    )
                    _apply_outcome(row, event=event, stock_code=stock_code, horizon=horizon, **result)
                    summary.rows_written += 1
                    if result["status"] == STATUS_SETTLED:
                        summary.settled += 1
                    else:
                        summary.unavailable += 1
                        reason = result.get("reason") or "unknown"
                        summary.unavailable_reasons[reason] = (
                            summary.unavailable_reasons.get(reason, 0) + 1
                        )
        if dry_run:
            session.rollback()
        else:
            session.commit()
    return summary.as_dict()


def _settle_one(
    *,
    event: NewsExtractedEvent,
    stock_code: str,
    horizon: int,
    trade_date: Optional[date],
    calendar: TradingCalendar,
    calendar_index: Dict[date, int],
    bars: Dict[str, Dict[date, Dict[str, float]]],
    benchmark_bars: Dict[date, Dict[str, float]],
    factors: Dict[str, Dict[date, float]],
    benchmark: str,
    cost_bps: float,
) -> Dict[str, Any]:
    base = {
        "status": STATUS_UNAVAILABLE,
        "trade_date": trade_date,
        "entry_date": None,
        "exit_date": None,
        "benchmark_code": benchmark,
        "cost_bps": cost_bps,
    }
    if trade_date is None:
        return {**base, "reason": REASON_TRADE_DATE_MISSING, "exit_date": _coerce_date(event.signal_date)}
    index = calendar_index.get(trade_date)
    if index is None:
        return {**base, "reason": REASON_TRADE_DATE_MISSING}

    entry_date = calendar.dates[index + 1] if index + 1 < len(calendar.dates) else None
    if entry_date is None:
        return {**base, "reason": REASON_ENTRY_NOT_REACHED}
    exit_index = index + horizon
    if exit_index >= len(calendar.dates):
        return {**base, "entry_date": entry_date, "reason": REASON_EXIT_NOT_REACHED}
    exit_date = calendar.dates[exit_index]

    stock_bars = bars.get(stock_code) or {}
    entry_bar = stock_bars.get(entry_date)
    exit_bar = stock_bars.get(exit_date)
    if not entry_bar or not exit_bar:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_STOCK_BAR_MISSING,
        }
    stock_open = _float(entry_bar.get("open"))
    stock_close = _float(exit_bar.get("close"))
    if not stock_open or stock_close is None:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_STOCK_BAR_MISSING,
        }
    high = _float(entry_bar.get("high"))
    low = _float(entry_bar.get("low"))
    if high is not None and low is not None and high == low and high > 0:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_ENTRY_LIMIT_LOCKED,
        }

    code_factors = factors.get(stock_code) or {}
    entry_factor = _factor_on_or_before(code_factors, entry_date)
    exit_factor = _factor_on_or_before(code_factors, exit_date)
    if not entry_factor or not exit_factor:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_ADJ_FACTOR_MISSING,
        }

    if not benchmark_bars:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_BENCHMARK_UNAVAILABLE,
        }
    benchmark_entry = benchmark_bars.get(entry_date)
    benchmark_exit = benchmark_bars.get(exit_date)
    if not benchmark_entry or not benchmark_exit:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_BENCHMARK_BAR_MISSING,
        }
    benchmark_open = _float(benchmark_entry.get("open"))
    benchmark_close = _float(benchmark_exit.get("close"))
    if not benchmark_open or benchmark_close is None:
        return {
            **base,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "reason": REASON_BENCHMARK_BAR_MISSING,
        }

    stock_return = (stock_close * exit_factor) / (stock_open * entry_factor) - 1.0
    benchmark_return = benchmark_close / benchmark_open - 1.0
    excess = stock_return - benchmark_return - cost_bps / 10000.0
    return {
        "status": STATUS_SETTLED,
        "reason": "",
        "trade_date": trade_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "stock_return_pct": round(stock_return * 100, 4),
        "benchmark_code": benchmark,
        "benchmark_return_pct": round(benchmark_return * 100, 4),
        "excess_return_pct": round(excess * 100, 4),
        "cost_bps": cost_bps,
    }


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
