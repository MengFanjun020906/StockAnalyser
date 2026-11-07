from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
from decimal import Decimal
import re
from typing import Any, Iterable

from .fingerprint import fingerprint, stable_id
from .account_cost import evaluate_account_cost
from .params import get_price_plan_params
from .schemas import (
    AccountPlanDecision,
    DimensionPlanReview,
    GenerationStatus,
    PlanLifecycleState,
    PlanReviewArtifact,
    PricePlanArtifact,
    PriceRelation,
    ReachabilityBand,
    ReviewConclusion,
    ScenarioReviewImpact,
    ScenarioRole,
    ZoneScenario,
)


REVIEW_PERMISSIONS = {
    ReviewConclusion.SUPPORT: ("allowed", Decimal("1.0")),
    ReviewConclusion.MIXED: ("conditional", Decimal("0.5")),
    ReviewConclusion.INSUFFICIENT: ("observe_only", Decimal("0")),
    ReviewConclusion.OPPOSING: ("observe_only", Decimal("0")),
    ReviewConclusion.HARD_VETO: ("vetoed", Decimal("0")),
}
RISK_PROFILE_POSITION_MULTIPLIERS = {
    "aggressive": Decimal("1.0"),
    "balanced": Decimal("0.8"),
    "conservative": Decimal("0.6"),
}
DIMENSION_EXECUTION_EFFECTS = {
    ReviewConclusion.SUPPORT: "keep",
    ReviewConclusion.MIXED: "reduce_position",
    ReviewConclusion.INSUFFICIENT: "observe_only",
    ReviewConclusion.OPPOSING: "observe_only",
    ReviewConclusion.HARD_VETO: "veto",
}
_DIMENSION_ALIASES = {
    "technical": "technical",
    "capital": "capital",
    "capital_flow": "capital",
    "fundamental": "fundamental",
    "intel": "intel",
    "news": "intel",
    "news_event": "intel",
}
_REVIEW_SEVERITY = {
    ReviewConclusion.SUPPORT: 0,
    ReviewConclusion.MIXED: 1,
    ReviewConclusion.INSUFFICIENT: 2,
    ReviewConclusion.OPPOSING: 2,
    ReviewConclusion.HARD_VETO: 3,
}
_EFFECT_SEVERITY = {
    "keep": 0,
    "reduce_position": 1,
    "observe_only": 2,
    "veto": 3,
}
_PRICE_AUTHORITY_KEYS = frozenset(
    {
        "ideal_buy",
        "secondary_buy",
        "entry_price",
        "entry_zone",
        "entry_zone_low",
        "entry_zone_high",
        "stop_loss",
        "stop_loss_price",
        "take_profit",
        "take_profit_price",
        "target_price",
        "target_1",
        "target_2",
    }
)
_PRICE_AUTHORITY_TEXT = re.compile(
    r"(?:理想入场|次优入场|入场(?:价|区间|点位)|买入(?:价|区间|点位)|止损(?:价|位)?|"
    r"(?:第一|第二)?目标(?:价|位)?|ideal\s+buy|secondary\s+buy|entry\s+(?:price|zone)|"
    r"stop[ _-]?loss|take[ _-]?profit|target[ _-]?[12]?)"
    r"[^\n\r]{0,48}?\d+(?:\.\d+)?(?![\d.]|\s*%)",
    flags=re.IGNORECASE,
)
_DETERMINISTIC_PRICE_BLOCK = re.compile(
    r"<!-- deterministic-price-plan:start -->.*?<!-- deterministic-price-plan:end -->",
    flags=re.DOTALL,
)


