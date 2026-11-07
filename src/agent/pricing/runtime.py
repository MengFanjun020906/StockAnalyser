from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from src.config import get_config
from src.storage import DatabaseManager

from .fingerprint import fingerprint, stable_id
from .market_rules import is_supported_cn_common_stock, resolve_cn_market_rules
from .params import PRICE_PLAN_PARAMS_V1
from .plan import evaluate_price_plan
from .readiness import PricePlanReadinessGate
from .repository import PricePlanRepository
from .schemas import (
    GenerationStatus,
    PricePlanArtifact,
    PricePlanSnapshot,
    TradePlanResult,
)


def enrich_trade_plan_result(
    trade_plan_result: TradePlanResult,
    *,
    agent_result: Any,
    context: dict[str, Any] | None = None,
    db_manager: DatabaseManager | None = None,
) -> TradePlanResult:
    """Attach immutable review, account decision, and current lifecycle state."""
    artifact = trade_plan_result.price_plan
    if artifact.generation_status is GenerationStatus.NO_PLAN:
        return trade_plan_result
    from .decision import (
        build_account_decision,
        build_review_artifact,
        sanitize_llm_price_fields,
    )

    db = db_manager or DatabaseManager.get_instance()
    repository = PricePlanRepository(db)
    review = build_review_artifact(
        artifact,
        result=agent_result,
        tool_calls=getattr(agent_result, "tool_calls_log", []) or [],
    )
    if review is not None:
        review = repository.save_review(review)
        if review.validated_output.get("price_fields_present") is True:
            for attribute in ("dashboard", "debate", "trade_plan"):
                value = getattr(agent_result, attribute, None)
                if value is not None:
                    setattr(agent_result, attribute, sanitize_llm_price_fields(value))
    lifecycle_states = repository.list_lifecycle_states(artifact.plan_id)
    risk_profile = _risk_profile(context)
    owner_id, account_id, decision_scope = _decision_identity(context)
    market_rules = repository.get_market_rules(artifact.market_rules_snapshot_ref)
    decision = build_account_decision(
        artifact,
        review=review,
        lifecycle_states=lifecycle_states,
        risk_profile=risk_profile,
        position_sizing=_position_sizing(context),
        account_position_cap_pct=_account_position_cap_pct(context),
        account_execution=_account_execution_context(context),
        lot_size=market_rules.buy_lot_size if market_rules is not None else 100,
        decision_scope=decision_scope,
    )
    decision = repository.save_account_decision(
        decision,
        owner_id=owner_id,
        account_id=account_id,
    )
    reasons = sorted(
        set(
            code
            for code in [*trade_plan_result.reason_codes, *decision.reason_codes]
            if code != "REVIEW_PENDING"
        )
    )
    return TradePlanResult(
        price_plan=artifact,
        review=review,
        account_decision=decision,
        lifecycle_states=lifecycle_states,
        execution_status=decision.execution_status,
        reason_codes=reasons,
    )


