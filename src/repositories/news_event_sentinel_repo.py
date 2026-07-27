# -*- coding: utf-8 -*-
"""Repository for news event sentinel run and trigger ledgers."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import desc, select

from src.storage import (
    DatabaseManager,
    NewsEventSentinelRun,
    NewsEventSentinelTrigger,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text[: len(fmt)], fmt)
                except ValueError:
                    continue
    return None


class NewsEventSentinelRepository:
    """Persistent run/trigger ledger for the news event sentinel."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def record_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id is required")

        def _write(session):
            row = session.execute(
                select(NewsEventSentinelRun).where(NewsEventSentinelRun.run_id == run_id)
            ).scalar_one_or_none()
            if row is None:
                row = NewsEventSentinelRun(run_id=run_id)
                session.add(row)
            row.started_at = _coerce_datetime(run.get("started_at")) or datetime.now()
            row.finished_at = _coerce_datetime(run.get("finished_at"))
            row.status = str(run.get("status") or "running")
            row.watched_symbol_count = int(run.get("watched_symbol_count") or 0)
            row.source_query_count = int(run.get("source_query_count") or 0)
            row.fetched_count = int(run.get("fetched_count") or 0)
            row.unseen_count = int(run.get("unseen_count") or 0)
            row.raw_episode_count = int(run.get("raw_episode_count") or 0)
            row.card_count = int(run.get("card_count") or 0)
            row.trigger_count = int(run.get("trigger_count") or 0)
            row.suppressed_by_cooldown = int(run.get("suppressed_by_cooldown") or 0)
            row.errors_json = _json_dumps(run.get("errors") or [])
            row.diagnostics_json = _json_dumps(run.get("diagnostics") or {})
            row.updated_at = datetime.now()
            session.flush()
            return self.run_to_dict(row)

        return self.db._run_write_transaction("news_event_sentinel.record_run", _write)

    def record_trigger(self, trigger: Dict[str, Any]) -> Dict[str, Any]:
        trigger_id = str(trigger.get("trigger_id") or "").strip()
        if not trigger_id:
            raise ValueError("trigger_id is required")

        def _write(session):
            row = session.execute(
                select(NewsEventSentinelTrigger).where(NewsEventSentinelTrigger.trigger_id == trigger_id)
            ).scalar_one_or_none()
            if row is None:
                row = NewsEventSentinelTrigger(trigger_id=trigger_id)
                session.add(row)
            row.run_id = str(trigger.get("run_id") or "")
            row.card_id = str(trigger.get("card_id") or "")
            row.event_id = str(trigger.get("event_id") or "")
            row.canonical_symbol = str(trigger.get("canonical_symbol") or "").upper()
            row.event_type = str(trigger.get("event_type") or "unknown")
            row.direction = str(trigger.get("direction") or "neutral")
            row.severity = str(trigger.get("severity") or "low")
            row.cooldown_key = str(trigger.get("cooldown_key") or "")
            row.triggered_at = _coerce_datetime(trigger.get("triggered_at")) or datetime.now()
            row.notification_status = str(trigger.get("notification_status") or "pending")
            row.trace_status = str(trigger.get("trace_status") or "skipped")
            row.notification_payload_json = _json_dumps(trigger.get("notification_payload") or {})
            row.diagnostics_json = _json_dumps(trigger.get("diagnostics") or {})
            row.updated_at = datetime.now()
            session.flush()
            return self.trigger_to_dict(row)

        return self.db._run_write_transaction("news_event_sentinel.record_trigger", _write)

    def latest_trigger_for_cooldown(self, cooldown_key: str, *, since: datetime) -> Optional[Dict[str, Any]]:
        key = str(cooldown_key or "").strip()
        if not key:
            return None
        with self.db.get_session() as session:
            row = session.execute(
                select(NewsEventSentinelTrigger)
                .where(NewsEventSentinelTrigger.cooldown_key == key)
                .where(NewsEventSentinelTrigger.triggered_at >= since)
                .order_by(desc(NewsEventSentinelTrigger.triggered_at), desc(NewsEventSentinelTrigger.id))
                .limit(1)
            ).scalar_one_or_none()
            return self.trigger_to_dict(row) if row else None

    def list_runs(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        capped_limit = max(1, min(int(limit or 20), 500))
        with self.db.get_session() as session:
            rows = session.execute(
                select(NewsEventSentinelRun)
                .order_by(desc(NewsEventSentinelRun.started_at), desc(NewsEventSentinelRun.id))
                .limit(capped_limit)
            ).scalars().all()
            return [self.run_to_dict(row) for row in rows]

    def list_triggers(
        self,
        *,
        symbol: str = "",
        cooldown_key: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        capped_limit = max(1, min(int(limit or 20), 500))
        with self.db.get_session() as session:
            stmt = select(NewsEventSentinelTrigger)
            if symbol:
                stmt = stmt.where(NewsEventSentinelTrigger.canonical_symbol == str(symbol).upper())
            if cooldown_key:
                stmt = stmt.where(NewsEventSentinelTrigger.cooldown_key == str(cooldown_key))
            rows = session.execute(
                stmt.order_by(desc(NewsEventSentinelTrigger.triggered_at), desc(NewsEventSentinelTrigger.id))
                .limit(capped_limit)
            ).scalars().all()
            return [self.trigger_to_dict(row) for row in rows]

    @staticmethod
    def run_to_dict(row: NewsEventSentinelRun) -> Dict[str, Any]:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "status": row.status,
            "watched_symbol_count": row.watched_symbol_count,
            "source_query_count": row.source_query_count,
            "fetched_count": row.fetched_count,
            "unseen_count": row.unseen_count,
            "raw_episode_count": row.raw_episode_count,
            "card_count": row.card_count,
            "trigger_count": row.trigger_count,
            "suppressed_by_cooldown": row.suppressed_by_cooldown,
            "errors": _json_loads(row.errors_json, []),
            "diagnostics": _json_loads(row.diagnostics_json, {}),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def trigger_to_dict(row: NewsEventSentinelTrigger) -> Dict[str, Any]:
        return {
            "id": row.id,
            "trigger_id": row.trigger_id,
            "run_id": row.run_id,
            "card_id": row.card_id,
            "event_id": row.event_id,
            "canonical_symbol": row.canonical_symbol,
            "event_type": row.event_type,
            "direction": row.direction,
            "severity": row.severity,
            "cooldown_key": row.cooldown_key,
            "triggered_at": _iso(row.triggered_at),
            "notification_status": row.notification_status,
            "trace_status": row.trace_status,
            "notification_payload": _json_loads(row.notification_payload_json, {}),
            "diagnostics": _json_loads(row.diagnostics_json, {}),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
