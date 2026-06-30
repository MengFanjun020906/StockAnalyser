# -*- coding: utf-8 -*-
"""Baostock minute-bar sync for Agent entry-execution backtests."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from data_provider.baostock_fetcher import BaostockFetcher
from src.repositories.stock_repo import StockRepository
from src.services.agent_entry_execution_backtest_service import (
    DEFAULT_ENTRY_EXPIRY_DAYS,
    DEFAULT_MAX_HOLD_DAYS,
    DEFAULT_MINUTE_ADJUSTFLAG,
    DEFAULT_MINUTE_FREQUENCY,
    AgentEntryExecutionBacktestService,
)
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)


@dataclass
class EntryMinuteSyncResult:
    trace_count: int = 0
    plan_count: int = 0
    symbol_count: int = 0
    fetched_symbols: int = 0
    skipped_symbols: int = 0
    failed_symbols: int = 0
    fetched_rows: int = 0
    written_rows: int = 0
    frequency: str = DEFAULT_MINUTE_FREQUENCY
    adjustflag: str = DEFAULT_MINUTE_ADJUSTFLAG
    items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_count": self.trace_count,
            "plan_count": self.plan_count,
            "symbol_count": self.symbol_count,
            "fetched_symbols": self.fetched_symbols,
            "skipped_symbols": self.skipped_symbols,
            "failed_symbols": self.failed_symbols,
            "fetched_rows": self.fetched_rows,
            "written_rows": self.written_rows,
            "frequency": self.frequency,
            "adjustflag": self.adjustflag,
            "items": self.items,
        }


class AgentEntryMinuteDataService:
    """Sync baostock minute bars for stocks in final Agent reports."""

    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        stock_repo: Optional[StockRepository] = None,
        backtest_service: Optional[AgentEntryExecutionBacktestService] = None,
        baostock_fetcher: Optional[BaostockFetcher] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.stock_repo = stock_repo or StockRepository(self.db)
        self.backtest_service = backtest_service or AgentEntryExecutionBacktestService(
            db_manager=self.db,
            stock_repo=self.stock_repo,
        )
        self.baostock_fetcher = baostock_fetcher or BaostockFetcher()

    def sync_for_latest_reports(
        self,
        *,
        trace_root: Optional[Path] = None,
        limit: Optional[int] = 300,
        decision_date: Optional[date] = None,
        symbol: Optional[str] = None,
        frequency: str = DEFAULT_MINUTE_FREQUENCY,
        adjustflag: str = DEFAULT_MINUTE_ADJUSTFLAG,
        current_date: Optional[date] = None,
    ) -> EntryMinuteSyncResult:
        """Fetch and persist minute bars for final-report stocks only."""
        normalized_frequency = _normalize_frequency(frequency)
        normalized_adjustflag = str(adjustflag or DEFAULT_MINUTE_ADJUSTFLAG)
        plans = self.backtest_service.collect_trade_plans(trace_root=trace_root, limit=limit)
        normalized_symbol = _normalize_symbol(symbol)
        if decision_date is not None or normalized_symbol:
            plans = [
                item for item in plans
                if _plan_matches_filter(item, decision_date=decision_date, symbol=normalized_symbol)
            ]
        trace_ids = {str(item.get("trace_id") or "") for item in plans if item.get("trace_id")}
        ranges = _build_symbol_ranges(plans, current_date=current_date or date.today())
        result = EntryMinuteSyncResult(
            trace_count=len(trace_ids),
            plan_count=len(plans),
            symbol_count=len(ranges),
            frequency=normalized_frequency,
            adjustflag=normalized_adjustflag,
        )

        for symbol, sync_range in sorted(ranges.items()):
            if not _is_supported_baostock_stock(symbol):
                result.skipped_symbols += 1
                result.items.append({
                    "symbol": symbol,
                    "status": "skipped_unsupported_symbol",
                    "reason": "baostock minute bars support沪深A股，不支持港股/美股/北交所/指数",
                })
                continue

            start_text = sync_range["start_date"].isoformat()
            end_text = sync_range["end_date"].isoformat()
            try:
                df = self.baostock_fetcher.get_minute_k_data(
                    symbol,
                    start_text,
                    end_text,
                    frequency=normalized_frequency,
                    adjustflag=normalized_adjustflag,
                )
                records = _records_from_baostock_df(
                    df,
                    fallback_symbol=symbol,
                    frequency=normalized_frequency,
                    adjustflag=normalized_adjustflag,
                )
                written = self.stock_repo.save_minute_bars(records, data_source="BaostockMinute")
                coverage = self.stock_repo.get_minute_coverage(
                    code=symbol,
                    start_date=sync_range["start_date"],
                    end_date=sync_range["end_date"],
                    frequency=normalized_frequency,
                    adjustflag=normalized_adjustflag,
                )
                result.fetched_symbols += 1
                result.fetched_rows += len(records)
                result.written_rows += written
                result.items.append({
                    "symbol": symbol,
                    "status": "ok" if records else "empty",
                    "start_date": start_text,
                    "end_date": end_text,
                    "fetched_rows": len(records),
                    "written_rows": written,
                    "coverage": coverage,
                    "trace_ids": sorted(sync_range["trace_ids"]),
                })
            except Exception as exc:
                logger.warning("同步 Agent 入场分钟线失败 %s: %s", symbol, exc)
                result.failed_symbols += 1
                result.items.append({
                    "symbol": symbol,
                    "status": "failed",
                    "start_date": start_text,
                    "end_date": end_text,
                    "error": str(exc),
                    "trace_ids": sorted(sync_range["trace_ids"]),
                })

        return result


def _build_symbol_ranges(plans: List[Dict[str, Any]], *, current_date: date) -> Dict[str, Dict[str, Any]]:
    ranges: Dict[str, Dict[str, Any]] = {}
    for item in plans:
        plan = item.get("trade_plan") if isinstance(item.get("trade_plan"), dict) else {}
        if plan.get("parse_status") != "ok":
            continue
        if plan.get("entry_zone_low") is None and plan.get("entry_zone_high") is None:
            continue
        symbol = _normalize_symbol(plan.get("ts_code") or item.get("ts_code"))
        decision_date = _parse_date(plan.get("decision_date") or item.get("decision_date"))
        if not symbol or decision_date is None:
            continue
        entry_expiry = int(plan.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS)
        max_hold = int(plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)
        horizon_days = max(30, (entry_expiry + max_hold) * 2)
        end_date = min(current_date, decision_date + timedelta(days=horizon_days))
        if end_date < decision_date:
            end_date = decision_date

        existing = ranges.get(symbol)
        if existing is None:
            ranges[symbol] = {
                "start_date": decision_date,
                "end_date": end_date,
                "trace_ids": {str(item.get("trace_id") or "")},
            }
            continue
        existing["start_date"] = min(existing["start_date"], decision_date)
        existing["end_date"] = max(existing["end_date"], end_date)
        existing["trace_ids"].add(str(item.get("trace_id") or ""))
    return ranges


def _plan_matches_filter(item: Dict[str, Any], *, decision_date: Optional[date], symbol: str) -> bool:
    plan = item.get("trade_plan") if isinstance(item.get("trade_plan"), dict) else {}
    if decision_date is not None:
        item_date = _parse_date(plan.get("decision_date") or item.get("decision_date"))
        if item_date != decision_date:
            return False
    if symbol and _normalize_symbol(plan.get("ts_code") or item.get("ts_code")) != symbol:
        return False
    return True


def _records_from_baostock_df(
    df: pd.DataFrame,
    *,
    fallback_symbol: str,
    frequency: str,
    adjustflag: str,
) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    records: List[Dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        bar_dt = _parse_baostock_time(row.get("time"), row.get("date"))
        if bar_dt is None:
            continue
        baostock_code = str(row.get("code") or "").strip().lower()
        code = _normalize_symbol(baostock_code) or fallback_symbol
        records.append({
            "code": code,
            "baostock_code": baostock_code or None,
            "frequency": frequency,
            "adjustflag": str(row.get("adjustflag") or adjustflag),
            "bar_datetime": bar_dt,
            "bar_time": bar_dt.strftime("%H:%M:%S"),
            "open": _to_float(row.get("open")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "close": _to_float(row.get("close")),
            "volume": _to_float(row.get("volume")),
            "amount": _to_float(row.get("amount")),
        })
    return records


def _parse_baostock_time(raw_time: Any, raw_date: Any) -> Optional[datetime]:
    text = str(raw_time or "").strip()
    if text:
        for fmt in ("%Y%m%d%H%M%S%f", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    parsed_date = _parse_date(raw_date)
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, datetime.min.time())


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.startswith(("SH.", "SZ.")):
        return text.split(".", 1)[1]
    if "." in text:
        head, tail = text.split(".", 1)
        if head in {"SH", "SZ"}:
            return tail
        return head
    return text


def _is_supported_baostock_stock(symbol: str) -> bool:
    code = _normalize_symbol(symbol)
    if not re.match(r"^\d{6}$", code):
        return False
    if code.startswith(("4", "8")):
        return False
    return code.startswith(("0", "1", "2", "3", "5", "6", "9"))


def _normalize_frequency(value: Any) -> str:
    text = str(value or DEFAULT_MINUTE_FREQUENCY)
    if text not in {"5", "15", "30", "60"}:
        raise ValueError("baostock minute frequency only supports 5/15/30/60")
    return text


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["AgentEntryMinuteDataService", "EntryMinuteSyncResult"]
