from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any

from sqlalchemy import desc, select

from src.storage import (
    AccountPlanDecisionRecord,
    ChipDistributionSnapshotRecord,
    DatabaseManager,
    MarketRuleSnapshotRecord,
    PlanReviewArtifactRecord,
    PricePlanLifecycleEvent,
    PricePlanLifecycleStateRecord,
    TradePlanTraceLink,
    TradePlanArtifactRecord,
)

from .fingerprint import fingerprint, stable_id
from .lifecycle import evaluate_scenario_lifecycle
from .schemas import (
    AccountPlanDecision,
    ChipSnapshot,
    MarketRuleSnapshot,
    PlanLifecycleState,
    PlanReviewArtifact,
    PricePlanArtifact,
)


class PricePlanRepository:
    """Persistence boundary for immutable plan artifacts and scenario state."""

    def __init__(self, db_manager: DatabaseManager | None = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def save_artifact(self, artifact: PricePlanArtifact) -> PricePlanArtifact:
        def _write(session):
            existing = session.execute(
                select(TradePlanArtifactRecord)
                .where(
                    TradePlanArtifactRecord.calculation_fingerprint
                    == artifact.calculation_fingerprint
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                self._link_trace(session, existing.plan_id, artifact.trace_ref)
                return PricePlanArtifact.model_validate_json(existing.artifact_json)

            latest = session.execute(
                select(TradePlanArtifactRecord)
                .where(TradePlanArtifactRecord.symbol == artifact.symbol)
                .order_by(desc(TradePlanArtifactRecord.revision))
                .limit(1)
            ).scalar_one_or_none()
            stored = artifact.model_copy(
                update={
                    "revision": int(latest.revision or 0) + 1 if latest else 1,
                    "supersedes_plan_id": latest.plan_id if latest else None,
                }
            )
            if latest is not None and latest.plan_id != stored.plan_id:
                self._supersede_plan_states(session, latest.plan_id, stored)
            valid_until = max(
                (item.confirmation_deadline for item in stored.scenarios),
                default=None,
            )
            session.add(
                TradePlanArtifactRecord(
                    plan_id=stored.plan_id,
                    revision=stored.revision,
                    supersedes_plan_id=stored.supersedes_plan_id,
                    symbol=stored.symbol,
                    market=stored.market,
                    timeframe=stored.timeframe,
                    requested_as_of=stored.requested_as_of,
                    actual_as_of_session=stored.actual_as_of_session,
                    generation_status=stored.generation_status.value,
                    valid_until=valid_until,
                    data_fingerprint=stored.data_fingerprint,
                    calculation_fingerprint=stored.calculation_fingerprint,
                    plan_fingerprint=stored.plan_fingerprint,
                    algorithm_version=stored.algorithm_version,
                    params_version=stored.params_version,
                    artifact_json=stored.model_dump_json(),
                    trace_ref=stored.trace_ref,
                    created_at=_naive_datetime(stored.created_at),
                )
            )
            self._link_trace(session, stored.plan_id, stored.trace_ref)
            self._save_market_rules(session, stored)
            for scenario in stored.scenarios:
                event_seed = {
                    "plan_id": stored.plan_id,
                    "scenario_id": scenario.scenario_id,
                    "event_type": "plan_created",
                }
                event_id = stable_id("plan-event", event_seed, length=24)
                session.add(
                    PricePlanLifecycleEvent(
                        event_id=event_id,
                        plan_id=stored.plan_id,
                        scenario_id=scenario.scenario_id,
                        event_type="plan_created",
                        occurred_at=_naive_datetime(stored.created_at),
                        as_of=_naive_datetime(stored.created_at),
                        dedupe_key=f"plan_created:{stored.plan_id}:{scenario.scenario_id}",
                        payload_json=json.dumps(
                            {
                                "scenario_fingerprint": scenario.scenario_fingerprint,
                                "role": scenario.role.value,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                session.add(
                    PricePlanLifecycleStateRecord(
                        plan_id=stored.plan_id,
                        scenario_id=scenario.scenario_id,
                        state_version=1,
                        state="waiting_touch",
                        last_evaluated_as_of=stored.actual_as_of_session,
                        last_event_id=event_id,
                    )
                )
            session.flush()
            return stored

        return self.db._run_write_transaction("save_price_plan_artifact", _write)

    def get_artifact(self, plan_id: str) -> PricePlanArtifact | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(TradePlanArtifactRecord)
                .where(TradePlanArtifactRecord.plan_id == plan_id)
                .limit(1)
            ).scalar_one_or_none()
            return PricePlanArtifact.model_validate_json(row.artifact_json) if row else None

    def get_latest_artifact(self, symbol: str) -> PricePlanArtifact | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(TradePlanArtifactRecord)
                .where(TradePlanArtifactRecord.symbol == symbol)
                .order_by(
                    desc(TradePlanArtifactRecord.actual_as_of_session),
                    desc(TradePlanArtifactRecord.revision),
                )
                .limit(1)
            ).scalar_one_or_none()
            return PricePlanArtifact.model_validate_json(row.artifact_json) if row else None

    def list_artifacts_by_trace(self, trace_ref: str) -> list[PricePlanArtifact]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(TradePlanArtifactRecord)
                .outerjoin(
                    TradePlanTraceLink,
                    TradePlanTraceLink.plan_id == TradePlanArtifactRecord.plan_id,
                )
                .where(
                    (TradePlanTraceLink.trace_ref == trace_ref)
                    | (TradePlanArtifactRecord.trace_ref == trace_ref)
                )
                .order_by(TradePlanArtifactRecord.revision)
            ).scalars().all()
            unique = {row.plan_id: row for row in rows}
            return [
                PricePlanArtifact.model_validate_json(row.artifact_json)
                for row in unique.values()
            ]

    def get_market_rules(self, snapshot_id: str) -> MarketRuleSnapshot | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(MarketRuleSnapshotRecord)
                .where(MarketRuleSnapshotRecord.snapshot_id == snapshot_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                return MarketRuleSnapshot.model_validate_json(row.snapshot_json)
            except Exception:
                return None

    def get_market_rules_for_symbol_date(
        self,
        symbol: str,
        trade_date: Any,
    ) -> MarketRuleSnapshot | None:
        normalized = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:]
        with self.db.get_session() as session:
            rows = session.execute(
                select(MarketRuleSnapshotRecord)
                .where(
                    MarketRuleSnapshotRecord.symbol == normalized,
                    MarketRuleSnapshotRecord.trade_date == trade_date,
                )
                .order_by(desc(MarketRuleSnapshotRecord.id))
            ).scalars().all()
            for row in rows:
                try:
                    rules = MarketRuleSnapshot.model_validate_json(row.snapshot_json)
                except Exception:
                    continue
                if (
                    rules.symbol == normalized
                    and rules.instrument_type_verified
                    and rules.st_status_verified
                    and rules.listing_status_verified
                ):
                    return rules
        return None

    def list_artifacts(
        self,
        *,
        symbol: str | None = None,
        limit: int = 300,
    ) -> list[PricePlanArtifact]:
        with self.db.get_session() as session:
            stmt = select(TradePlanArtifactRecord)
            if symbol:
                stmt = stmt.where(TradePlanArtifactRecord.symbol == symbol)
            rows = session.execute(
                stmt.order_by(
                    desc(TradePlanArtifactRecord.actual_as_of_session),
                    desc(TradePlanArtifactRecord.revision),
                ).limit(max(1, min(int(limit), 1000)))
            ).scalars().all()
            return [PricePlanArtifact.model_validate_json(row.artifact_json) for row in rows]

    def list_lifecycle_states(self, plan_id: str) -> list[PlanLifecycleState]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PricePlanLifecycleStateRecord)
                .where(PricePlanLifecycleStateRecord.plan_id == plan_id)
                .order_by(PricePlanLifecycleStateRecord.id)
            ).scalars().all()
            return [_lifecycle_state(row) for row in rows]

    def list_lifecycle_events(self, plan_id: str) -> list[dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(PricePlanLifecycleEvent)
                .where(PricePlanLifecycleEvent.plan_id == plan_id)
                .order_by(PricePlanLifecycleEvent.occurred_at, PricePlanLifecycleEvent.id)
            ).scalars().all()
            return [
                {
                    "event_id": row.event_id,
                    "scenario_id": row.scenario_id,
                    "event_type": row.event_type,
                    "occurred_at": row.occurred_at,
                    "as_of": row.as_of,
                    "source": "deterministic_price_plan",
                    "payload": json.loads(row.payload_json) if row.payload_json else {},
                }
                for row in rows
            ]

    def reconcile_lifecycle(
        self,
        artifact: PricePlanArtifact,
        bars: list[Any],
    ) -> list[PlanLifecycleState]:
        from .params import get_price_plan_params

        params = get_price_plan_params(artifact.params_version)
        if params is None:
            raise ValueError(f"unknown price-plan params version: {artifact.params_version}")
        event_map = {
            scenario.scenario_id: evaluate_scenario_lifecycle(
                scenario,
                bars,
                params=params,
            )
            for scenario in artifact.scenarios
        }
        latest_bar_date = max(
            (
                _bar_date(item)
                for item in bars
                if _bar_date(item) is not None
            ),
            default=artifact.actual_as_of_session,
        )

        def _write(session):
            for scenario in artifact.scenarios:
                state = session.execute(
                    select(PricePlanLifecycleStateRecord)
                    .where(
                        PricePlanLifecycleStateRecord.plan_id == artifact.plan_id,
                        PricePlanLifecycleStateRecord.scenario_id == scenario.scenario_id,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if state is None or state.state in {"invalidated", "expired", "closed"}:
                    continue
                for event in event_map[scenario.scenario_id]:
                    dedupe_key = (
                        f"{event.event_type}:{artifact.plan_id}:{scenario.scenario_id}:"
                        f"{event.occurred_on.isoformat()}"
                    )
                    existing = session.execute(
                        select(PricePlanLifecycleEvent)
                        .where(PricePlanLifecycleEvent.dedupe_key == dedupe_key)
                        .limit(1)
                    ).scalar_one_or_none()
                    if existing is not None:
                        continue
                    event_id = stable_id("plan-event", {"dedupe_key": dedupe_key}, length=24)
                    occurred_at = datetime.combine(event.occurred_on, time(hour=15))
                    session.add(
                        PricePlanLifecycleEvent(
                            event_id=event_id,
                            plan_id=artifact.plan_id,
                            scenario_id=scenario.scenario_id,
                            event_type=event.event_type,
                            occurred_at=occurred_at,
                            as_of=occurred_at,
                            dedupe_key=dedupe_key,
                            payload_json=json.dumps(event.payload, ensure_ascii=False),
                        )
                    )
                    state.state_version += 1
                    state.state = event.state
                    state.last_event_id = event_id
                    if event.event_type == "touched":
                        state.first_touched_at = state.first_touched_at or occurred_at
                    elif event.event_type == "technical_confirmed":
                        state.confirmed_at = occurred_at
                    elif event.event_type in {"invalidated", "ambiguous"}:
                        state.invalidated_at = occurred_at if event.event_type == "invalidated" else None
                    elif event.event_type == "expired":
                        state.expired_at = occurred_at
                state.last_evaluated_as_of = latest_bar_date
            session.flush()

        self.db._run_write_transaction("reconcile_price_plan_lifecycle", _write)
        return self.list_lifecycle_states(artifact.plan_id)

    @staticmethod
    def _supersede_plan_states(session, plan_id: str, replacement: PricePlanArtifact) -> None:
        states = session.execute(
            select(PricePlanLifecycleStateRecord)
            .where(PricePlanLifecycleStateRecord.plan_id == plan_id)
        ).scalars().all()
        for state in states:
            if state.state in {"invalidated", "expired", "closed"}:
                continue
            dedupe_key = f"superseded:{plan_id}:{state.scenario_id}:{replacement.plan_id}"
            event_id = stable_id("plan-event", {"dedupe_key": dedupe_key}, length=24)
            occurred_at = _naive_datetime(replacement.created_at)
            session.add(
                PricePlanLifecycleEvent(
                    event_id=event_id,
                    plan_id=plan_id,
                    scenario_id=state.scenario_id,
                    event_type="superseded",
                    occurred_at=occurred_at,
                    as_of=occurred_at,
                    dedupe_key=dedupe_key,
                    payload_json=json.dumps(
                        {"superseded_by_plan_id": replacement.plan_id}, ensure_ascii=False
                    ),
                )
            )
            state.state_version += 1
            state.state = "closed"
            state.last_event_id = event_id
            state.last_evaluated_as_of = replacement.actual_as_of_session

    def save_review(self, review: PlanReviewArtifact) -> PlanReviewArtifact:
        def _write(session):
            existing = session.execute(
                select(PlanReviewArtifactRecord)
                .where(PlanReviewArtifactRecord.review_fingerprint == review.review_fingerprint)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return PlanReviewArtifact.model_validate_json(existing.artifact_json)
            session.add(
                PlanReviewArtifactRecord(
                    review_id=review.review_id,
                    plan_id=review.plan_id,
                    scenario_id=review.scenario_id,
                    evidence_bundle_fingerprint=review.evidence_bundle_fingerprint,
                    prompt_version=review.prompt_version,
                    model_ref=review.model_ref,
                    conclusion=review.conclusion.value,
                    review_status=review.review_status,
                    review_fingerprint=review.review_fingerprint,
                    artifact_json=review.model_dump_json(),
                    created_at=_naive_datetime(review.created_at),
                )
            )
            return review

        return self.db._run_write_transaction("save_price_plan_review", _write)

    def save_account_decision(
        self,
        decision: AccountPlanDecision,
        *,
        owner_id: str | None = None,
        account_id: int | None = None,
    ) -> AccountPlanDecision:
        def _write(session):
            existing = session.execute(
                select(AccountPlanDecisionRecord)
                .where(AccountPlanDecisionRecord.decision_id == decision.decision_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return AccountPlanDecision.model_validate_json(existing.artifact_json)
            session.add(
                AccountPlanDecisionRecord(
                    decision_id=decision.decision_id,
                    plan_id=decision.plan_id,
                    owner_id=owner_id,
                    account_id=account_id,
                    selected_scenario_id=decision.selected_scenario_id,
                    risk_profile=decision.risk_profile,
                    execution_status=decision.execution_status,
                    input_fingerprint=decision.input_fingerprint,
                    artifact_json=decision.model_dump_json(),
                    created_at=_naive_datetime(decision.created_at),
                )
            )
            return decision

        return self.db._run_write_transaction("save_account_price_plan_decision", _write)

    def get_latest_review(self, plan_id: str) -> dict[str, Any] | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(PlanReviewArtifactRecord)
                .where(PlanReviewArtifactRecord.plan_id == plan_id)
                .order_by(desc(PlanReviewArtifactRecord.created_at), desc(PlanReviewArtifactRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            return json.loads(row.artifact_json) if row else None

    def get_latest_account_decision(self, plan_id: str) -> dict[str, Any] | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(AccountPlanDecisionRecord)
                .where(AccountPlanDecisionRecord.plan_id == plan_id)
                .order_by(desc(AccountPlanDecisionRecord.created_at), desc(AccountPlanDecisionRecord.id))
                .limit(1)
            ).scalar_one_or_none()
            return json.loads(row.artifact_json) if row else None

    def save_chip_snapshot(
        self,
        symbol: str,
        chip: ChipSnapshot,
        *,
        raw_payload: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> str:
        observed = observed_at or datetime.now()
        seed = {
            "symbol": symbol,
            "trade_date": chip.trade_date,
            "observed_at": observed,
            "chip": chip,
        }
        snapshot_id = stable_id("chip", seed, length=24)

        def _write(session):
            existing = session.execute(
                select(ChipDistributionSnapshotRecord)
                .where(ChipDistributionSnapshotRecord.snapshot_id == snapshot_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ChipDistributionSnapshotRecord(
                        snapshot_id=snapshot_id,
                        symbol=symbol,
                        trade_date=chip.trade_date,
                        observed_at=observed,
                        source=chip.source,
                        normalized_json=chip.model_dump_json(),
                        raw_payload_json=json.dumps(raw_payload, ensure_ascii=False, default=str)
                        if raw_payload is not None
                        else None,
                    )
                )
            return snapshot_id

        return self.db._run_write_transaction("save_chip_snapshot", _write)

    def save_market_rules(self, rules: MarketRuleSnapshot) -> str:
        def _write(session):
            existing = session.execute(
                select(MarketRuleSnapshotRecord)
                .where(MarketRuleSnapshotRecord.snapshot_id == rules.snapshot_id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    MarketRuleSnapshotRecord(
                        snapshot_id=rules.snapshot_id,
                        symbol=rules.symbol,
                        trade_date=rules.trade_date,
                        venue=rules.venue,
                        board=rules.board,
                        snapshot_json=rules.model_dump_json(),
                    )
                )
            return rules.snapshot_id

        return self.db._run_write_transaction("save_market_rule_snapshot", _write)

    def get_chip_snapshot(self, symbol: str, as_of: datetime) -> ChipSnapshot | None:
        with self.db.get_session() as session:
            row = session.execute(
                select(ChipDistributionSnapshotRecord)
                .where(
                    ChipDistributionSnapshotRecord.symbol == symbol,
                    ChipDistributionSnapshotRecord.trade_date <= as_of.date(),
                    ChipDistributionSnapshotRecord.observed_at <= as_of,
                )
                .order_by(
                    desc(ChipDistributionSnapshotRecord.trade_date),
                    desc(ChipDistributionSnapshotRecord.observed_at),
                )
                .limit(1)
            ).scalar_one_or_none()
            return ChipSnapshot.model_validate_json(row.normalized_json) if row else None

    @staticmethod
    def _save_market_rules(session, artifact: PricePlanArtifact) -> None:
        existing = session.execute(
            select(MarketRuleSnapshotRecord)
            .where(
                MarketRuleSnapshotRecord.snapshot_id
                == artifact.market_rules_snapshot_ref
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return
        # The full rules object is persisted by snapshot/runtime before this
        # point when available. Keep an immutable reference row for readiness.
        session.add(
            MarketRuleSnapshotRecord(
                snapshot_id=artifact.market_rules_snapshot_ref,
                symbol=artifact.symbol,
                trade_date=artifact.actual_as_of_session,
                venue="cn",
                board="unknown",
                snapshot_json=json.dumps(
                    {
                        "snapshot_id": artifact.market_rules_snapshot_ref,
                        "artifact_plan_id": artifact.plan_id,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    @staticmethod
    def _link_trace(session, plan_id: str, trace_ref: str | None) -> None:
        normalized = str(trace_ref or "").strip()
        if not normalized:
            return
        existing = session.execute(
            select(TradePlanTraceLink)
            .where(
                TradePlanTraceLink.trace_ref == normalized,
                TradePlanTraceLink.plan_id == plan_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            session.add(TradePlanTraceLink(trace_ref=normalized, plan_id=plan_id))


def _lifecycle_state(row: PricePlanLifecycleStateRecord) -> PlanLifecycleState:
    return PlanLifecycleState(
        plan_id=row.plan_id,
        scenario_id=row.scenario_id,
        state_version=row.state_version,
        state=row.state,
        first_touched_at=row.first_touched_at,
        confirmed_at=row.confirmed_at,
        invalidated_at=row.invalidated_at,
        expired_at=row.expired_at,
        last_evaluated_as_of=row.last_evaluated_as_of,
        last_event_id=row.last_event_id,
    )


def _naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _bar_date(value: Any):
    raw = value.get("date") if isinstance(value, dict) else getattr(value, "date", None)
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, type(datetime.now().date())):
        return raw
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except (TypeError, ValueError):
        return None
    PlanReviewArtifactRecord,