def build_review_artifact(
    artifact: PricePlanArtifact,
    *,
    result: Any,
    tool_calls: Iterable[dict[str, Any]],
) -> PlanReviewArtifact | None:
    """Freeze existing Agent evidence as a price-less structured review."""
    if artifact.generation_status is GenerationStatus.NO_PLAN:
        return None
    evidence = _review_evidence(tool_calls)
    action = _extract_action(result)
    price_fields_present = _has_unauthorized_price_fields(result)
    failed_count = sum(not item["success"] for item in evidence)
    available_dimensions = _available_dimensions(evidence)
    dimension_reviews = _build_dimension_reviews(
        artifact,
        result=result,
        tool_calls=tool_calls,
        evidence=evidence,
        price_fields_present=price_fields_present,
    )
    explicit_dimension_review = any(
        "MODEL_PLAN_VERDICT" in item.reason_codes for item in dimension_reviews
    )
    stock_reviews = [
        item
        for item in dimension_reviews
        if item.scope == "stock" and "MODEL_PLAN_VERDICT" in item.reason_codes
    ]
    debate = getattr(result, "debate", None)
    debate_failed = bool(
        isinstance(debate, dict)
        and debate.get("enabled")
        and debate.get("success") is False
    )
    if price_fields_present and not explicit_dimension_review:
        conclusion = ReviewConclusion.INSUFFICIENT
    elif debate_failed:
        conclusion = ReviewConclusion.INSUFFICIENT
    elif isinstance(debate, dict) and debate.get("success") is True and not explicit_dimension_review:
        conclusion = ReviewConclusion.INSUFFICIENT
    elif explicit_dimension_review and stock_reviews:
        conclusion = max(
            (item.verdict for item in stock_reviews),
            key=lambda item: _REVIEW_SEVERITY[item],
        )
    elif explicit_dimension_review:
        # Scenario-only verdicts cannot establish a stock-wide support conclusion.
        conclusion = ReviewConclusion.MIXED
    else:
        conclusion = ReviewConclusion.INSUFFICIENT
    scenario_impacts = _build_scenario_impacts(
        artifact,
        dimension_reviews=dimension_reviews,
    )
    evidence_bundle_fingerprint = fingerprint(evidence)
    model_ref = "/".join(
        item
        for item in (
            str(getattr(result, "provider", "") or "").strip(),
            str(getattr(result, "model", "") or "").strip(),
        )
        if item
    ) or "existing-agent-result"
    validated_output = {
        "action": action or "unknown",
        "available_dimensions": available_dimensions,
        "failed_tool_count": failed_count,
        "price_fields_present": price_fields_present,
        "authority_violation": "llm_price_fields" if price_fields_present else None,
        "price_authority_action": "strip_price_fields_keep_dimension_reviews"
        if price_fields_present
        else None,
        "dimension_review_policy": "dimension-plan-review.v1",
        "policy_version": "review-policy.v1",
        "review_error": str(debate.get("error") or "judge_review_failed")
        if debate_failed
        else None,
    }
    review_fingerprint = fingerprint(
        {
            "plan_fingerprint": artifact.plan_fingerprint,
            "evidence_bundle_fingerprint": evidence_bundle_fingerprint,
            "prompt_version": "existing-agent-review-adapter.v2",
            "model_ref": model_ref,
            "conclusion": conclusion,
            "validated_output": validated_output,
            "dimension_reviews": [item.model_dump(mode="json") for item in dimension_reviews],
            "scenario_impacts": [item.model_dump(mode="json") for item in scenario_impacts],
        }
    )
    return PlanReviewArtifact(
        review_id=stable_id(
            "review",
            {"plan_id": artifact.plan_id, "review_fingerprint": review_fingerprint},
            length=32,
        ),
        plan_id=artifact.plan_id,
        evidence_bundle_fingerprint=evidence_bundle_fingerprint,
        prompt_version="existing-agent-review-adapter.v2",
        model_ref=model_ref,
        conclusion=conclusion,
        evidence_refs=[item["ref"] for item in evidence if item["success"]],
        validated_output=validated_output,
        dimension_reviews=dimension_reviews,
        scenario_impacts=scenario_impacts,
        review_status=(
            "failed"
            if debate_failed or (price_fields_present and not explicit_dimension_review)
            else ("completed" if explicit_dimension_review else "pending")
        ),
        review_fingerprint=review_fingerprint,
        created_at=datetime.now(timezone.utc),
    )


