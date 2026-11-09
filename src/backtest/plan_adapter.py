# -*- coding: utf-8 -*-
"""Adapt immutable structured price artifacts into executable replay plans.

Only machine-readable artifacts + account decisions + explicit selection links
are accepted.  Legacy natural-language extraction is deliberately excluded from
P0 and remains a diagnostic concern.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.schemas import (
    AccountPlanDecision,
    GenerationStatus,
    PricePlanArtifact,
    ScenarioRole,
    ZoneScenario,
)
from src.storage import DatabaseManager, TradePlanTraceLink

from .calendar import CnTradingCalendar, as_shanghai_naive
from .schemas import (
    ExperimentConfig,
    PlanInputRecord,
    ReplayPlan,
    ScenarioSelection,
    SelectionLink,
    SourcePipeline,
)

logger = logging.getLogger(__name__)

ALLOWED_DECISION_STATUSES = {"actionable", "waiting_confirmation"}


class PlanAdapterError(RuntimeError):
    pass


@dataclass
class PlanAdaptationResult:
    plans: list[ReplayPlan]
    inputs: list[PlanInputRecord]
    diagnostics: dict[str, Any]
    quality_flags: list[str] = field(default_factory=list)

    @property
    def included_count(self) -> int:
        return sum(1 for item in self.inputs if item.included)

    @property
    def excluded_count(self) -> int:
        return sum(1 for item in self.inputs if not item.included)


class PlanAdapter:
    def __init__(
        self,
        *,
        config: ExperimentConfig,
        db_manager: DatabaseManager | None = None,
        calendar: CnTradingCalendar | None = None,
        price_plans: PricePlanRepository | None = None,
        artifacts: Sequence[PricePlanArtifact] | None = None,
        decisions: dict[str, AccountPlanDecision | dict[str, Any] | None] | None = None,
        trace_links: dict[str, list[str]] | None = None,
        trace_metadata: dict[str, dict[str, Any]] | None = None,
        selection_links: Sequence[SelectionLink] | None = None,
        trace_root: Path | None = None,
    ) -> None:
        self.config = config
        self.db = db_manager
        self.calendar = calendar or CnTradingCalendar()
        self.price_plans = price_plans or (PricePlanRepository(db_manager) if db_manager else None)
        self._artifacts = list(artifacts) if artifacts is not None else None
        self._decisions = decisions
        self._trace_links = trace_links
        self._trace_metadata = trace_metadata
        self._selection_links = list(selection_links) if selection_links is not None else list(config.selection_links)
        self.trace_root = trace_root

    def adapt(self) -> PlanAdaptationResult:
        artifacts = self._load_artifacts()
        decisions = self._load_decisions(artifacts)
        trace_links = self._load_trace_links(artifacts)
        link_index = self._build_link_index()
        inputs: list[PlanInputRecord] = []
        plans: list[ReplayPlan] = []
        quality_flags: set[str] = set()
        reason_counts: dict[str, int] = {}
        dedup: dict[str, PricePlanArtifact] = {}

        for artifact in artifacts:
            dedup[artifact.plan_id] = artifact
        known_plan_ids = set(dedup)
        for plan_id in self.config.plan_ids:
            if plan_id not in known_plan_ids:
                inputs.append(
                    PlanInputRecord(
                        input_id=_input_id(plan_id, None, "plan_not_found"),
                        source_id=plan_id,
                        plan_id=plan_id,
                        included=False,
                        exclusion_reason="plan_not_found",
                    )
                )
                reason_counts["plan_not_found"] = reason_counts.get("plan_not_found", 0) + 1
        linked_trace_ids = {trace_id for refs in trace_links.values() for trace_id in refs}
        for trace_id in self.config.trace_ids:
            if trace_id not in linked_trace_ids:
                inputs.append(
                    PlanInputRecord(
                        input_id=_input_id(None, None, f"trace_not_found:{trace_id}"),
                        source_type="trace",
                        source_id=trace_id,
                        included=False,
                        exclusion_reason="trace_not_found",
                    )
                )
                reason_counts["trace_not_found"] = reason_counts.get("trace_not_found", 0) + 1
        for artifact in sorted(dedup.values(), key=lambda item: (item.actual_as_of_session, item.plan_id)):
            plan, record, flags = self._adapt_one(
                artifact,
                decision=decisions.get(artifact.plan_id),
                trace_refs=sorted(trace_links.get(artifact.plan_id, [])),
                link_index=link_index,
            )
            quality_flags.update(flags)
            inputs.append(record)
            if plan is not None:
                plans.append(plan)
            else:
                reason_counts[record.exclusion_reason or "excluded"] = (
                    reason_counts.get(record.exclusion_reason or "excluded", 0) + 1
                )

        plans, top_excluded = self._apply_selection_top_n(plans)
        dropped_keys = {(plan.plan_id, plan.scenario_id) for plan in top_excluded}
        for index, record in enumerate(inputs):
            if (record.plan_id, record.scenario_id) in dropped_keys and record.included:
                inputs[index] = record.model_copy(
                    update={
                        "included": False,
                        "exclusion_reason": "selection_top_n_exceeded",
                    }
                )
                reason_counts["selection_top_n_exceeded"] = (
                    reason_counts.get("selection_top_n_exceeded", 0) + 1
                )

        plans.sort(key=lambda item: (item.available_at, item.rank, item.plan_key))
        diagnostics = {
            "artifacts_considered": len(dedup),
            "included": len(plans),
            "excluded": len([item for item in inputs if not item.included]),
            "exclusion_reasons": dict(sorted(reason_counts.items())),
            "source_pipeline": self.config.source_pipeline.value,
            "selection_links": len(self._selection_links),
            "selection_required": self.config.source_pipeline is SourcePipeline.SELECTION_CONFIRMED,
        }
        return PlanAdaptationResult(
            plans=plans,
            inputs=inputs,
            diagnostics=diagnostics,
            quality_flags=sorted(quality_flags),
        )

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    def _load_artifacts(self) -> list[PricePlanArtifact]:
        if self._artifacts is not None:
            return self._artifacts
        if self.price_plans is None:
            return []
        artifacts: list[PricePlanArtifact] = []
        if self.config.plan_ids:
            for plan_id in self.config.plan_ids:
                artifact = self.price_plans.get_artifact(plan_id)
                if artifact is not None:
                    artifacts.append(artifact)
        elif self.config.trace_ids:
            for trace_id in self.config.trace_ids:
                artifacts.extend(self.price_plans.list_artifacts_by_trace(trace_id))
        else:
            artifacts = self.price_plans.list_artifacts(limit=2000)
        return artifacts

    def _load_decisions(self, artifacts: Iterable[PricePlanArtifact]) -> dict[str, Any]:
        if self._decisions is not None:
            return dict(self._decisions)
        result: dict[str, Any] = {}
        if self.price_plans is None:
            return result
        for artifact in artifacts:
            payload = self.price_plans.get_latest_account_decision(artifact.plan_id)
            result[artifact.plan_id] = payload
        return result

    def _load_trace_links(self, artifacts: Iterable[PricePlanArtifact]) -> dict[str, list[str]]:
        if self._trace_links is not None:
            return {key: list(value) for key, value in self._trace_links.items()}
        if self.db is None:
            return {}
        plan_ids = [artifact.plan_id for artifact in artifacts]
        if not plan_ids:
            return {}
        with self.db.get_session() as session:
            rows = session.execute(
                select(TradePlanTraceLink).where(TradePlanTraceLink.plan_id.in_(plan_ids))
            ).scalars().all()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row.plan_id, []).append(row.trace_ref)
        return result

    def _build_link_index(self) -> dict[tuple[str, str], SelectionLink]:
        result: dict[tuple[str, str], SelectionLink] = {}
        for link in self._selection_links:
            result[(link.confirmation_trace_ref, _normalize_symbol(link.symbol))] = link
        return result

    # ------------------------------------------------------------------
    # One artifact
    # ------------------------------------------------------------------
    def _adapt_one(
        self,
        artifact: PricePlanArtifact,
        *,
        decision: AccountPlanDecision | dict[str, Any] | None,
        trace_refs: list[str],
        link_index: dict[tuple[str, str], SelectionLink],
    ) -> tuple[ReplayPlan | None, PlanInputRecord, set[str]]:
        symbol = _normalize_symbol(artifact.symbol)
        flags: set[str] = set()
        available_at, inferred = _artifact_available_at(artifact)
        if inferred:
            flags.add("timestamp_inferred")
        base = PlanInputRecord(
            input_id=_input_id(artifact.plan_id, None, "artifact"),
            source_id=artifact.plan_id,
            plan_id=artifact.plan_id,
            revision=artifact.revision,
            trace_refs=trace_refs,
            symbol=symbol,
            available_at=available_at,
            valid_from=None,
            included=False,
        )
        if artifact.market != "cn":
            return None, base.model_copy(update={"exclusion_reason": "unsupported_market"}), flags
        if artifact.generation_status is not GenerationStatus.GENERATED:
            reason = "observation_only" if artifact.generation_status is GenerationStatus.OBSERVATION else "no_formal_plan"
            return None, base.model_copy(update={"exclusion_reason": reason}), flags
        if artifact.data_quality == "invalid":
            return None, base.model_copy(update={"exclusion_reason": "invalid_data_quality"}), flags
        if not artifact.scenarios:
            return None, base.model_copy(update={"exclusion_reason": "no_scenarios"}), flags

        parsed_decision: AccountPlanDecision | None = None
        if decision is not None:
            try:
                parsed_decision = (
                    decision
                    if isinstance(decision, AccountPlanDecision)
                    else AccountPlanDecision.model_validate(decision)
                )
            except Exception:
                parsed_decision = None
        if parsed_decision is None and self.config.require_account_decision:
            return None, base.model_copy(update={"exclusion_reason": "missing_account_decision"}), flags
        if parsed_decision is not None:
            if parsed_decision.execution_status not in ALLOWED_DECISION_STATUSES:
                return (
                    None,
                    base.model_copy(
                        update={
                            "decision_id": parsed_decision.decision_id,
                            "exclusion_reason": f"decision_status:{parsed_decision.execution_status}",
                        }
                    ),
                    flags,
                )
        scenario = self._select_scenario(artifact, parsed_decision)
        if scenario is None:
            return (
                None,
                base.model_copy(
                    update={
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "exclusion_reason": "selected_scenario_unavailable",
                    }
                ),
                flags,
            )
        if scenario.role is ScenarioRole.OBSERVATION and not self.config.include_observation_scenarios:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "exclusion_reason": "observation_scenario",
                    }
                ),
                flags,
            )
        if scenario.stop_loss is None or scenario.target_1 is None:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "exclusion_reason": "missing_machine_risk_levels",
                    }
                ),
                flags,
            )
        confirmation_ref = _select_confirmation_ref(artifact, trace_refs)
        selection_link = None
        if confirmation_ref:
            selection_link = link_index.get((confirmation_ref, symbol))
        if selection_link is None:
            # Also accept a link keyed by any linked trace, not only the artifact's
            # primary trace_ref, as long as it is explicit config input.
            for ref in trace_refs:
                selection_link = link_index.get((ref, symbol))
                if selection_link is not None:
                    confirmation_ref = ref
                    break
        if selection_link is not None and selection_link.selection_trace_ref == selection_link.confirmation_trace_ref:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "selection_trace_ref": selection_link.selection_trace_ref,
                        "confirmation_trace_ref": selection_link.confirmation_trace_ref,
                        "link_quality": "explicit",
                        "exclusion_reason": "invalid_selection_link",
                    }
                ),
                flags,
            )
        if self.config.source_pipeline is SourcePipeline.SELECTION_CONFIRMED and selection_link is None:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "confirmation_trace_ref": confirmation_ref,
                        "link_quality": "missing",
                        "exclusion_reason": "missing_selection_link",
                    }
                ),
                flags | {"selection_link_missing"},
            )
        if self.config.source_pipeline is SourcePipeline.SINGLE_STOCK and selection_link is not None:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "decision_id": parsed_decision.decision_id if parsed_decision else None,
                        "selection_trace_ref": selection_link.selection_trace_ref,
                        "confirmation_trace_ref": selection_link.confirmation_trace_ref,
                        "link_quality": "explicit",
                        "exclusion_reason": "belongs_to_selection_pipeline",
                    }
                ),
                flags,
            )
        pipeline = (
            SourcePipeline.SELECTION_CONFIRMED
            if selection_link is not None
            else SourcePipeline.SINGLE_STOCK
        )
        link_quality = "explicit" if selection_link is not None else ("single_stock" if confirmation_ref else "missing")
        if pipeline is SourcePipeline.SINGLE_STOCK and not confirmation_ref:
            flags.add("missing_confirmation_trace")
        valid_from = max(scenario.valid_from, self.calendar.first_session_after_datetime(available_at))
        valid_until = max(scenario.confirmation_deadline, _add_sessions(self.calendar, valid_from, scenario.order_valid_trading_days - 1))
        if valid_until < valid_from:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "valid_from": valid_from,
                        "confirmation_trace_ref": confirmation_ref,
                        "link_quality": link_quality,
                        "exclusion_reason": "invalid_plan_window",
                    }
                ),
                flags,
            )
        if valid_until < self.config.start_date or valid_from > self.config.end_date:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "valid_from": valid_from,
                        "confirmation_trace_ref": confirmation_ref,
                        "link_quality": link_quality,
                        "exclusion_reason": "outside_experiment_window",
                    }
                ),
                flags,
            )
        if self.config.symbols and symbol not in {_normalize_symbol(item) for item in self.config.symbols}:
            return (
                None,
                base.model_copy(
                    update={
                        "scenario_id": scenario.scenario_id,
                        "valid_from": valid_from,
                        "exclusion_reason": "symbol_filtered",
                    }
                ),
                flags,
            )
        target_pct = (
            parsed_decision.target_position_pct
            if parsed_decision is not None and parsed_decision.target_position_pct is not None
            else self.config.default_target_position_pct
        )
        if parsed_decision is None or parsed_decision.target_position_pct is None:
            flags.add("position_pct_defaulted")
        requires_confirmation = (
            parsed_decision is not None and parsed_decision.execution_status == "waiting_confirmation"
        ) or bool(scenario.confirmation_rules)
        # An actionable decision has already passed its confirmation; do not
        # re-run confirmation and delay entry.
        if parsed_decision is not None and parsed_decision.execution_status == "actionable":
            requires_confirmation = False
        plan_key = f"{artifact.plan_id}:{scenario.scenario_id}"
        plan = ReplayPlan(
            plan_key=plan_key,
            plan_id=artifact.plan_id,
            source_pipeline=pipeline,
            selection_trace_ref=selection_link.selection_trace_ref if selection_link else None,
            confirmation_trace_ref=confirmation_ref,
            link_quality=link_quality,
            source_revision=artifact.revision,
            source_hash=artifact.plan_fingerprint,
            trace_refs=trace_refs,
            symbol=symbol,
            decision_id=parsed_decision.decision_id if parsed_decision else None,
            decision_status=parsed_decision.execution_status if parsed_decision else "",
            scenario_id=scenario.scenario_id,
            scenario_role=scenario.role.value,
            available_at=available_at,
            available_at_inferred=inferred,
            valid_from=valid_from,
            valid_until=valid_until,
            entry_low=scenario.entry_low,
            entry_high=scenario.entry_high,
            entry_reference=scenario.entry_reference,
            stop_loss=scenario.stop_loss,
            target_1=scenario.target_1,
            target_2=scenario.target_2,
            target_1_exit_ratio=scenario.target_1_exit_ratio,
            order_valid_trading_days=scenario.order_valid_trading_days,
            max_hold_trading_days=self.config.max_hold_trading_days,
            target_position_pct=target_pct,
            rank=scenario.base_rank,
            requires_confirmation=requires_confirmation,
            confirmation_valid_trading_days=scenario.confirmation_valid_trading_days,
            confirmation_deadline=scenario.confirmation_deadline,
            scenario_json=scenario.model_dump(mode="json"),
            quality_flags=sorted(flags),
        )
        record = base.model_copy(
            update={
                "input_id": _input_id(artifact.plan_id, scenario.scenario_id, "included"),
                "source_pipeline": pipeline,
                "selection_trace_ref": plan.selection_trace_ref,
                "confirmation_trace_ref": plan.confirmation_trace_ref,
                "link_quality": link_quality,
                "scenario_id": scenario.scenario_id,
                "decision_id": plan.decision_id,
                "valid_from": valid_from,
                "included": True,
                "quality_flags": sorted(flags),
                "payload_hash": plan.fingerprint(),
            }
        )
        return plan, record, flags

    def _select_scenario(
        self,
        artifact: PricePlanArtifact,
        decision: AccountPlanDecision | None,
    ) -> ZoneScenario | None:
        if self.config.scenario_selection is ScenarioSelection.RANK_DIAGNOSTIC:
            candidates = sorted(artifact.scenarios, key=lambda item: (item.base_rank, item.scenario_id))
            return candidates[0] if candidates else None
        if decision is None or not decision.selected_scenario_id:
            return None
        selected = next(
            (item for item in artifact.scenarios if item.scenario_id == decision.selected_scenario_id),
            None,
        )
        if selected is None:
            return None
        if selected.role is ScenarioRole.OBSERVATION and not self.config.include_observation_scenarios:
            return selected
        return selected

    def _apply_selection_top_n(
        self, plans: list[ReplayPlan]
    ) -> tuple[list[ReplayPlan], list[ReplayPlan]]:
        top_n = self.config.risk_policy.selection_top_n if self.config.risk_policy else None
        if not top_n:
            return plans, []
        grouped: dict[str, list[ReplayPlan]] = {}
        singles: list[ReplayPlan] = []
        for plan in plans:
            if plan.selection_trace_ref:
                grouped.setdefault(plan.selection_trace_ref, []).append(plan)
            else:
                singles.append(plan)
        kept: list[ReplayPlan] = []
        dropped: list[ReplayPlan] = []
        for group in grouped.values():
            ordered = sorted(group, key=lambda item: (item.rank, item.available_at, item.plan_key))
            kept.extend(ordered[:top_n])
            dropped.extend(ordered[top_n:])
        kept.extend(singles)
        return kept, dropped


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _artifact_available_at(artifact: PricePlanArtifact) -> tuple[datetime, bool]:
    created = artifact.created_at
    if created is None:
        return datetime.combine(artifact.actual_as_of_session, time(15, 0)), True
    normalized = as_shanghai_naive(created)
    if created.tzinfo is None:
        # Persisted naive timestamps are local Asia/Shanghai wall time.
        return normalized, False
    return normalized, False


def _select_confirmation_ref(artifact: PricePlanArtifact, trace_refs: list[str]) -> str | None:
    if artifact.trace_ref:
        return artifact.trace_ref
    return trace_refs[0] if trace_refs else None


def _add_sessions(calendar: CnTradingCalendar, start: date, count: int) -> date:
    if count <= 0:
        return start
    sessions = calendar.sessions(start, start + timedelta(days=max(90, count * 4)))
    if not sessions:
        return start
    index = min(count, len(sessions) - 1)
    return sessions[index]


def _normalize_symbol(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[-6:]
    return digits or str(value or "").strip().upper()


def _input_id(plan_id: str | None, scenario_id: str | None, suffix: str) -> str:
    seed = f"{plan_id or ''}|{scenario_id or ''}|{suffix}"
    return "input-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


__all__ = [
    "ALLOWED_DECISION_STATUSES",
    "PlanAdaptationResult",
    "PlanAdapter",
    "PlanAdapterError",
]
