import pytest
from pydantic import ValidationError

from src.schemas.agent_signal import (
    EvidenceRef,
    L1Signal,
    L2SignalSummary,
    L3Decision,
    ReasoningTraceRef,
    RiskGateCheck,
    RiskGateResult,
    TradePlan,
)


def test_l1_l2_l3_protocol_serializes_signal_chain():
    evidence = EvidenceRef(
        source="get_capital_flow",
        source_type="tool",
        ref_id="tool-call-1",
        summary="主力资金净流入",
        data_quality="sufficient",
    )
    l1 = L1Signal(
        symbol="688469",
        category="capital_flow",
        source="get_capital_flow",
        direction="bullish",
        confidence=0.82,
        data_quality="sufficient",
        evidence=["主力净流入"],
        evidence_refs=[evidence],
    )
    l2 = L2SignalSummary(
        symbol="688469",
        category="capital_flow",
        direction="bullish",
        confidence=0.78,
        data_quality="sufficient",
        key_points=["资金面支持"],
        l1_signal_ids=["l1-capital-flow"],
        evidence_refs=[evidence],
    )
    l3 = L3Decision(
        symbol="688469",
        action="wait",
        confidence=0.7,
        rationale="信号偏多但等待回踩确认。",
        data_quality="limited",
        l2_summary_ids=["l2-capital-flow"],
        review_triggers=["回踩 5 日线后复核"],
    )

    payload = {
        "l1_signal": l1.model_dump(mode="json"),
        "l2_summary": l2.model_dump(mode="json"),
        "l3_decision": l3.model_dump(mode="json"),
    }

    assert payload["l1_signal"]["category"] == "capital_flow"
    assert payload["l2_summary"]["l1_signal_ids"] == ["l1-capital-flow"]
    assert payload["l3_decision"]["action"] == "wait"


def test_protocol_can_carry_bad_active_plan_for_risk_gate_audit():
    decision = L3Decision(
        symbol="688469",
        action="open",
        confidence=0.8,
        rationale="准备开仓，但尚未给出止损。",
    )
    plan = TradePlan(symbol="688469", action="add", target_position_pct=15)

    assert decision.stop_loss_pct is None
    assert plan.invalidation_conditions == []


def test_trade_plan_rejects_invalid_entry_zone():
    with pytest.raises(ValidationError) as exc_info:
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=10,
            entry_zone_low=12,
            entry_zone_high=11,
            stop_loss_pct=7,
        )

    assert "entry_zone_low" in str(exc_info.value)


def test_risk_gate_and_trace_refs_are_json_serializable():
    result = RiskGateResult(
        status="manual_review",
        original_action="sell",
        allowed_action="manual_review",
        checks=[
            RiskGateCheck(
                rule_id="a_share_t_plus_one",
                passed=False,
                severity="blocking",
                message="T+1 blocked",
                suggested_action="manual_review",
            )
        ],
        blocked_reasons=["T+1 blocked"],
        required_manual_review=True,
    )
    trace = ReasoningTraceRef(
        session_id="trace-1",
        artifact_dir="data/agent_traces/trace-1",
        risk_gate_ref="risk_gate.json",
    )

    assert result.passed is False
    assert result.model_dump(mode="json")["allowed_action"] == "manual_review"
    assert trace.model_dump(mode="json")["risk_gate_ref"] == "risk_gate.json"