def _build_dimension_reviews(
    artifact: PricePlanArtifact,
    *,
    result: Any,
    tool_calls: Iterable[dict[str, Any]],
    evidence: list[dict[str, Any]],
    price_fields_present: bool,
) -> list[DimensionPlanReview]:
    from src.agent.dimension_evidence import build_dimension_evidence_summary

    coverage = build_dimension_evidence_summary(tool_calls)
    assessments = _plan_dimension_assessments(result)
    debate = getattr(result, "debate", None)
    debate_failed = bool(
        isinstance(debate, dict)
        and debate.get("enabled")
        and debate.get("success") is False
    )
    all_scenario_ids = [item.scenario_id for item in artifact.scenarios]
    output: list[DimensionPlanReview] = []
    for dimension in ("technical", "capital", "fundamental", "intel"):
        quality_payload = coverage.get(dimension) or {}
        quality = str(quality_payload.get("quality") or "unavailable")
        if quality not in {"ok", "partial", "unavailable"}:
            quality = "unavailable"
        dimension_assessments = [
            item for item_dimension, item in assessments if item_dimension == dimension
        ]
        if not dimension_assessments:
            dimension_assessments = [None]
        for assessment in dimension_assessments:
            output.append(
                _build_dimension_review(
                    dimension=dimension,
                    assessment=assessment,
                    quality_payload=quality_payload,
                    quality=quality,
                    evidence=evidence,
                    price_fields_present=price_fields_present,
                    all_scenario_ids=all_scenario_ids,
                    review_status="failed" if debate_failed else "not_run",
                )
            )
    return output


def _build_dimension_review(
    *,
    dimension: str,
    assessment: dict[str, Any] | None,
    quality_payload: dict[str, Any],
    quality: str,
    evidence: list[dict[str, Any]],
    price_fields_present: bool,
    all_scenario_ids: list[str],
    review_status: str,
) -> DimensionPlanReview:
    explicit = assessment is not None
    raw_verdict = str((assessment or {}).get("plan_verdict") or "insufficient").lower()
    try:
        verdict = ReviewConclusion(raw_verdict)
    except ValueError:
        verdict = ReviewConclusion.INSUFFICIENT
        explicit = False
    has_verified_ref = _has_verified_dimension_ref(
        assessment or {},
        evidence=evidence,
        dimension=dimension,
    )
    if explicit and quality == "unavailable":
        verdict = ReviewConclusion.INSUFFICIENT
        explicit = False
        reason_codes = ["PLAN_VERDICT_EVIDENCE_UNAVAILABLE"]
    elif explicit and verdict is not ReviewConclusion.INSUFFICIENT and not has_verified_ref:
        verdict = (
            ReviewConclusion.OPPOSING
            if verdict is ReviewConclusion.HARD_VETO
            else ReviewConclusion.INSUFFICIENT
        )
        explicit = False
        reason_codes = ["PLAN_VERDICT_EVIDENCE_REF_UNVERIFIED"]
    elif verdict is ReviewConclusion.SUPPORT and quality == "partial":
        verdict = ReviewConclusion.MIXED
        reason_codes = ["PLAN_VERDICT_EVIDENCE_PARTIAL"]
    else:
        reason_codes = [
            str(item)
            for item in ((assessment or {}).get("reason_codes") or [])
            if str(item).strip()
        ]
    summary = str((assessment or {}).get("summary") or "").strip()
    if not summary:
        summary = {
            "ok": "已取得该维度证据，但模型未给出对价格计划的明确方向。",
            "partial": "该维度证据不完整，不能据此提高执行强度。",
            "unavailable": "该维度缺少可用证据，不能据此提高执行强度。",
        }[quality]
    requested_scenarios = (assessment or {}).get("scenario_ids")
    scenario_scope = isinstance(requested_scenarios, list) and bool(requested_scenarios)
    scenario_ids = (
        [str(item) for item in requested_scenarios if str(item) in all_scenario_ids]
        if isinstance(requested_scenarios, list) and requested_scenarios
        else all_scenario_ids
    )
    if scenario_scope and not scenario_ids:
        reason_codes.append("SCENARIO_REF_INVALID")
    evidence_refs = [
        item["ref"]
        for item in evidence
        if item["success"] and item.get("dimension") == dimension
    ]
    supplied_refs = (assessment or {}).get("evidence_refs")
    if isinstance(supplied_refs, list):
        evidence_refs = list(
            dict.fromkeys(
                [
                    *evidence_refs,
                    *(str(item) for item in supplied_refs if str(item).strip()),
                ]
            )
        )
    if explicit:
        reason_codes.insert(0, "MODEL_PLAN_VERDICT")
        review_status = "completed"
    else:
        reason_codes.insert(0, "PLAN_VERDICT_UNAVAILABLE")
    if price_fields_present and dimension == "technical":
        reason_codes.insert(0, "LLM_PRICE_AUTHORITY_VIOLATION")
    missing = list(quality_payload.get("missing_evidence") or [])
    supplied_missing = (assessment or {}).get("missing")
    if isinstance(supplied_missing, list):
        missing = list(
            dict.fromkeys(
                [
                    *missing,
                    *(str(item) for item in supplied_missing if str(item).strip()),
                ]
            )
        )
    return DimensionPlanReview(
        dimension=dimension,
        scope="scenario" if scenario_scope else "stock",
        verdict=verdict,
        evidence_quality=quality,
        review_status=review_status,
        execution_effect=DIMENSION_EXECUTION_EFFECTS[verdict],
        summary=(
            "Judge 结构化评审失败，当前只保留该维度的数据完整度，不能形成价格计划方向。"
            if review_status == "failed"
            else summary
        ),
        reason_codes=list(dict.fromkeys(reason_codes)),
        evidence_refs=evidence_refs,
        missing_evidence=missing,
        scenario_ids=scenario_ids,
    )


