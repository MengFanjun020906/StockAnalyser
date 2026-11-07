# -*- coding: utf-8 -*-
"""Persistence for entry-signal and position-watch lifecycle state."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import and_, desc, func, or_, select

from src.storage import (
    DatabaseManager,
    EntrySignal,
    PositionWatch,
    PositionWatchEvent,
    SignalLifecycleEvent,
    SignalNotificationOutbox,
    SignalSchedulerState,
)


ACTIVE_SIGNAL_STATUSES = {"proposed", "active", "price_triggered", "needs_review", "ambiguous"}
TERMINAL_SIGNAL_STATUSES = {"filled", "expired", "invalidated", "superseded", "cancelled"}


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _owner_clause(column: Any, owner_id: Optional[str]) -> Any:
    return column == owner_id if owner_id is not None else column.is_(None)


def _model_dict(
    row: Any,
    *,
    json_fields: Sequence[str] = (),
    json_defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for field in json_fields:
        output_field = field.removesuffix("_json")
        default = (json_defaults or {}).get(output_field, {})
        payload[output_field] = _json_loads(payload.pop(field, None), default)
    return payload


class SignalLifecycleRepository:
    """Atomic snapshot-and-event writes for signal lifecycle objects."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    @staticmethod
    def entry_to_dict(row: EntrySignal) -> Dict[str, Any]:
        payload = _model_dict(
            row,
            json_fields=("trigger_rule_json", "invalidation_rules_json", "metadata_json"),
            json_defaults={"invalidation_rules": []},
        )
        payload["trigger_rule"] = payload.pop("trigger_rule", {})
        payload["invalidation_rules"] = payload.pop("invalidation_rules", [])
        payload["metadata"] = payload.pop("metadata", {})
        return payload

    @staticmethod
    def signal_event_to_dict(row: SignalLifecycleEvent) -> Dict[str, Any]:
        payload = _model_dict(row, json_fields=("payload_json",))
        payload["payload"] = payload.pop("payload", {})
        return payload

    @staticmethod
    def watch_to_dict(row: PositionWatch) -> Dict[str, Any]:
        payload = _model_dict(row, json_fields=("metadata_json",))
        payload["metadata"] = payload.pop("metadata", {})
        return payload

    @staticmethod
    def watch_event_to_dict(row: PositionWatchEvent) -> Dict[str, Any]:
        payload = _model_dict(row, json_fields=("payload_json",))
        payload["payload"] = payload.pop("payload", {})
        return payload

    @staticmethod
    def scheduler_state_to_dict(row: SignalSchedulerState) -> Dict[str, Any]:
        return _model_dict(row)

    @staticmethod
    def notification_to_dict(row: SignalNotificationOutbox) -> Dict[str, Any]:
        payload = _model_dict(row, json_fields=("payload_json",))
        payload["payload"] = payload.pop("payload", {})
        return payload

    def list_entry_signals(
        self,
        *,
        owner_id: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        stock_code: Optional[str] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(EntrySignal)
            if owner_id is not None:
                stmt = stmt.where(EntrySignal.owner_id == owner_id)
            normalized_statuses = [str(value) for value in statuses or [] if value]
            if normalized_statuses:
                stmt = stmt.where(EntrySignal.status.in_(normalized_statuses))
            if stock_code:
                stmt = stmt.where(EntrySignal.stock_code == stock_code)
            rows = session.execute(
                stmt.order_by(desc(EntrySignal.updated_at), desc(EntrySignal.id)).limit(max(1, min(limit, 1000)))
            ).scalars().all()
            return [self.entry_to_dict(row) for row in rows]

    def get_entry_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(EntrySignal).where(EntrySignal.signal_id == signal_id).limit(1)
            ).scalar_one_or_none()
            return self.entry_to_dict(row) if row is not None else None

    def list_signal_events(self, signal_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(SignalLifecycleEvent)
                .where(SignalLifecycleEvent.signal_id == signal_id)
                .order_by(SignalLifecycleEvent.occurred_at, SignalLifecycleEvent.id)
                .limit(max(1, min(limit, 2000)))
            ).scalars().all()
            return [self.signal_event_to_dict(row) for row in rows]

    def upsert_entry_plan(self, plan: Dict[str, Any], *, dedupe_key: str) -> Dict[str, Any]:
        """Create or revise the active signal for one owner/symbol/setup."""

        def _write(session):
            duplicate = session.execute(
                select(SignalLifecycleEvent).where(SignalLifecycleEvent.dedupe_key == dedupe_key).limit(1)
            ).scalar_one_or_none()
            if duplicate is not None:
                row = session.execute(
                    select(EntrySignal).where(EntrySignal.signal_id == duplicate.signal_id).limit(1)
                ).scalar_one()
                return self.entry_to_dict(row)

            active = session.execute(
                select(EntrySignal)
                .where(
                    _owner_clause(EntrySignal.owner_id, plan.get("owner_id")),
                    EntrySignal.stock_code == plan["stock_code"],
                    EntrySignal.market == plan["market"],
                    EntrySignal.signal_type == plan.get("signal_type", "entry"),
                    EntrySignal.status.in_(ACTIVE_SIGNAL_STATUSES),
                )
                .order_by(desc(EntrySignal.updated_at), desc(EntrySignal.id))
                .limit(1)
            ).scalar_one_or_none()

            now = plan.get("created_as_of") or datetime.now()
            signal_id = str(plan["signal_id"])
            event_type = "signal_created"
            from_status = None
            if active is not None and active.setup_type == plan.get("setup_type", "agent_entry"):
                signal_id = active.signal_id
                from_status = active.status
                previous_metadata = _json_loads(active.metadata_json, {})
                next_metadata = plan.get("metadata") or {}
                changed_fields = [
                    field for field in ("entry_low", "entry_high", "stop_price")
                    if getattr(active, field) != plan.get(field)
                ]
                changed = bool(
                    changed_fields
                    or _json_loads(active.invalidation_rules_json, []) != plan.get("invalidation_rules", [])
                )
                same_evidence = bool(
                    previous_metadata.get("evidence_fingerprint")
                    and previous_metadata.get("evidence_fingerprint") == next_metadata.get("evidence_fingerprint")
                )
                if changed and same_evidence:
                    active.last_checked_as_of = plan.get("last_checked_as_of") or now
                    active.version += 1
                    active.updated_at = datetime.now()
                    session.add(SignalLifecycleEvent(
                        event_id=uuid.uuid4().hex,
                        signal_id=active.signal_id,
                        event_type="target_drift_rejected",
                        from_status=active.status,
                        to_status=active.status,
                        occurred_at=now,
                        as_of=plan.get("last_checked_as_of") or now,
                        source="single_stock_query",
                        source_trace_id=plan.get("source_trace_id"),
                        dedupe_key=dedupe_key,
                        payload_json=_json_dumps({
                            "changed_fields": changed_fields,
                            "reason": "plan_changed_without_new_evidence",
                        }),
                    ))
                    session.flush()
                    return self.entry_to_dict(active)
                active.entry_low = plan.get("entry_low")
                active.entry_high = plan.get("entry_high")
                active.stop_price = plan.get("stop_price")
                active.plan_id = plan.get("plan_id") or active.plan_id
                active.scenario_id = plan.get("scenario_id") or active.scenario_id
                active.trigger_rule_json = _json_dumps(plan.get("trigger_rule") or {})
                active.invalidation_rules_json = _json_dumps(plan.get("invalidation_rules") or [])
                active.last_checked_as_of = plan.get("last_checked_as_of") or now
                # The first validity window is immutable. A revision may change
                # the plan, but cannot keep an unfilled signal alive forever.
                merged_metadata = dict(next_metadata)
                if plan["valid_until"] != active.valid_until:
                    merged_metadata["validity_change_rejected"] = {
                        "requested": str(plan["valid_until"]),
                        "frozen": str(active.valid_until),
                    }
                active.valid_trading_days = int(active.valid_trading_days or plan.get("valid_trading_days") or 5)
                active.evidence_quality = plan.get("evidence_quality") or active.evidence_quality
                active.source_trace_id = plan.get("source_trace_id") or active.source_trace_id
                active.metadata_json = _json_dumps(merged_metadata)
                requested_status = plan.get("status") or active.status
                status_event = None
                if requested_status == "needs_review" and active.status in {"proposed", "active"}:
                    active.status = "needs_review"
                    status_event = "signal_needs_review"
                elif requested_status == "active" and active.status == "needs_review":
                    active.status = "active"
                    status_event = "signal_reactivated"
                active.version += 1
                active.updated_at = datetime.now()
                if changed:
                    active.revision += 1
                    event_type = "signal_revised"
                elif status_event:
                    event_type = status_event
                else:
                    event_type = "signal_checked"
                row = active
            else:
                if active is not None:
                    active_from_status = active.status
                    active.status = "superseded"
                    active.version += 1
                    active.updated_at = datetime.now()
                    session.add(SignalLifecycleEvent(
                        event_id=uuid.uuid4().hex,
                        signal_id=active.signal_id,
                        event_type="signal_superseded",
                        from_status=active_from_status,
                        to_status="superseded",
                        occurred_at=now,
                        as_of=now,
                        source="single_stock_query",
                        source_trace_id=plan.get("source_trace_id"),
                        dedupe_key=f"{dedupe_key}:superseded:{active.signal_id}",
                        payload_json=_json_dumps({"superseded_by": signal_id}),
                    ))
                    plan["supersedes_signal_id"] = active.signal_id
                row = EntrySignal(
                    signal_id=signal_id,
                    plan_id=plan.get("plan_id"),
                    scenario_id=plan.get("scenario_id"),
                    owner_id=plan.get("owner_id"),
                    account_id=plan.get("account_id"),
                    stock_code=plan["stock_code"],
                    stock_name=plan.get("stock_name"),
                    market=plan["market"],
                    signal_type=plan.get("signal_type", "entry"),
                    setup_type=plan.get("setup_type", "agent_entry"),
                    direction=plan.get("direction", "long"),
                    status=plan.get("status", "active"),
                    revision=1,
                    entry_low=plan.get("entry_low"),
                    entry_high=plan.get("entry_high"),
                    stop_price=plan.get("stop_price"),
                    trigger_rule_json=_json_dumps(plan.get("trigger_rule") or {}),
                    invalidation_rules_json=_json_dumps(plan.get("invalidation_rules") or []),
                    created_as_of=now,
                    last_checked_as_of=plan.get("last_checked_as_of") or now,
                    valid_trading_days=int(plan.get("valid_trading_days") or 5),
                    valid_until=plan["valid_until"],
                    evidence_quality=plan.get("evidence_quality") or "ok",
                    source_trace_id=plan.get("source_trace_id"),
                    supersedes_signal_id=plan.get("supersedes_signal_id"),
                    metadata_json=_json_dumps(plan.get("metadata") or {}),
                    version=1,
                )
                session.add(row)
                session.flush()

            session.add(SignalLifecycleEvent(
                event_id=uuid.uuid4().hex,
                signal_id=row.signal_id,
                event_type=event_type,
                from_status=from_status,
                to_status=row.status,
                occurred_at=now,
                as_of=now,
                source="single_stock_query",
                source_trace_id=plan.get("source_trace_id"),
                dedupe_key=dedupe_key,
                payload_json=_json_dumps({
                    "entry_low": row.entry_low,
                    "entry_high": row.entry_high,
                    "entry_zones": (plan.get("trigger_rule") or {}).get("zones") or [],
                    "stop_price": row.stop_price,
                    "revision": row.revision,
                }),
            ))
            session.flush()
            return self.entry_to_dict(row)

        return self.db._run_write_transaction("upsert_entry_signal", _write)

    def transition_entry_signal(
        self,
        signal_id: str,
        *,
        to_status: str,
        event_type: str,
        occurred_at: datetime,
        as_of: datetime,
        source: str,
        dedupe_key: str,
        payload: Optional[Dict[str, Any]] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            duplicate = session.execute(
                select(SignalLifecycleEvent).where(SignalLifecycleEvent.dedupe_key == dedupe_key).limit(1)
            ).scalar_one_or_none()
            row = session.execute(
                select(EntrySignal).where(EntrySignal.signal_id == signal_id).limit(1)
            ).scalar_one()
            if duplicate is not None:
                return self.entry_to_dict(row)
            if expected_version is not None and row.version != expected_version:
                raise ValueError(f"entry signal version conflict: expected {expected_version}, actual {row.version}")
            from_status = row.status
            allowed_transitions = {
                "proposed": {"active", "price_triggered", "needs_review", "invalidated", "expired", "superseded", "cancelled"},
                "active": {"price_triggered", "needs_review", "invalidated", "expired", "ambiguous", "superseded", "cancelled"},
                "price_triggered": {"filled", "needs_review", "invalidated", "expired", "ambiguous", "superseded", "cancelled"},
                "needs_review": {"active", "price_triggered", "ambiguous", "invalidated", "expired", "superseded", "cancelled"},
                "ambiguous": {"active", "needs_review", "filled", "invalidated", "expired", "cancelled"},
            }
            if to_status != from_status and to_status not in allowed_transitions.get(from_status, set()):
                raise ValueError(f"invalid entry signal transition: {from_status} -> {to_status}")
            row.status = to_status
            row.last_checked_as_of = as_of
            row.version += 1
            row.updated_at = datetime.now()
            session.add(SignalLifecycleEvent(
                event_id=uuid.uuid4().hex,
                signal_id=signal_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                occurred_at=occurred_at,
                as_of=as_of,
                source=source,
                source_trace_id=(payload or {}).get("source_trace_id"),
                dedupe_key=dedupe_key,
                payload_json=_json_dumps(payload or {}),
            ))
            session.flush()
            return self.entry_to_dict(row)

        return self.db._run_write_transaction(f"transition_entry_signal[{signal_id}]", _write)

    def list_position_watches(
        self,
        *,
        account_id: Optional[int] = None,
        statuses: Optional[Iterable[str]] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(PositionWatch)
            if account_id is not None:
                stmt = stmt.where(PositionWatch.account_id == account_id)
            normalized_statuses = [str(value) for value in statuses or [] if value]
            if normalized_statuses:
                stmt = stmt.where(PositionWatch.status.in_(normalized_statuses))
            rows = session.execute(
                stmt.order_by(desc(PositionWatch.updated_at), desc(PositionWatch.id)).limit(max(1, min(limit, 1000)))
            ).scalars().all()
            return [self.watch_to_dict(row) for row in rows]

    def get_position_watch(self, watch_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(PositionWatch).where(PositionWatch.watch_id == watch_id).limit(1)
            ).scalar_one_or_none()
            return self.watch_to_dict(row) if row is not None else None

    def list_position_watch_events(self, watch_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PositionWatchEvent)
                .where(PositionWatchEvent.watch_id == watch_id)
                .order_by(PositionWatchEvent.occurred_at, PositionWatchEvent.id)
                .limit(max(1, min(limit, 2000)))
            ).scalars().all()
            return [self.watch_event_to_dict(row) for row in rows]

    def upsert_position_watch(
        self,
        position: Dict[str, Any],
        *,
        as_of: datetime,
        risk_level: str,
        current_action: str,
        score: Optional[int],
        exit_warning_level: int,
        event_type: str,
        event_payload: Dict[str, Any],
        dedupe_key: str,
        needs_review: bool = False,
        notification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            duplicate = session.execute(
                select(PositionWatchEvent).where(PositionWatchEvent.dedupe_key == dedupe_key).limit(1)
            ).scalar_one_or_none()
            row = session.execute(
                select(PositionWatch)
                .where(
                    PositionWatch.account_id == int(position["account_id"]),
                    PositionWatch.stock_code == position["stock_code"],
                    PositionWatch.market == position["market"],
                    PositionWatch.status.in_(("active", "needs_review")),
                )
                .order_by(desc(PositionWatch.position_cycle_id))
                .limit(1)
            ).scalar_one_or_none()
            if duplicate is not None and row is not None:
                return self.watch_to_dict(row)

            if row is None:
                latest_cycle = session.execute(
                    select(func.max(PositionWatch.position_cycle_id)).where(
                        PositionWatch.account_id == int(position["account_id"]),
                        PositionWatch.stock_code == position["stock_code"],
                        PositionWatch.market == position["market"],
                    )
                ).scalar_one_or_none() or 0
                cycle = int(latest_cycle) + 1
                watch_id = f"acct-{position['account_id']}-{position['market']}-{position['stock_code']}-cycle-{cycle}"
                row = PositionWatch(
                    watch_id=watch_id,
                    account_id=int(position["account_id"]),
                    owner_id=position.get("owner_id"),
                    stock_code=position["stock_code"],
                    stock_name=position.get("stock_name"),
                    market=position["market"],
                    position_cycle_id=cycle,
                    status="needs_review" if needs_review or risk_level == "unknown" else "active",
                    quantity=float(position.get("quantity") or 0.0),
                    average_cost=float(position.get("average_cost") or 0.0),
                    stop_price=position.get("stop_price"),
                    opened_at=position.get("opened_at") or as_of,
                    last_checked_as_of=as_of,
                    source_position_updated_at=position.get("source_position_updated_at"),
                    risk_level=risk_level,
                    current_action=current_action,
                    latest_score=score,
                    latest_exit_warning_level=exit_warning_level,
                    metadata_json=_json_dumps(position.get("metadata") or {}),
                    version=1,
                )
                session.add(row)
                session.flush()
                actual_event_type = "position_opened"
                from_status = None
            else:
                from_status = row.status
                row.quantity = float(position.get("quantity") or 0.0)
                row.average_cost = float(position.get("average_cost") or 0.0)
                row.stop_price = position.get("stop_price") or row.stop_price
                row.last_checked_as_of = as_of
                row.source_position_updated_at = position.get("source_position_updated_at")
                row.risk_level = risk_level
                row.current_action = current_action
                row.latest_score = score
                row.latest_exit_warning_level = exit_warning_level
                row.status = "needs_review" if needs_review or risk_level == "unknown" else "active"
                row.metadata_json = _json_dumps(position.get("metadata") or {})
                row.version += 1
                row.updated_at = datetime.now()
                actual_event_type = event_type

            session.add(PositionWatchEvent(
                event_id=uuid.uuid4().hex,
                watch_id=row.watch_id,
                event_type=actual_event_type,
                from_status=from_status,
                to_status=row.status,
                occurred_at=as_of,
                as_of=as_of,
                source="portfolio_scheduler",
                source_trace_id=event_payload.get("source_trace_id"),
                dedupe_key=dedupe_key,
                payload_json=_json_dumps(event_payload),
            ))
            if notification:
                session.add(SignalNotificationOutbox(
                    notification_id=uuid.uuid4().hex,
                    dedupe_key=str(notification["dedupe_key"]),
                    watch_id=row.watch_id,
                    state_version=int(row.version),
                    action=str(notification["action"]),
                    channel=str(notification.get("channel") or "all"),
                    status="pending",
                    payload_json=_json_dumps(notification.get("payload") or {}),
                    attempts=0,
                    next_attempt_at=notification.get("next_attempt_at") or datetime.now(),
                ))
            session.flush()
            return self.watch_to_dict(row)

        return self.db._run_write_transaction("upsert_position_watch", _write)

    def close_position_watch(
        self,
        watch_id: str,
        *,
        as_of: datetime,
        dedupe_key: str,
        reason: str = "position_quantity_zero",
        notification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            row = session.execute(
                select(PositionWatch).where(PositionWatch.watch_id == watch_id).limit(1)
            ).scalar_one()
            duplicate = session.execute(
                select(PositionWatchEvent).where(PositionWatchEvent.dedupe_key == dedupe_key).limit(1)
            ).scalar_one_or_none()
            if duplicate is not None:
                return self.watch_to_dict(row)
            from_status = row.status
            row.status = "closed"
            row.quantity = 0.0
            row.current_action = "exit"
            row.last_checked_as_of = as_of
            row.version += 1
            row.updated_at = datetime.now()
            session.add(PositionWatchEvent(
                event_id=uuid.uuid4().hex,
                watch_id=watch_id,
                event_type="position_closed",
                from_status=from_status,
                to_status="closed",
                occurred_at=as_of,
                as_of=as_of,
                source="portfolio_scheduler",
                dedupe_key=dedupe_key,
                payload_json=_json_dumps({"reason": reason}),
            ))
            if notification:
                session.add(SignalNotificationOutbox(
                    notification_id=uuid.uuid4().hex,
                    dedupe_key=str(notification["dedupe_key"]),
                    watch_id=row.watch_id,
                    state_version=int(row.version),
                    action=str(notification.get("action") or "exit"),
                    channel=str(notification.get("channel") or "all"),
                    status="pending",
                    payload_json=_json_dumps(notification.get("payload") or {}),
                    attempts=0,
                    next_attempt_at=notification.get("next_attempt_at") or datetime.now(),
                ))
            session.flush()
            return self.watch_to_dict(row)

        return self.db._run_write_transaction(f"close_position_watch[{watch_id}]", _write)

    def update_scheduler_state(
        self,
        market: str,
        *,
        status: str,
        heartbeat_at: datetime,
        last_started_at: Optional[datetime] = None,
        last_success_at: Optional[datetime] = None,
        last_trading_date: Optional[date] = None,
        next_run_at: Optional[datetime] = None,
        watch_count: Optional[int] = None,
        l1_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _write(session):
            row = session.get(SignalSchedulerState, market)
            if row is None:
                row = SignalSchedulerState(market=market, heartbeat_at=heartbeat_at)
                session.add(row)
            row.status = status
            row.heartbeat_at = heartbeat_at
            if last_started_at is not None:
                row.last_started_at = last_started_at
            if last_success_at is not None:
                row.last_success_at = last_success_at
            if last_trading_date is not None:
                row.last_trading_date = last_trading_date
            row.next_run_at = next_run_at
            if watch_count is not None:
                row.watch_count = watch_count
            if l1_count is not None:
                row.l1_count = l1_count
            row.error = error
            row.updated_at = datetime.now()
            session.flush()
            return self.scheduler_state_to_dict(row)

        return self.db._run_write_transaction(f"signal_scheduler_state[{market}]", _write)

    def list_scheduler_states(self) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(SignalSchedulerState).order_by(SignalSchedulerState.market)
            ).scalars().all()
            return [self.scheduler_state_to_dict(row) for row in rows]

    def claim_notification_batch(self, *, now: Optional[datetime] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Claim pending/failed notifications atomically for one dispatcher."""
        current = now or datetime.now()
        stale_sending_before = current - timedelta(minutes=10)

        def _write(session):
            rows = session.execute(
                select(SignalNotificationOutbox)
                .where(
                    or_(
                        and_(
                            SignalNotificationOutbox.status.in_(("pending", "failed")),
                            SignalNotificationOutbox.next_attempt_at <= current,
                        ),
                        and_(
                            SignalNotificationOutbox.status == "sending",
                            SignalNotificationOutbox.updated_at <= stale_sending_before,
                        ),
                    ),
                )
                .order_by(SignalNotificationOutbox.next_attempt_at, SignalNotificationOutbox.id)
                .limit(max(1, min(limit, 100)))
            ).scalars().all()
            claimed = []
            for row in rows:
                row.status = "sending"
                row.attempts += 1
                row.updated_at = current
                claimed.append(self.notification_to_dict(row))
            session.flush()
            return claimed

        return self.db._run_write_transaction("claim_signal_notifications", _write)

    def complete_notification(self, notification_id: str, *, sent: bool, error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = datetime.now()

        def _write(session):
            row = session.execute(
                select(SignalNotificationOutbox).where(
                    SignalNotificationOutbox.notification_id == notification_id
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "sent" if sent else "failed"
            row.last_error = None if sent else str(error or "notification delivery failed")
            row.sent_at = now if sent else row.sent_at
            if not sent:
                delay = min(3600, 30 * (2 ** max(0, min(row.attempts - 1, 6))))
                row.next_attempt_at = now + timedelta(seconds=delay)
            row.updated_at = now
            session.flush()
            return self.notification_to_dict(row)

        return self.db._run_write_transaction(f"complete_signal_notification[{notification_id}]", _write)


__all__ = [
    "ACTIVE_SIGNAL_STATUSES",
    "SignalLifecycleRepository",
    "TERMINAL_SIGNAL_STATUSES",
]
