# -*- coding: utf-8 -*-
"""Repository implementing the durable Graphiti outbox state machine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, or_, select

from src.storage import DatabaseManager, GraphitiOutbox


class GraphitiOutboxRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def enqueue(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        payload: Optional[Dict[str, Any]] = None,
        market: str = "cn",
        event_key: str = "",
    ) -> Dict[str, Any]:
        event_type_key = str(event_type or "").strip()
        aggregate_key = str(aggregate_id or "").strip()
        if not event_type_key or not aggregate_key:
            raise ValueError("event_type and aggregate_id are required")
        key = str(event_key or f"{event_type_key}:{market}:{aggregate_key}").strip()
        encoded_payload = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
        now = datetime.now()

        def _write(session):
            row = session.execute(
                select(GraphitiOutbox).where(GraphitiOutbox.event_key == key)
            ).scalar_one_or_none()
            if row is None:
                row = GraphitiOutbox(event_key=key)
                session.add(row)
            row.event_type = event_type_key
            row.aggregate_id = aggregate_key
            row.market = str(market or "cn")
            row.payload_json = encoded_payload
            row.status = "pending"
            row.attempt_count = 0
            row.available_at = now
            row.locked_at = None
            row.completed_at = None
            row.last_error = ""
            row.updated_at = now
            session.flush()
            return self._to_dict(row)

        return self.db._run_write_transaction("graphiti_outbox.enqueue", _write)

    def claim_batch(self, *, limit: int = 10, stale_after_seconds: int = 900) -> List[Dict[str, Any]]:
        capped_limit = max(1, min(int(limit or 10), 100))
        now = datetime.now()
        stale_before = now - timedelta(seconds=max(30, int(stale_after_seconds or 900)))

        def _write(session):
            stale_rows = session.execute(
                select(GraphitiOutbox).where(
                    GraphitiOutbox.status == "processing",
                    GraphitiOutbox.locked_at < stale_before,
                )
            ).scalars().all()
            for row in stale_rows:
                row.status = "retry"
                row.available_at = now
                row.locked_at = None
            rows = session.execute(
                select(GraphitiOutbox)
                .where(GraphitiOutbox.status.in_(["pending", "retry"]))
                .where(or_(GraphitiOutbox.available_at.is_(None), GraphitiOutbox.available_at <= now))
                .order_by(
                    case(
                        (GraphitiOutbox.event_type == "news_signal_card_delete", 0),
                        (GraphitiOutbox.event_type == "news_signal_edge_projection", 1),
                        else_=2,
                    ),
                    GraphitiOutbox.available_at,
                    GraphitiOutbox.id,
                )
                .limit(capped_limit)
            ).scalars().all()
            for row in rows:
                row.status = "processing"
                row.locked_at = now
                row.updated_at = now
            session.flush()
            return [self._to_dict(row) for row in rows]

        return self.db._run_write_transaction("graphiti_outbox.claim_batch", _write)

    def mark_succeeded(self, job_id: int) -> None:
        now = datetime.now()

        def _write(session):
            row = session.get(GraphitiOutbox, int(job_id))
            if row is None:
                return None
            row.status = "succeeded"
            row.completed_at = now
            row.locked_at = None
            row.last_error = ""
            row.updated_at = now
            return None

        self.db._run_write_transaction("graphiti_outbox.mark_succeeded", _write)

    def mark_failed(
        self,
        job_id: int,
        *,
        error: str,
        max_attempts: int = 5,
        retry_base_seconds: int = 30,
    ) -> str:
        now = datetime.now()

        def _write(session):
            row = session.get(GraphitiOutbox, int(job_id))
            if row is None:
                return "missing"
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_error = str(error or "")[:4000]
            row.locked_at = None
            row.updated_at = now
            if row.attempt_count >= max(1, int(max_attempts or 1)):
                row.status = "dead"
                row.completed_at = now
            else:
                delay = min(3600, max(1, int(retry_base_seconds or 30)) * (2 ** (row.attempt_count - 1)))
                row.status = "retry"
                row.available_at = now + timedelta(seconds=delay)
            return str(row.status)

        return str(self.db._run_write_transaction("graphiti_outbox.mark_failed", _write))

    def metrics(self) -> Dict[str, Any]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(GraphitiOutbox.status, func.count(GraphitiOutbox.id)).group_by(GraphitiOutbox.status)
            ).all()
            return {
                "status_counts": {str(status or "unknown"): int(count or 0) for status, count in rows},
                "total": sum(int(count or 0) for _status, count in rows),
            }

    @staticmethod
    def _to_dict(row: GraphitiOutbox) -> Dict[str, Any]:
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        return {
            "id": row.id,
            "event_key": row.event_key,
            "event_type": row.event_type,
            "aggregate_id": row.aggregate_id,
            "market": row.market,
            "payload": payload if isinstance(payload, dict) else {},
            "status": row.status,
            "attempt_count": int(row.attempt_count or 0),
            "available_at": row.available_at.isoformat() if row.available_at else None,
            "last_error": row.last_error or "",
        }