def generate_trade_plan(
    symbol: str,
    *,
    as_of: date,
    snapshot: PricePlanSnapshot | None = None,
    trace_ref: str | None = None,
    disabled_families: frozenset[str] | None = None,
    db_manager: DatabaseManager | None = None,
    persist: bool = True,
) -> TradePlanResult:
    """Generate the single-stock deterministic price plan read model."""
    config = get_config()
    enabled = bool(getattr(config, "agent_deterministic_price_plan_enabled", True))
    configured_disabled = frozenset(
        str(item).strip()
        for item in getattr(config, "agent_price_plan_disabled_anchor_families", [])
        if str(item).strip()
    )
    effective_disabled = configured_disabled if disabled_families is None else disabled_families
    if not enabled:
        return _system_result(
            symbol=symbol,
            as_of=as_of,
            reason="SYSTEM_DISABLED",
            execution_status="system_disabled",
            trace_ref=trace_ref,
        )
    if not is_supported_cn_common_stock(symbol):
        return _system_result(
            symbol=symbol,
            as_of=as_of,
            reason="UNSUPPORTED_INSTRUMENT",
            execution_status="no_plan",
            trace_ref=trace_ref,
        )

    db = db_manager or DatabaseManager.get_instance()
    repository = PricePlanRepository(db)
    from .snapshot import PricePlanSnapshotBuilder, resolve_completed_cn_as_of

    readiness_as_of = (
        snapshot.actual_as_of_session
        if snapshot is not None
        else resolve_completed_cn_as_of(as_of)
    )
    readiness = PricePlanReadinessGate(db).evaluate(
        symbol=symbol,
        as_of=readiness_as_of,
    )
    if not readiness.ready:
        return _system_result(
            symbol=symbol,
            as_of=as_of,
            actual_as_of_session=readiness_as_of,
            reason="SYSTEM_NOT_READY",
            execution_status="system_unready",
            trace_ref=trace_ref,
            diagnostics={
                "readiness_checks": readiness.checks,
                "requested_as_of": as_of.isoformat(),
                "readiness_as_of_session": readiness_as_of.isoformat(),
            },
        )
    if snapshot is None:
        snapshot = PricePlanSnapshotBuilder(
            chip_loader=lambda requested_symbol, target: _load_chip_payload(
                repository,
                requested_symbol,
                target,
            ),
            market_rules_loader=repository.get_market_rules_for_symbol_date,
        ).build(symbol, as_of=as_of)

        # A live provider may lag the calendar target by one session. The
        # snapshot builder has already applied the verified live identity to
        # the actual bar date. Historical requests still require an exact
        # persisted rules snapshot and are re-checked here.
        if snapshot.actual_as_of_session != readiness_as_of:
            if snapshot.actual_as_of_session < readiness_as_of:
                readiness_as_of = snapshot.actual_as_of_session
            else:
                readiness_as_of = snapshot.actual_as_of_session
                readiness = PricePlanReadinessGate(db).evaluate(
                    symbol=symbol,
                    as_of=readiness_as_of,
                )
                if not readiness.ready:
                    return _system_result(
                        symbol=symbol,
                        as_of=snapshot.requested_as_of,
                        actual_as_of_session=readiness_as_of,
                        reason="SYSTEM_NOT_READY",
                        execution_status="system_unready",
                        trace_ref=trace_ref,
                        diagnostics={
                            "readiness_checks": readiness.checks,
                            "requested_as_of": snapshot.requested_as_of.isoformat(),
                            "readiness_as_of_session": readiness_as_of.isoformat(),
                        },
                    )
    artifact = evaluate_price_plan(
        snapshot,
        PRICE_PLAN_PARAMS_V1,
        disabled_families=effective_disabled,
        trace_ref=trace_ref,
    )
    if persist:
        repository.save_market_rules(snapshot.market_rules)
        if snapshot.chip is not None and as_of >= date.today():
            repository.save_chip_snapshot(
                snapshot.symbol,
                snapshot.chip,
                observed_at=datetime.now(),
            )
        artifact = repository.save_artifact(artifact)
    execution_status = (
        "review_pending" if artifact.generation_status != "no_plan" else "no_plan"
    )
    reason_codes = list(artifact.reason_codes)
    if artifact.generation_status != "no_plan":
        reason_codes.append("REVIEW_PENDING")
    return TradePlanResult(
        price_plan=artifact,
        execution_status=execution_status,
        reason_codes=sorted(set(reason_codes)),
    )


def _load_chip_payload(
    repository: PricePlanRepository,
    symbol: str,
    target: date,
) -> dict[str, Any] | None:
    persisted = repository.get_chip_snapshot(
        symbol,
        datetime.combine(target, time.max),
    )
    if persisted is not None:
        return {"status": "ok", **persisted.model_dump(mode="python")}
    if target < date.today():
        return None
    try:
        from src.agent.tools.data_tools import _handle_get_chip_distribution

        return _handle_get_chip_distribution(symbol)
    except Exception:
        return None


def _risk_profile(context: dict[str, Any] | None) -> str:
    agent_context = (context or {}).get("agent_user_context")
    investor = (
        agent_context.get("investor")
        if isinstance(agent_context, dict)
        else getattr(agent_context, "investor", None)
    )
    value = (
        investor.get("risk_preference")
        if isinstance(investor, dict)
        else getattr(investor, "risk_preference", None)
    )
    return str(value or (context or {}).get("risk_preference") or "balanced")


