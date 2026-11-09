# -*- coding: utf-8 -*-
"""Database access for portfolio backtest runs, inputs and data jobs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, delete, desc, func, select, update

from src.storage import (
    BacktestCorporateActionRecord,
    BacktestDataJobRecord,
    BacktestRunInputRecord,
    BacktestRunRecord,
    DatabaseManager,
)

logger = logging.getLogger(__name__)


class BacktestRunRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str,
        mode: str,
        status: str,
        request_fingerprint: str,
        config_json: str,
        engine_version: str,
        git_revision: str | None,
        git_dirty: bool,
        plan_hashes: list[str],
        source_pipeline: str,
        risk_profile: str,
        worker_kind: str = "standard",
    ) -> Dict[str, Any]:
        def _write(session):
            attempt = (
                session.execute(
                    select(func.max(BacktestRunRecord.attempt)).where(
                        BacktestRunRecord.request_fingerprint == request_fingerprint
                    )
                ).scalar_one_or_none()
                or 0
            ) + 1
            row = BacktestRunRecord(
                run_id=run_id,
                mode=mode,
                status=status,
                attempt=attempt,
                request_fingerprint=request_fingerprint,
                config_json=config_json,
                engine_version=engine_version,
                git_revision=git_revision,
                git_dirty=git_dirty,
                plan_hashes_json=json.dumps(plan_hashes, ensure_ascii=True),
                source_pipeline=source_pipeline,
                risk_profile=risk_profile,
                worker_kind=worker_kind,
                progress=0.0,
                phase="queued",
                cancel_requested=False,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(row)
            session.flush()
            return _run_dict(row)

        return self.db._run_write_transaction("create_backtest_run", _write)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(BacktestRunRecord).where(BacktestRunRecord.run_id == run_id).limit(1)
            ).scalar_one_or_none()
            return _run_dict(row) if row else None

    def list_runs(
        self,
        *,
        status: str | None = None,
        mode: str | None = None,
        source_pipeline: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        conditions = []
        if status:
            conditions.append(BacktestRunRecord.status == status)
        if mode:
            conditions.append(BacktestRunRecord.mode == mode)
        if source_pipeline:
            conditions.append(BacktestRunRecord.source_pipeline == source_pipeline)
        with self.db.get_session() as session:
            query = select(BacktestRunRecord)
            count_query = select(func.count(BacktestRunRecord.id))
            if conditions:
                query = query.where(and_(*conditions))
                count_query = count_query.where(and_(*conditions))
            total = int(session.execute(count_query).scalar_one() or 0)
            rows = session.execute(
                query.order_by(desc(BacktestRunRecord.created_at), desc(BacktestRunRecord.id))
                .offset(max(0, (page - 1) * page_size))
                .limit(page_size)
            ).scalars().all()
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": [_run_dict(row) for row in rows],
            }

    def find_active_by_request_fingerprint(self, request_fingerprint: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.request_fingerprint == request_fingerprint,
                        BacktestRunRecord.status.in_(["queued", "validating", "running"]),
                    )
                )
                .order_by(desc(BacktestRunRecord.created_at), desc(BacktestRunRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            return _run_dict(row) if row else None

    def find_succeeded_by_fingerprint(self, experiment_fingerprint: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.experiment_fingerprint == experiment_fingerprint,
                        BacktestRunRecord.status == "succeeded",
                        BacktestRunRecord.result_dir.is_not(None),
                    )
                )
                .order_by(desc(BacktestRunRecord.finished_at), desc(BacktestRunRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            return _run_dict(row) if row else None

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        worker_kind: str = "standard",
    ) -> Optional[Dict[str, Any]]:
        now = datetime.now()
        lease_until = now + timedelta(seconds=max(30, lease_seconds))

        def _write(session):
            row = session.execute(
                select(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.status == "queued",
                        BacktestRunRecord.worker_kind == worker_kind,
                    )
                )
                .order_by(BacktestRunRecord.created_at, BacktestRunRecord.id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "validating"
            row.lease_owner = worker_id
            row.lease_expires_at = lease_until
            row.heartbeat_at = now
            row.started_at = row.started_at or now
            row.phase = "claimed"
            row.progress = max(0.01, float(row.progress or 0.0))
            row.updated_at = now
            session.flush()
            return _run_dict(row)

        return self.db._run_write_transaction("claim_backtest_run", _write)

    def heartbeat(
        self,
        *,
        run_id: str,
        worker_id: str,
        progress: float,
        phase: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now()
        lease_until = now + timedelta(seconds=max(30, lease_seconds))

        def _write(session):
            result = session.execute(
                update(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.run_id == run_id,
                        BacktestRunRecord.lease_owner == worker_id,
                        BacktestRunRecord.status.in_(["validating", "running"]),
                    )
                )
                .values(
                    progress=float(max(0.0, min(1.0, progress))),
                    phase=phase,
                    heartbeat_at=now,
                    lease_expires_at=lease_until,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

        return bool(self.db._run_write_transaction("heartbeat_backtest_run", _write))

    def set_running(self, *, run_id: str, worker_id: str) -> bool:
        now = datetime.now()

        def _write(session):
            result = session.execute(
                update(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.run_id == run_id,
                        BacktestRunRecord.lease_owner == worker_id,
                    )
                )
                .values(status="running", phase="running", updated_at=now)
            )
            return result.rowcount > 0

        return bool(self.db._run_write_transaction("start_backtest_run", _write))

    def finalize(
        self,
        *,
        run_id: str,
        status: str,
        experiment_fingerprint: str | None = None,
        manifest_json: str | None = None,
        metrics_json: str | None = None,
        summary_json: str | None = None,
        quality_flags: list[str] | None = None,
        counts: dict[str, Any] | None = None,
        result_dir: str | None = None,
        data_quality: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        reused_from_run_id: str | None = None,
        progress: float = 1.0,
        phase: str = "finished",
    ) -> None:
        now = datetime.now()

        def _write(session):
            session.execute(
                update(BacktestRunRecord)
                .where(BacktestRunRecord.run_id == run_id)
                .values(
                    status=status,
                    experiment_fingerprint=experiment_fingerprint,
                    manifest_json=manifest_json,
                    metrics_json=metrics_json,
                    summary_json=summary_json,
                    quality_flags_json=json.dumps(quality_flags or [], ensure_ascii=True),
                    counts_json=json.dumps(counts or {}, ensure_ascii=True, default=str),
                    result_dir=result_dir,
                    data_quality=data_quality,
                    error_code=error_code,
                    error_message=error_message,
                    reused_from_run_id=reused_from_run_id,
                    progress=float(max(0.0, min(1.0, progress))),
                    phase=phase,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

        self.db._run_write_transaction("finalize_backtest_run", _write)

    def request_cancel(self, run_id: str) -> bool:
        now = datetime.now()

        def _write(session):
            row = session.execute(
                select(BacktestRunRecord).where(BacktestRunRecord.run_id == run_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.status in {"succeeded", "partial", "failed", "cancelled", "interrupted"}:
                return False
            row.cancel_requested = True
            row.updated_at = now
            if row.status == "queued":
                row.status = "cancelled"
                row.finished_at = now
                row.phase = "cancelled"
                row.progress = 0.0
            return True

        return bool(self.db._run_write_transaction("cancel_backtest_run", _write))

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.db.get_session() as session:
            value = session.execute(
                select(BacktestRunRecord.cancel_requested).where(BacktestRunRecord.run_id == run_id).limit(1)
            ).scalar_one_or_none()
            return bool(value)

    def reap_stale(self, *, limit: int = 100) -> int:
        now = datetime.now()

        def _write(session):
            rows = session.execute(
                select(BacktestRunRecord)
                .where(
                    and_(
                        BacktestRunRecord.status.in_(["validating", "running"]),
                        BacktestRunRecord.lease_expires_at.is_not(None),
                        BacktestRunRecord.lease_expires_at < now,
                    )
                )
                .order_by(BacktestRunRecord.id)
                .limit(limit)
            ).scalars().all()
            for row in rows:
                row.status = "interrupted"
                row.error_code = "lease_expired"
                row.error_message = "worker lease expired; rerun from frozen input"
                row.finished_at = now
                row.phase = "interrupted"
                row.lease_owner = None
                row.lease_expires_at = None
            return len(rows)

        return int(self.db._run_write_transaction("reap_stale_backtest_runs", _write))

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    def save_inputs(self, run_id: str, inputs: Iterable[Any]) -> None:
        def _write(session):
            session.execute(delete(BacktestRunInputRecord).where(BacktestRunInputRecord.run_id == run_id))
            for item in inputs:
                session.add(
                    BacktestRunInputRecord(
                        run_id=run_id,
                        input_id=item.input_id,
                        source_type=item.source_type,
                        source_id=item.source_id,
                        plan_id=item.plan_id,
                        revision=item.revision,
                        scenario_id=item.scenario_id,
                        decision_id=item.decision_id,
                        trace_refs_json=json.dumps(item.trace_refs, ensure_ascii=True),
                        symbol=item.symbol,
                        available_at=_naive(item.available_at),
                        valid_from=item.valid_from,
                        included=bool(item.included),
                        exclusion_reason=item.exclusion_reason,
                        quality_flags_json=json.dumps(item.quality_flags, ensure_ascii=True),
                        payload_hash=item.payload_hash,
                        created_at=datetime.now(),
                    )
                )

        self.db._run_write_transaction("save_backtest_run_inputs", _write)

    def get_inputs(self, run_id: str, *, included: bool | None = None) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            query = select(BacktestRunInputRecord).where(BacktestRunInputRecord.run_id == run_id)
            if included is not None:
                query = query.where(BacktestRunInputRecord.included == bool(included))
            rows = session.execute(query.order_by(BacktestRunInputRecord.id)).scalars().all()
            return [_input_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Data jobs
    # ------------------------------------------------------------------
    def create_data_job(self, *, job_id: str, job_type: str, request_json: str) -> Dict[str, Any]:
        def _write(session):
            row = BacktestDataJobRecord(
                job_id=job_id,
                job_type=job_type,
                status="queued",
                request_json=request_json,
                progress=0.0,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(row)
            session.flush()
            return _job_dict(row)

        return self.db._run_write_transaction("create_backtest_data_job", _write)

    def get_data_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(BacktestDataJobRecord).where(BacktestDataJobRecord.job_id == job_id).limit(1)
            ).scalar_one_or_none()
            return _job_dict(row) if row else None

    def start_data_job(self, job_id: str, *, worker_id: str) -> bool:
        now = datetime.now()

        def _write(session):
            result = session.execute(
                update(BacktestDataJobRecord)
                .where(
                    and_(
                        BacktestDataJobRecord.job_id == job_id,
                        BacktestDataJobRecord.status == "queued",
                    )
                )
                .values(
                    status="running",
                    lease_owner=worker_id,
                    started_at=now,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

        return bool(self.db._run_write_transaction("start_backtest_data_job", _write))

    def finish_data_job(
        self,
        job_id: str,
        *,
        status: str,
        result_json: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = datetime.now()

        def _write(session):
            session.execute(
                update(BacktestDataJobRecord)
                .where(BacktestDataJobRecord.job_id == job_id)
                .values(
                    status=status,
                    result_json=result_json,
                    error_code=error_code,
                    error_message=error_message,
                    progress=1.0 if status in {"succeeded", "partial"} else 0.0,
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

        self.db._run_write_transaction("finish_backtest_data_job", _write)

    def reap_data_jobs(self) -> int:
        now = datetime.now()

        def _write(session):
            result = session.execute(
                update(BacktestDataJobRecord)
                .where(BacktestDataJobRecord.status.in_(["queued", "running"]))
                .values(
                    status="interrupted",
                    error_code="service_restarted",
                    error_message="data job interrupted by service restart",
                    finished_at=now,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            return result.rowcount

        return int(self.db._run_write_transaction("reap_backtest_data_jobs", _write))

    # ------------------------------------------------------------------
    # Corporate actions (test/manual ingestion)
    # ------------------------------------------------------------------
    def upsert_corporate_action(
        self,
        *,
        symbol: str,
        ex_date: date,
        action_type: str,
        cash_per_share: float = 0.0,
        stock_ratio: float = 0.0,
        split_ratio: float = 1.0,
        version: str = "manual.v1",
        source: str = "manual",
        verified: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> None:
        def _write(session):
            existing = session.execute(
                select(BacktestCorporateActionRecord).where(
                    and_(
                        BacktestCorporateActionRecord.symbol == symbol,
                        BacktestCorporateActionRecord.ex_date == ex_date,
                        BacktestCorporateActionRecord.action_type == action_type,
                        BacktestCorporateActionRecord.version == version,
                    )
                ).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                existing.cash_per_share = cash_per_share
                existing.stock_ratio = stock_ratio
                existing.split_ratio = split_ratio
                existing.source = source
                existing.verified = verified
                existing.payload_json = json.dumps(payload or {}, ensure_ascii=True)
                return
            session.add(
                BacktestCorporateActionRecord(
                    symbol=symbol,
                    ex_date=ex_date,
                    action_type=action_type,
                    cash_per_share=cash_per_share,
                    stock_ratio=stock_ratio,
                    split_ratio=split_ratio,
                    version=version,
                    source=source,
                    verified=verified,
                    payload_json=json.dumps(payload or {}, ensure_ascii=True),
                    created_at=datetime.now(),
                )
            )

        self.db._run_write_transaction("upsert_backtest_corporate_action", _write)


def _run_dict(row: BacktestRunRecord) -> Dict[str, Any]:
    return {
        "run_id": row.run_id,
        "mode": row.mode,
        "status": row.status,
        "attempt": row.attempt,
        "request_fingerprint": row.request_fingerprint,
        "experiment_fingerprint": row.experiment_fingerprint,
        "reused_from_run_id": row.reused_from_run_id,
        "config_json": row.config_json,
        "engine_version": row.engine_version,
        "git_revision": row.git_revision,
        "git_dirty": bool(row.git_dirty),
        "plan_hashes_json": row.plan_hashes_json,
        "manifest_json": _load_json(row.manifest_json),
        "metrics_json": _load_json(row.metrics_json),
        "summary_json": _load_json(row.summary_json),
        "quality_flags_json": _load_json(row.quality_flags_json) or [],
        "counts_json": _load_json(row.counts_json) or {},
        "result_dir": row.result_dir,
        "data_quality": row.data_quality,
        "progress": float(row.progress or 0.0),
        "phase": row.phase or "",
        "error_code": row.error_code,
        "error_message": row.error_message,
        "cancel_requested": bool(row.cancel_requested),
        "lease_owner": row.lease_owner,
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "source_pipeline": getattr(row, "source_pipeline", None),
        "risk_profile": getattr(row, "risk_profile", None),
        "worker_kind": getattr(row, "worker_kind", None),
    }


def _input_dict(row: BacktestRunInputRecord) -> Dict[str, Any]:
    return {
        "input_id": row.input_id,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "plan_id": row.plan_id,
        "revision": row.revision,
        "scenario_id": row.scenario_id,
        "decision_id": row.decision_id,
        "trace_refs": _load_json(row.trace_refs_json) or [],
        "symbol": row.symbol,
        "available_at": row.available_at.isoformat() if row.available_at else None,
        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "included": bool(row.included),
        "exclusion_reason": row.exclusion_reason,
        "quality_flags": _load_json(row.quality_flags_json) or [],
        "payload_hash": row.payload_hash,
    }


def _job_dict(row: BacktestDataJobRecord) -> Dict[str, Any]:
    return {
        "job_id": row.job_id,
        "job_type": row.job_type,
        "status": row.status,
        "request_json": _load_json(row.request_json),
        "result_json": _load_json(row.result_json),
        "progress": float(row.progress or 0.0),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _load_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


__all__ = ["BacktestRunRepository"]