def _has_verified_dimension_ref(
    assessment: dict[str, Any],
    *,
    evidence: list[dict[str, Any]],
    dimension: str,
) -> bool:
    supplied = {
        str(item).strip()
        for item in assessment.get("evidence_refs") or []
        if str(item).strip()
    }
    if not supplied:
        return False
    valid: set[str] = set()
    for index, item in enumerate(evidence):
        if not item["success"] or item.get("dimension") != dimension:
            continue
        tool = str(item.get("tool") or "unknown")
        valid.add(str(item.get("ref") or ""))
        valid.add(f"tool:{index + 1}:{tool}")
    return bool(supplied & valid)


def _plan_dimension_assessments(result: Any) -> list[tuple[str, dict[str, Any]]]:
    debate = getattr(result, "debate", None)
    if not isinstance(debate, dict) or debate.get("success") is not True:
        return []
    judge = debate.get("judge_decision")
    if not isinstance(judge, dict):
        return []
    output: list[tuple[str, dict[str, Any]]] = []
    for item in judge.get("dimension_assessments") or []:
        if not isinstance(item, dict):
            continue
        dimension = _DIMENSION_ALIASES.get(str(item.get("dimension") or "").lower())
        plan_verdict = str(item.get("plan_verdict") or "").lower()
        if dimension is None or plan_verdict not in {item.value for item in ReviewConclusion}:
            continue
        output.append((dimension, item))
    return output


