from copy import deepcopy

from src.agent.judge_sanity import WORKER_UNAVAILABLE_MARKER, apply_judge_sanity


def _judge(action="open"):
    return {
        "stage": "judge_decision",
        "status": "ok",
        "summary": {
            "primary_plan_verdict": "accept",
            "final_action": action,
            "decision_summary": "原始裁决",
            "next_step": "render_final_report",
        },
        "full": {"winner": "primary"},
    }


def _allocation(action="open", pct=10, amount=5000):
    return {
        "stage": "portfolio_allocation",
        "status": "ok",
        "summary": {
            "portfolio_action": action,
            "recommended_position_count": 1 if pct > 0 else 0,
            "initial_total_position_pct": pct,
            "reserved_cash_pct": 100 - pct,
        },
        "full": {
            "positions_plan": [
                {
                    "code": "600001",
                    "name": "测试一",
                    "action": action,
                    "initial_position_pct": pct,
                    "initial_amount": amount,
                }
            ],
            "risk_controls": [],
        },
    }


def test_worker_unavailable_forces_open_judge_to_wait():
    payload = _judge("open")
    allocation = _allocation()
    allocation["full"]["risk_controls"].append(f"{WORKER_UNAVAILABLE_MARKER} tool timeout")

    sanitized = apply_judge_sanity(payload, allocation_payload=allocation)

    assert sanitized["summary"]["final_action"] == "wait"
    assert sanitized["summary"]["primary_plan_verdict"] == "accept_with_changes"
    checks = sanitized["full"]["sanity_checks"]
    assert checks[0]["rule_id"] == "worker_unavailable_force_wait"
    assert WORKER_UNAVAILABLE_MARKER not in sanitized["summary"]["decision_summary"]


def test_open_without_position_plan_downgrades_to_wait():
    payload = _judge("open")
    allocation = _allocation(action="wait", pct=0, amount=0)

    sanitized = apply_judge_sanity(payload, allocation_payload=allocation)

    assert sanitized["summary"]["final_action"] == "wait"
    assert any(check["rule_id"] == "open_without_position_plan" for check in sanitized["full"]["sanity_checks"])


def test_position_pct_clamped_and_sanitized_allocation_returned():
    payload = _judge("open")
    allocation = _allocation(pct=35, amount=35000)

    sanitized = apply_judge_sanity(
        payload,
        allocation_payload=allocation,
        investor_profile={"max_single_position_pct": 15},
    )

    sanitized_allocation = sanitized["full"]["sanitized_allocation"]
    plan = sanitized_allocation["full"]["positions_plan"][0]
    assert plan["initial_position_pct"] == 15
    assert plan["initial_amount"] == 15000
    assert sanitized_allocation["summary"]["initial_total_position_pct"] == 15
    assert any(check["rule_id"] == "max_single_position_clamp" for check in sanitized["full"]["sanity_checks"])


def test_risk_defense_regime_downgrades_active_judge():
    sanitized = apply_judge_sanity(
        _judge("open"),
        allocation_payload=_allocation(),
        market_regime={"regime": "risk_off", "risk_level": "high", "volatility_bucket": "extreme"},
    )

    assert sanitized["summary"]["final_action"] == "wait"
    assert any(check["rule_id"] == "risk_defense_downgrade" for check in sanitized["full"]["sanity_checks"])


def test_risk_defense_wait_with_no_position_keeps_audit_check():
    sanitized = apply_judge_sanity(
        _judge("wait"),
        allocation_payload=_allocation(action="wait", pct=0, amount=0),
        market_regime={"regime": "risk_off"},
    )

    assert sanitized["summary"]["final_action"] == "wait"
    checks = sanitized["full"]["sanity_checks"]
    assert any(check["rule_id"] == "risk_defense_already_wait" for check in checks)


def test_wait_is_not_upgraded_or_mutated_without_checks():
    payload = _judge("wait")
    original = deepcopy(payload)

    sanitized = apply_judge_sanity(payload, allocation_payload=_allocation(action="wait", pct=0, amount=0))

    assert sanitized == original
