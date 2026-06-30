# -*- coding: utf-8 -*-
"""Deterministic sanity checks for stock-selection Judge output."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


WORKER_UNAVAILABLE_MARKER = "[WORKER_UNAVAILABLE]"
ACTIVE_ACTIONS = {"open", "buy", "add", "increase"}
DEFAULT_MAX_SINGLE_POSITION_PCT = 20.0


@dataclass(frozen=True)
class JudgeSanityConfig:
    """Policy defaults for deterministic Judge post-processing."""

    max_single_position_pct: float = DEFAULT_MAX_SINGLE_POSITION_PCT
    worker_unavailable_action: str = "wait"
    overconfident_open_action: str = "wait"


def apply_judge_sanity(
    judge_payload: Dict[str, Any],
    *,
    allocation_payload: Optional[Dict[str, Any]] = None,
    market_regime: Optional[Dict[str, Any]] = None,
    investor_profile: Optional[Dict[str, Any]] = None,
    config: Optional[JudgeSanityConfig] = None,
) -> Dict[str, Any]:
    """Return a sanitized copy of a Judge payload plus audit metadata.

    The function only downgrades or clamps unsafe outputs. It never upgrades a
    non-active action into an opening recommendation.
    """
    cfg = config or _config_from_investor(investor_profile)
    payload = deepcopy(judge_payload) if isinstance(judge_payload, dict) else {}
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        payload["summary"] = summary
    full = payload.get("full")
    if not isinstance(full, dict):
        full = {}
        payload["full"] = full

    checks: List[Dict[str, Any]] = list(full.get("sanity_checks") or [])
    allocation = deepcopy(allocation_payload) if isinstance(allocation_payload, dict) else {}
    allocation_summary = allocation.get("summary") if isinstance(allocation.get("summary"), dict) else {}
    allocation_full = allocation.get("full") if isinstance(allocation.get("full"), dict) else {}
    positions = allocation_full.get("positions_plan")
    if not isinstance(positions, list):
        positions = []

    worker_unavailable = _contains_marker(payload) or _contains_marker(allocation)
    if worker_unavailable and _is_active_action(summary.get("final_action")):
        _downgrade_summary(
            summary,
            cfg.worker_unavailable_action,
            reason="上游 worker 或工具阶段出现不可用标记，Judge 主动交易裁决降级为等待确认。",
            checks=checks,
            rule_id="worker_unavailable_force_wait",
        )

    if _is_active_action(summary.get("final_action")) and _active_position_count(positions) == 0:
        _downgrade_summary(
            summary,
            cfg.overconfident_open_action,
            reason="Judge 给出开仓动作，但组合配置没有任何可执行开仓仓位，降级为等待确认。",
            checks=checks,
            rule_id="open_without_position_plan",
        )

    if _is_risk_defense_regime(market_regime):
        if _is_active_action(summary.get("final_action")):
            _downgrade_summary(
                summary,
                "wait",
                reason="市场状态处于 risk_off/panic/extreme，Judge 主动开仓裁决降级为等待确认。",
                checks=checks,
                rule_id="risk_defense_downgrade",
            )
        elif (
            str(summary.get("final_action") or "").strip().lower() == "wait"
            and _active_position_count(positions) == 0
        ):
            _append_check_once(
                checks,
                rule_id="risk_defense_already_wait",
                action="audit",
                reason="市场状态处于 risk_off/panic/extreme，且组合配置已无可执行开仓仓位，确认维持等待。",
            )

    clamp_checks, total_pct = _clamp_positions(positions, cfg.max_single_position_pct)
    checks.extend(clamp_checks)
    if clamp_checks:
        _sync_allocation_summary(allocation_summary, positions, total_pct)
        full["sanitized_allocation"] = allocation
        if _is_active_action(summary.get("final_action")) and total_pct <= 0:
            _downgrade_summary(
                summary,
                "wait",
                reason="仓位 sanity clamp 后没有剩余可执行开仓比例，最终动作降级为等待确认。",
                checks=checks,
                rule_id="zero_after_position_clamp",
            )

    if checks:
        full["sanity_checks"] = checks
        notes = full.get("required_plan_changes")
        if not isinstance(notes, list):
            notes = []
        for check in checks:
            reason = check.get("reason")
            if reason and reason not in notes:
                notes.append(reason)
        full["required_plan_changes"] = notes

    payload["summary"] = summary
    payload["full"] = full
    return payload


def _config_from_investor(investor_profile: Optional[Dict[str, Any]]) -> JudgeSanityConfig:
    max_single = DEFAULT_MAX_SINGLE_POSITION_PCT
    if isinstance(investor_profile, dict):
        value = investor_profile.get("max_single_position_pct")
        try:
            if value is not None:
                max_single = max(0.0, float(value))
        except (TypeError, ValueError):
            max_single = DEFAULT_MAX_SINGLE_POSITION_PCT
    return JudgeSanityConfig(max_single_position_pct=max_single)


def _is_active_action(value: Any) -> bool:
    return str(value or "").strip().lower() in ACTIVE_ACTIONS


def _contains_marker(value: Any) -> bool:
    if isinstance(value, str):
        return WORKER_UNAVAILABLE_MARKER in value
    if isinstance(value, dict):
        return any(_contains_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item) for item in value)
    return False


def _active_position_count(positions: List[Any]) -> int:
    count = 0
    for item in positions:
        if not isinstance(item, dict):
            continue
        if _is_active_action(item.get("action")) and _float_or_zero(item.get("initial_position_pct")) > 0:
            count += 1
    return count


def _is_risk_defense_regime(market_regime: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(market_regime, dict):
        return False
    regime = str(market_regime.get("regime") or "").strip().lower()
    risk = str(market_regime.get("risk_level") or "").strip().lower()
    volatility = str(market_regime.get("volatility_bucket") or "").strip().lower()
    return regime in {"risk_off", "panic"} or risk in {"high", "extreme"} or volatility == "extreme"


def _downgrade_summary(
    summary: Dict[str, Any],
    action: str,
    *,
    reason: str,
    checks: List[Dict[str, Any]],
    rule_id: str,
) -> None:
    original_action = summary.get("final_action")
    if str(original_action or "").strip().lower() == action:
        return
    summary["final_action"] = action
    if action in {"wait", "monitor"} and summary.get("primary_plan_verdict") == "accept":
        summary["primary_plan_verdict"] = "accept_with_changes"
    if action == "wait" and summary.get("next_step") in {None, "", "stop_no_trade"}:
        summary["next_step"] = "render_final_report"
    original_reason = str(summary.get("decision_summary") or "").strip()
    summary["decision_summary"] = f"{original_reason}；{reason}" if original_reason else reason
    checks.append({
        "rule_id": rule_id,
        "action": "downgrade",
        "from": original_action,
        "to": action,
        "reason": reason,
    })


def _append_check_once(
    checks: List[Dict[str, Any]],
    *,
    rule_id: str,
    action: str,
    reason: str,
) -> None:
    if any(check.get("rule_id") == rule_id for check in checks):
        return
    checks.append({
        "rule_id": rule_id,
        "action": action,
        "reason": reason,
    })


def _clamp_positions(positions: List[Any], max_single_pct: float) -> Tuple[List[Dict[str, Any]], float]:
    checks: List[Dict[str, Any]] = []
    total_pct = 0.0
    for item in positions:
        if not isinstance(item, dict):
            continue
        pct = _float_or_zero(item.get("initial_position_pct"))
        if pct > max_single_pct:
            original_pct = pct
            item["initial_position_pct"] = max_single_pct
            if isinstance(item.get("initial_amount"), (int, float)) and original_pct > 0:
                item["initial_amount"] = item["initial_amount"] * max_single_pct / original_pct
            reason = f"单票首仓比例 {original_pct:.2f}% 超过上限 {max_single_pct:.2f}%，已截断。"
            rules = item.get("auto_downgrade_rules")
            if not isinstance(rules, list):
                rules = []
            if reason not in rules:
                rules.append(reason)
            item["auto_downgrade_rules"] = rules
            checks.append({
                "rule_id": "max_single_position_clamp",
                "action": "clamp",
                "code": item.get("code") or item.get("stock_code"),
                "from": original_pct,
                "to": max_single_pct,
                "reason": reason,
            })
            pct = max_single_pct
        total_pct += max(0.0, pct)
    return checks, total_pct


def _sync_allocation_summary(summary: Dict[str, Any], positions: List[Any], total_pct: float) -> None:
    active_count = _active_position_count(positions)
    summary["recommended_position_count"] = active_count
    summary["initial_total_position_pct"] = total_pct
    summary["reserved_cash_pct"] = max(0.0, 100.0 - total_pct)
    if active_count <= 0:
        summary["portfolio_action"] = "wait"


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["JudgeSanityConfig", "WORKER_UNAVAILABLE_MARKER", "apply_judge_sanity"]
