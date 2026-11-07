"""Persistence for immutable Jev shadow evaluations."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from src.storage import DatabaseManager, JevEvaluation


class JevRepository:
    def __init__(self, db_manager: DatabaseManager | None = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.db.get_session() as session:
            row = session.get(JevEvaluation, evaluation_id)
            return json.loads(row.payload_json) if row else None

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.session_scope() as session:
            session.execute(insert(JevEvaluation).values(
                id=payload["evaluation_id"],
                stock_code=payload["stock_code"],
                market=payload["market"],
                as_of=date.fromisoformat(payload["as_of"]),
                purpose=payload["purpose"],
                status=payload["status"],
                source=payload["source"],
                source_ref=payload.get("source_ref"),
                payload_json=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            ).on_conflict_do_nothing(index_elements=["id"]))
        return self.get(payload["evaluation_id"])

    def list_recent(self, *, stock_code: str | None = None, limit: int = 50,
                    source: str | None = None, source_ref: str | None = None) -> list[dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(JevEvaluation).order_by(JevEvaluation.created_at.desc(), JevEvaluation.id.desc())
            if stock_code:
                query = query.where(JevEvaluation.stock_code == stock_code)
            if source is not None:
                query = query.where(JevEvaluation.source == source)
            if source_ref is not None:
                query = query.where(JevEvaluation.source_ref == source_ref)
            rows = session.execute(query.limit(limit)).scalars().all()
            return [json.loads(row.payload_json) for row in rows]