def _position_sizing(context: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = (context or {}).get("price_plan_composite_payload")
    if not isinstance(payload, dict):
        payload = (context or {}).get("trace_composite_verdict")
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("composite_verdict")
    if not isinstance(verdict, dict):
        return None
    cap = verdict.get("target_position_cap_pct")
    return {
        "status": "ok" if cap is not None else "not_evaluable",
        "target_position_cap_pct": cap,
        "target_position_cap_ratio": verdict.get("target_position_cap_ratio"),
        "position_tier": verdict.get("position_tier"),
        "market_coefficient": verdict.get("market_coefficient"),
        "rule_version": "position_sizing.v1",
    }


def _account_position_cap_pct(context: dict[str, Any] | None):
    from decimal import Decimal

    agent_context = (context or {}).get("agent_user_context")
    investor = (
        agent_context.get("investor")
        if isinstance(agent_context, dict)
        else getattr(agent_context, "investor", None)
    )
    value = (
        investor.get("max_single_position_pct")
        if isinstance(investor, dict)
        else getattr(investor, "max_single_position_pct", None)
    )
    if value is None:
        value = (context or {}).get("max_single_position_pct")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _account_execution_context(
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    payload = context or {}
    agent_context = payload.get("agent_user_context")
    accounts = (
        agent_context.get("accounts", [])
        if isinstance(agent_context, dict)
        else getattr(agent_context, "accounts", [])
    )
    raw_account_id = payload.get("account_id")
    selected = None
    for account in accounts or []:
        account_id = account.get("account_id") if isinstance(account, dict) else getattr(account, "account_id", None)
        if raw_account_id is not None and str(account_id) == str(raw_account_id):
            selected = account
            break
    if selected is None and len(accounts or []) == 1:
        selected = accounts[0]
    if selected is None:
        return None
    if hasattr(selected, "model_dump"):
        return selected.model_dump(mode="python")
    return dict(selected) if isinstance(selected, dict) else None


def _decision_identity(
    context: dict[str, Any] | None,
) -> tuple[str | None, int | None, str]:
    payload = context or {}
    owner_id = str(payload.get("owner_id") or "").strip() or None
    raw_account_id = payload.get("account_id")
    agent_context = payload.get("agent_user_context")
    accounts = (
        agent_context.get("accounts", [])
        if isinstance(agent_context, dict)
        else getattr(agent_context, "accounts", [])
    )
    if raw_account_id is None and len(accounts or []) == 1:
        account = accounts[0]
        raw_account_id = (
            account.get("account_id")
            if isinstance(account, dict)
            else getattr(account, "account_id", None)
        )
        if owner_id is None:
            owner_id = str(
                account.get("owner_id")
                if isinstance(account, dict)
                else getattr(account, "owner_id", "")
                or ""
            ).strip() or None
    try:
        account_id = int(raw_account_id) if raw_account_id is not None else None
    except (TypeError, ValueError):
        account_id = None
    scope = (
        f"account:{account_id}"
        if account_id is not None
        else f"owner:{owner_id}"
        if owner_id is not None
        else "request"
    )
    return owner_id, account_id, scope


def _system_result(
    *,
    symbol: str,
    as_of: date,
    actual_as_of_session: date | None = None,
    reason: str,
    execution_status: str,
    trace_ref: str | None,
    diagnostics: dict[str, Any] | None = None,
) -> TradePlanResult:
    normalized_symbol = "".join(ch for ch in symbol if ch.isdigit())[-6:] or symbol
    effective_as_of = actual_as_of_session or as_of
    try:
        rules = resolve_cn_market_rules(symbol, effective_as_of)
        market_rules_ref = rules.snapshot_id
    except ValueError:
        market_rules_ref = stable_id(
            "market-rules-unavailable",
            {"symbol": normalized_symbol, "as_of": effective_as_of},
            length=20,
        )
    calculation_fingerprint = fingerprint(
        {
            "symbol": symbol,
            "requested_as_of": as_of,
            "actual_as_of_session": effective_as_of,
            "reason": reason,
            "algorithm": PRICE_PLAN_PARAMS_V1.algorithm_version,
        }
    )
    payload = {
        "calculation_fingerprint": calculation_fingerprint,
        "generation_status": "no_plan",
        "reasons": [reason],
    }
    artifact = PricePlanArtifact(
        plan_id=stable_id("plan", payload),
        revision=1,
        symbol=normalized_symbol,
        requested_as_of=as_of,
        actual_as_of_session=effective_as_of,
        generation_status=GenerationStatus.NO_PLAN,
        scenarios=[],
        observation_anchors=[],
        market_rules_snapshot_ref=market_rules_ref,
        data_quality="invalid",
        reason_codes=[reason],
        diagnostics=diagnostics or {},
        algorithm_version=PRICE_PLAN_PARAMS_V1.algorithm_version,
        params_version=PRICE_PLAN_PARAMS_V1.version,
        data_fingerprint=fingerprint(
            {
                "symbol": symbol,
                "requested_as_of": as_of,
                "actual_as_of_session": effective_as_of,
                "status": reason,
            }
        ),
        calculation_fingerprint=calculation_fingerprint,
        plan_fingerprint=fingerprint(payload),
        created_at=datetime.combine(effective_as_of, time.max, tzinfo=timezone.utc),
        trace_ref=trace_ref,
    )
    return TradePlanResult(
        price_plan=artifact,
        execution_status=execution_status,
        reason_codes=[reason],
    )