def _build_scenario_impacts(
    artifact: PricePlanArtifact,
    *,
    dimension_reviews: list[DimensionPlanReview],
) -> list[ScenarioReviewImpact]:
    output: list[ScenarioReviewImpact] = []
    for scenario in artifact.scenarios:
        math_effect = (
            "observe_only" if scenario.role is ScenarioRole.OBSERVATION else "keep"
        )
        reviews = [
            item for item in dimension_reviews if scenario.scenario_id in item.scenario_ids
        ]
        explicit_reviews = [
            item for item in reviews if "MODEL_PLAN_VERDICT" in item.reason_codes
        ]
        effects = [math_effect, *(item.execution_effect for item in explicit_reviews)]
        covered_dimensions = {item.dimension for item in explicit_reviews}
        missing_dimensions = [
            item
            for item in ("technical", "capital", "fundamental", "intel")
            if item not in covered_dimensions
        ]
        if missing_dimensions:
            effects.append("observe_only")
        final_effect = max(effects, key=lambda item: _EFFECT_SEVERITY[item])
        reason_codes = list(scenario.scenario_reason_codes)
        reason_codes.extend(
            code
            for item in reviews
            for code in item.reason_codes
            if code != "MODEL_PLAN_VERDICT"
        )
        reason_codes.extend(
            f"PLAN_VERDICT_UNAVAILABLE:{dimension}"
            for dimension in missing_dimensions
        )
        output.append(
            ScenarioReviewImpact(
                scenario_id=scenario.scenario_id,
                math_effect=math_effect,
                applicable_dimensions=list(dict.fromkeys(item.dimension for item in reviews)),
                final_effect=final_effect,
                reason_codes=list(dict.fromkeys(reason_codes)),
            )
        )
    return output


def build_account_decision(
    artifact: PricePlanArtifact,
    *,
    review: PlanReviewArtifact | None,
    lifecycle_states: list[PlanLifecycleState],
    risk_profile: str,
    position_sizing: dict[str, Any] | None = None,
    account_position_cap_pct: Decimal | None = None,
    account_execution: dict[str, Any] | None = None,
    lot_size: int = 100,
    decision_scope: str = "request",
) -> AccountPlanDecision:
    """Select one scenario without changing its mathematical prices."""
    normalized_profile = (
        risk_profile if risk_profile in {"aggressive", "balanced", "conservative"} else "balanced"
    )
    candidates = [
        item
        for item in artifact.scenarios
        if item.role is not ScenarioRole.OBSERVATION
        and item.price_relation in {PriceRelation.ABOVE, PriceRelation.INSIDE}
        and item.stop_loss is not None
        and item.target_1 is not None
        and item.reference_net_rr_1 is not None
        and item.reference_net_rr_1 > 0
    ]
    selected = sorted(candidates, key=lambda item: _scenario_sort_key(item, normalized_profile))[0] if candidates else None
    conclusion = review.conclusion if review is not None else ReviewConclusion.INSUFFICIENT
    if review is not None and selected is not None:
        impact = next(
            (
                item
                for item in review.scenario_impacts
                if item.scenario_id == selected.scenario_id
            ),
            None,
        )
        if impact is not None:
            conclusion = {
                "keep": ReviewConclusion.SUPPORT,
                "reduce_position": ReviewConclusion.MIXED,
                "observe_only": ReviewConclusion.OPPOSING,
                "veto": ReviewConclusion.HARD_VETO,
            }[impact.final_effect]
    permission, review_multiplier = REVIEW_PERMISSIONS[conclusion]
    state = next(
        (item for item in lifecycle_states if selected and item.scenario_id == selected.scenario_id),
        None,
    )
    position_cap, position_sizing_ref = _position_cap(
        position_sizing,
        risk_profile=normalized_profile,
        review_multiplier=review_multiplier,
        account_position_cap_pct=account_position_cap_pct,
    )
    params = get_price_plan_params(artifact.params_version)
    account_cost = evaluate_account_cost(
        selected,
        target_position_pct=position_cap,
        lot_size=lot_size,
        account=account_execution,
        minimum_rr=(
            params.min_reference_net_rr_1
            if params is not None
            else Decimal("1.20")
        ),
    )
    execution_status, reasons = _execution_status(
        artifact,
        selected=selected,
        state=state,
        review=review,
        permission=permission,
        risk_profile=normalized_profile,
        target_position_pct=position_cap,
        account_cost_status=account_cost.status,
        account_cost_reason=account_cost.reason_code,
    )
    input_payload = {
        "plan_fingerprint": artifact.plan_fingerprint,
        "review_fingerprint": review.review_fingerprint if review else None,
        "risk_profile": normalized_profile,
        "decision_scope": decision_scope,
        "selected_scenario_id": selected.scenario_id if selected else None,
        "lifecycle_state": state.model_dump(mode="json") if state else None,
        "position_sizing": position_sizing,
        "account_position_cap_pct": account_position_cap_pct,
        "target_position_pct": position_cap,
        "account_cost": asdict(account_cost),
        "policy_version": "account-plan-decision.v1",
    }
    input_fingerprint = fingerprint(input_payload)
    return AccountPlanDecision(
        decision_id=stable_id(
            "decision",
            {"plan_id": artifact.plan_id, "input_fingerprint": input_fingerprint},
            length=32,
        ),
        plan_id=artifact.plan_id,
        decision_scope=decision_scope,
        risk_profile=normalized_profile,  # type: ignore[arg-type]
        selected_scenario_id=selected.scenario_id if selected else None,
        review_permission=permission,
        confirmation_policy=(
            "two_closed_sessions" if normalized_profile == "conservative" or permission == "conditional"
            else "base_confirmation"
        ),
        position_sizing_ref=position_sizing_ref,
        target_position_pct=position_cap,
        account_net_rr_1=account_cost.account_net_rr_1,
        planned_quantity=account_cost.quantity,
        account_cost_policy_ref=account_cost.policy_ref,
        account_context_absent=account_cost.status == "account_absent",
        execution_status=execution_status,
        reason_codes=reasons,
        input_fingerprint=input_fingerprint,
        created_at=datetime.now(timezone.utc),
    )


