# -*- coding: utf-8 -*-
"""Decision outcome records (MEM-01a).

Records actionable decisions as pending rows and settles them at T+5 close
using local ``stock_daily`` bars only.  This module never participates in
selection, ranking, position sizing or gating; it is a write-only data
accumulation path for later memory/score work.
"""

from __future__ import annotations

import bisect
import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

from src.storage import DatabaseManager, DecisionOutcomeRecord, StockDaily

logger = logging.getLogger(__name__)

HORIZON_DAYS = 5
COST_BPS = 20.0
ENTRY_GRACE_SESSIONS = 3
EXIT_GRACE_SESSIONS = 10
RECORDABLE_EXECUTION_MODES = frozenset({"immediate_open", "conditional_open"})

STATUS_PENDING = "pending"
STATUS_SETTLED = "settled"
STATUS_SKIPPED_MISSING_ENTRY = "skipped_missing_entry"
STATUS_SKIPPED_UNFILLABLE = "skipped_unfillable"
STATUS_EXPIRED_MISSING_EXIT = "expired_missing_exit"
STATUS_DELISTED_EXIT = "delisted_exit"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_SETTLED,
        STATUS_SKIPPED_MISSING_ENTRY,
        STATUS_SKIPPED_UNFILLABLE,
        STATUS_EXPIRED_MISSING_EXIT,
        STATUS_DELISTED_EXIT,
    }
)


def _db():
    return DatabaseManager.get_instance()


def _normalize_code(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if not text:
        return None
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text.startswith(("SH", "SZ", "BJ")) and text[2:].isdigit():
        text = text[2:]
    return text or None


def _limit_pct(code: str) -> float:
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def record_pending_records(
    *,
    run_id: str,
    decision_as_of: date,
    items: Iterable[Dict[str, Any]],
    market: str = "cn",
    enabled: bool = True,
    db: Any = None,
) -> Dict[str, Any]:
    """Insert pending outcome rows for actionable recommendations.

    Idempotent on ``(run_id, code)``: pending rows only refresh reference fields,
    terminal rows are never overwritten.
    """

    if not enabled:
        return {"enabled": False, "inserted": 0, "updated": 0, "skipped": 0, "skipped_actions": 0}
    run_key = str(run_id or "").strip()
    if not run_key:
        return {"enabled": True, "inserted": 0, "updated": 0, "skipped": 0, "error": "missing_run_id"}

    inserted = updated = skipped = skipped_actions = 0
    manager = db or _db()
    with manager.get_session() as session:
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            code = _normalize_code(raw.get("code"))
            if not code:
                continue
            mode = str(raw.get("execution_mode") or "").strip().lower()
            if mode not in RECORDABLE_EXECUTION_MODES:
                skipped_actions += 1
                continue
            existing = session.execute(
                select(DecisionOutcomeRecord).where(
                    DecisionOutcomeRecord.run_id == run_key,
                    DecisionOutcomeRecord.code == code,
                )
            ).scalars().first()
            zone_text = str(raw.get("ideal_entry_zone") or raw.get("entry_condition") or "").strip() or None
            if existing is None:
                session.add(
                    DecisionOutcomeRecord(
                        run_id=run_key,
                        code=code,
                        market=str(raw.get("market") or market or "cn")[:12],
                        decision_as_of=decision_as_of,
                        action=str(raw.get("action") or "")[:32] or None,
                        execution_mode=mode[:32],
                        plan_mode=str(raw.get("plan_mode") or "")[:32] or None,
                        plan_id=str(raw.get("plan_id") or "")[:128] or None,
                        entry_zone_text=zone_text,
                        horizon_days=HORIZON_DAYS,
                        cost_bps=COST_BPS,
                        status=STATUS_PENDING,
                    )
                )
                inserted += 1
            elif existing.status == STATUS_PENDING:
                existing.action = str(raw.get("action") or "")[:32] or existing.action
                existing.execution_mode = mode[:32]
                existing.plan_mode = str(raw.get("plan_mode") or "")[:32] or existing.plan_mode
                existing.plan_id = str(raw.get("plan_id") or "")[:128] or existing.plan_id
                existing.entry_zone_text = zone_text or existing.entry_zone_text
                updated += 1
            else:
                skipped += 1
        session.commit()
    return {
        "enabled": True,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "skipped_actions": skipped_actions,
    }


def _trading_calendar(session: Any) -> List[date]:
    rows = session.execute(select(StockDaily.date).distinct().order_by(StockDaily.date)).scalars().all()
    return [row for row in rows if isinstance(row, date)]


def _bars_for(session: Any, code: str, start: date) -> List[Dict[str, Any]]:
    rows = (
        session.execute(
            select(
                StockDaily.date,
                StockDaily.open,
                StockDaily.high,
                StockDaily.low,
                StockDaily.close,
                StockDaily.volume,
                StockDaily.pct_chg,
            )
            .where(StockDaily.code == code, StockDaily.date >= start)
            .order_by(StockDaily.date)
        )
        .all()
    )
    return [
        {
            "date": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "pct_chg": row[6],
        }
        for row in rows
    ]


def _bar_on(bars: Sequence[Dict[str, Any]], day: date) -> Optional[Dict[str, Any]]:
    for bar in bars:
        if bar["date"] == day:
            return bar
        if bar["date"] > day:
            return None
    return None


def _is_limit_locked(bar: Dict[str, Any], code: str) -> bool:
    # 涨跌停口径仅适用于 6 位 A 股代码；港股/美股不做一字板判定。
    if not (len(code) == 6 and code.isdigit()):
        return False
    open_px, high, low = bar.get("open"), bar.get("high"), bar.get("low")
    pct = bar.get("pct_chg")
    if None in (open_px, high, low) or open_px <= 0:
        return False
    one_word = abs(float(high) - float(low)) < 1e-6 and abs(float(open_px) - float(high)) < 1e-6
    if not one_word:
        return False
    return pct is not None and float(pct) >= _limit_pct(code) - 0.2


def _settle_row(
    row: DecisionOutcomeRecord,
    *,
    calendar: Sequence[date],
    session: Any,
    as_of: date,
) -> bool:
    """Settle one pending row in place. Returns True when the row changed."""

    if row.entry_session and row.exit_session:
        entry_day, exit_day = row.entry_session, row.exit_session
        decision_idx = bisect.bisect_left(calendar, entry_day) - 1
    else:
        decision_idx = bisect.bisect_right(calendar, row.decision_as_of) - 1
        entry_idx = max(decision_idx + 1, 0)
        if decision_idx < 0 or entry_idx >= len(calendar):
            return False
        entry_day = calendar[entry_idx]
        exit_idx = decision_idx + int(row.horizon_days or HORIZON_DAYS)
        exit_day = calendar[exit_idx] if exit_idx < len(calendar) else None
        row.entry_session = entry_day
        row.exit_session = exit_day

    if exit_day is None:
        return True  # calendar has not reached the exit day yet
    as_of_idx = bisect.bisect_right(calendar, as_of) - 1
    bars = _bars_for(session, row.code, row.decision_as_of)
    entry_bar = _bar_on(bars, entry_day)
    if entry_bar is None or not entry_bar.get("open") or not entry_bar.get("volume"):
        waited = as_of_idx - bisect.bisect_right(calendar, entry_day) + 1
        if waited > ENTRY_GRACE_SESSIONS:
            row.status = STATUS_SKIPPED_MISSING_ENTRY
            row.reason_code = "entry_bar_missing"
            row.settled_at = datetime.now()
            return True
        return False
    if _is_limit_locked(entry_bar, row.code):
        row.status = STATUS_SKIPPED_UNFILLABLE
        row.reason_code = "entry_limit_locked"
        row.entry_reference = float(entry_bar["open"])
        row.settled_at = datetime.now()
        return True
    row.entry_reference = float(entry_bar["open"])
    row.entry_price = float(entry_bar["open"])

    exit_bar = _bar_on(bars, exit_day)
    reason = None
    status = STATUS_SETTLED
    if exit_bar is None or not exit_bar.get("close"):
        waited = as_of_idx - bisect.bisect_right(calendar, exit_day) + 1
        if waited <= EXIT_GRACE_SESSIONS:
            return True
        resume = next((bar for bar in bars if bar["date"] > exit_day and bar.get("close")), None)
        last_bar = bars[-1] if bars else None
        if resume is not None:
            exit_bar = resume
            reason = "suspended_at_exit"
        elif last_bar is not None and last_bar.get("close") and last_bar["date"] < exit_day:
            exit_bar = last_bar
            status = STATUS_DELISTED_EXIT
            reason = "delisted_before_exit"
        else:
            row.status = STATUS_EXPIRED_MISSING_EXIT
            row.reason_code = "no_bar_at_exit"
            row.settled_at = datetime.now()
            return True

    gross = float(exit_bar["close"]) / float(row.entry_price) - 1.0
    row.exit_price = float(exit_bar["close"])
    row.trade_return = round(gross - float(row.cost_bps or COST_BPS) / 10000.0, 6)
    row.status = status
    row.reason_code = reason
    row.settled_at = datetime.now()
    return True


def settle_due_records(*, as_of: Optional[date] = None, db: Any = None) -> Dict[str, Any]:
    """Settle all pending rows whose exit session has arrived (local data only)."""

    manager = db or _db()
    today = as_of or date.today()
    counters = {"pending": 0, "settled": 0, "skipped": 0, "expired": 0, "delisted": 0}
    with manager.get_session() as session:
        calendar = _trading_calendar(session)
        if not calendar:
            return {"checked": 0, "error": "empty_calendar"}
        rows = (
            session.execute(
                select(DecisionOutcomeRecord).where(DecisionOutcomeRecord.status == STATUS_PENDING)
            )
            .scalars()
            .all()
        )
        changed = 0
        for row in rows:
            if _settle_row(row, calendar=calendar, session=session, as_of=today):
                changed += 1
            if row.status == STATUS_PENDING:
                counters["pending"] += 1
            elif row.status == STATUS_SETTLED:
                counters["settled"] += 1
            elif row.status == STATUS_EXPIRED_MISSING_EXIT:
                counters["expired"] += 1
            elif row.status == STATUS_DELISTED_EXIT:
                counters["delisted"] += 1
            else:
                counters["skipped"] += 1
        session.commit()
    counters["checked"] = len(rows)
    counters["updated"] = changed
    return counters


def build_decision_outcome_background_tasks(config: Any) -> List[Dict[str, Any]]:
    """Register the daily settlement task; empty when the feature is disabled."""

    if not bool(getattr(config, "agent_decision_outcome_enabled", False)):
        return []

    def settle_task() -> None:
        summary = settle_due_records()
        logger.info(
            "[DecisionOutcome] settlement done: checked=%s settled=%s pending=%s",
            summary.get("checked"),
            summary.get("settled"),
            summary.get("pending"),
        )

    return [{"task": settle_task, "interval_seconds": 3600, "run_immediately": False}]


__all__ = [
    "COST_BPS",
    "HORIZON_DAYS",
    "RECORDABLE_EXECUTION_MODES",
    "build_decision_outcome_background_tasks",
    "record_pending_records",
    "settle_due_records",
]