def finalize_account_decision(
    decision: AccountPlanDecision,
    *,
    risk_gate_result: Any,
) -> AccountPlanDecision:
    """Create the immutable post-gate decision without changing math prices."""
    gate_payload = (
        risk_gate_result.model_dump(mode="json")
        if hasattr(risk_gate_result, "model_dump")
        else dict(risk_gate_result or {})
    )
    gate_fingerprint = fingerprint(gate_payload)
    pre_gate_ready = (
        decision.execution_status == "risk_blocked"
        and decision.reason_codes == ["RISK_GATE_PENDING"]
        and decision.target_position_pct is not None
        and decision.target_position_pct > 0
    )
    allowed_action = str(gate_payload.get("allowed_action") or "")
    gate_status = str(gate_payload.get("status") or "")
    actionable = (
        pre_gate_ready
        and allowed_action in {"open", "add"}
        and gate_status in {"passed", "downgraded"}
    )
    execution_status = "actionable" if actionable else decision.execution_status
    reason_codes = [] if actionable else list(decision.reason_codes)
    if pre_gate_ready and not actionable:
        execution_status = "risk_blocked"
        reason_codes = ["RISK_GATE_BLOCKED"]
    final_input_fingerprint = fingerprint(
        {
            "pre_gate_input_fingerprint": decision.input_fingerprint,
            "risk_gate_fingerprint": gate_fingerprint,
            "execution_status": execution_status,
            "reason_codes": reason_codes,
            "policy_version": "account-plan-decision.v1",
        }
    )
    return decision.model_copy(
        update={
            "decision_id": stable_id(
                "decision",
                {
                    "plan_id": decision.plan_id,
                    "input_fingerprint": final_input_fingerprint,
                },
                length=32,
            ),
            "risk_gate_ref": f"risk-gate:{gate_fingerprint}",
            "execution_status": execution_status,
            "reason_codes": reason_codes,
            "input_fingerprint": final_input_fingerprint,
            "created_at": datetime.now(timezone.utc),
        }
    )


def _scenario_sort_key(scenario: ZoneScenario, risk_profile: str) -> tuple[Any, ...]:
    relation_rank = 0 if scenario.price_relation is PriceRelation.INSIDE else 1
    reach_rank = {
        ReachabilityBand.NEAR: 0,
        ReachabilityBand.MEDIUM: 1,
        ReachabilityBand.FAR: 2,
        ReachabilityBand.UNKNOWN: 3,
    }[scenario.reachability_band]
    rr = -(scenario.reference_net_rr_1 or Decimal("0"))
    if risk_profile == "aggressive":
        return (relation_rank, reach_rank, scenario.base_rank, rr, scenario.scenario_id)
    if risk_profile == "conservative":
        return (
            0 if "price_structure" in scenario.source_families else 1,
            -len(set(scenario.source_families)),
            rr,
            scenario.base_rank,
            scenario.scenario_id,
        )
    return (scenario.base_rank, relation_rank, reach_rank, rr, scenario.scenario_id)


def _execution_status(
    artifact: PricePlanArtifact,
    *,
    selected: ZoneScenario | None,
    state: PlanLifecycleState | None,
    review: PlanReviewArtifact | None,
    permission: str,
    risk_profile: str,
    target_position_pct: Decimal | None,
    account_cost_status: str,
    account_cost_reason: str | None,
) -> tuple[str, list[str]]:
    if artifact.generation_status is GenerationStatus.NO_PLAN:
        return "no_plan", list(artifact.reason_codes)
    if selected is None or artifact.generation_status is GenerationStatus.OBSERVATION:
        return "observe_only", ["NO_SELECTABLE_SCENARIO"]
    if state is not None and state.state in {"invalidated", "expired", "closed"}:
        return "closed", [f"SCENARIO_{state.state.upper()}"]
    if permission == "vetoed":
        return "vetoed", ["REVIEW_HARD_VETO"]
    if permission == "observe_only":
        if review is not None and review.validated_output.get("price_fields_present") is True:
            return "observe_only", ["LLM_PRICE_AUTHORITY_VIOLATION"]
        return "observe_only", ["REVIEW_OBSERVE_ONLY"]
    if state is None or state.state != "confirmed":
        return "waiting_confirmation", ["TECHNICAL_CONFIRMATION_PENDING"]
    extra_confirmation_required = permission == "conditional" or risk_profile == "conservative"
    extra_confirmation_complete = bool(
        state.confirmed_at is not None
        and state.last_evaluated_as_of > state.confirmed_at.date()
    )
    if extra_confirmation_required and not extra_confirmation_complete:
        return "waiting_confirmation", ["EXTRA_CONFIRMATION_PENDING"]
    if target_position_pct is None or target_position_pct <= 0:
        return "risk_blocked", ["POSITION_SIZING_UNAVAILABLE"]
    if account_cost_status in {"unavailable", "below_minimum"}:
        return "risk_blocked", [account_cost_reason or "ACCOUNT_NET_RR_UNAVAILABLE"]
    return "risk_blocked", ["RISK_GATE_PENDING"]


def _position_cap(
    position_sizing: dict[str, Any] | None,
    *,
    risk_profile: str,
    review_multiplier: Decimal,
    account_position_cap_pct: Decimal | None,
) -> tuple[Decimal | None, str | None]:
    if not isinstance(position_sizing, dict) or position_sizing.get("status") != "ok":
        return None, None
    raw_cap = position_sizing.get("target_position_cap_pct")
    try:
        base_cap = Decimal(str(raw_cap))
    except (TypeError, ValueError, ArithmeticError):
        return None, None
    if base_cap <= 0:
        return None, None
    cap = (
        base_cap
        * RISK_PROFILE_POSITION_MULTIPLIERS[risk_profile]
        * review_multiplier
    )
    if account_position_cap_pct is not None:
        cap = min(cap, account_position_cap_pct)
    reference = ":".join(
        str(value)
        for value in (
            "position_sizing.v1",
            position_sizing.get("position_tier") or "unknown",
            position_sizing.get("market_coefficient") or "unknown",
        )
    )
    return cap.quantize(Decimal("0.000001")), reference


def _review_evidence(tool_calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    from src.agent.dimension_evidence import infer_tool_dimension

    output = []
    for index, call in enumerate(tool_calls or [], start=1):
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "unknown")
        output.append(
            {
                "ref": f"tool:{index}:{tool}",
                "tool": tool,
                "dimension": infer_tool_dimension(tool),
                "success": call.get("success") is True,
                "arguments": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                "result_preview": str(call.get("result_preview") or "")[:600],
            }
        )
    return output


def _available_dimensions(evidence: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item.get("dimension"))
            for item in evidence
            if item["success"] and item.get("dimension") in _DIMENSION_ALIASES
        }
    )


def _extract_action(result: Any) -> str:
    candidates = [
        getattr(result, "dashboard", None),
        getattr(result, "debate", None),
    ]
    keys = ("final_action", "current_action", "action", "decision_type", "operation_advice")
    values: list[str] = []
    for candidate in candidates:
        values.extend(_find_values(candidate, keys))
    for key in keys:
        value = getattr(result, key, None)
        if value not in (None, ""):
            values.append(str(value))
    text = " ".join(values).lower()
    # Negated entry advice must win over the entry verb itself.  Without this
    # guard, phrases such as "观望，不开仓" are incorrectly classified as open.
    if re.search(
        r"(?:不|勿)(?:建议|宜|应)?(?:立即|现在|当前)?(?:买入|开(?:新)?仓|加仓|入场)",
        text,
    ) or any(
        token in text
        for token in (
            "不开仓",
            "不买入",
            "不加仓",
            "暂不开仓",
            "暂不买入",
            "暂不入场",
            "勿开仓",
            "勿买入",
            "do not buy",
            "don't buy",
            "do not open",
            "no entry",
        )
    ):
        return "wait"
    token_map = (
        (("卖出", "退出", "sell", "stop_loss"), "sell"),
        (("减仓", "reduce", "take_profit"), "reduce"),
        (("回避", "拒绝", "reject", "avoid"), "reject"),
        (("观望", "等待", "wait", "monitor"), "wait"),
        (("加仓", "add"), "add"),
        (("买入", "开仓", "buy", "open"), "open"),
        (("持有", "hold"), "hold"),
    )
    for tokens, action in token_map:
        if any(token in text for token in tokens):
            return action
    return "unknown"


def _has_unauthorized_price_fields(result: Any) -> bool:
    for candidate in (
        getattr(result, "dashboard", None),
        getattr(result, "debate", None),
        getattr(result, "trade_plan", None),
    ):
        if _structured_price_present(candidate):
            return True
    content = _DETERMINISTIC_PRICE_BLOCK.sub(
        "",
        str(getattr(result, "content", "") or ""),
    )
    return bool(_PRICE_AUTHORITY_TEXT.search(content))


def _structured_price_present(value: Any, *, parent_key: str = "") -> bool:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _PRICE_AUTHORITY_KEYS and _contains_numeric_value(child):
                return True
            if _structured_price_present(child, parent_key=key):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_structured_price_present(item, parent_key=parent_key) for item in value)
    return False


def _contains_numeric_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, dict):
        return any(_contains_numeric_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_numeric_value(item) for item in value)
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "null", "待生成", "待补充"}:
        return False
    return bool(re.search(r"\d+(?:\.\d+)?", text))


def sanitize_llm_price_fields(value: Any) -> Any:
    """Replace model-owned execution prices while preserving qualitative evidence."""
    if isinstance(value, dict):
        return {
            key: (
                "N/A（由确定性价格引擎接管）"
                if str(key).strip().lower() in _PRICE_AUTHORITY_KEYS
                and _contains_numeric_value(child)
                else sanitize_llm_price_fields(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [sanitize_llm_price_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_llm_price_fields(item) for item in value)
    return value


def _find_values(value: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, dict):
        return []
    output = [str(value[key]) for key in keys if value.get(key) not in (None, "")]
    for child in value.values():
        if isinstance(child, dict):
            output.extend(_find_values(child, keys))
    return output


__all__ = [
    "build_account_decision",
    "build_review_artifact",
    "finalize_account_decision",
    "sanitize_llm_price_fields",
    "REVIEW_PERMISSIONS",
    "RISK_PROFILE_POSITION_MULTIPLIERS",
]
