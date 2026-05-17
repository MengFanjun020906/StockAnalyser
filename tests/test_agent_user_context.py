import pytest
from pydantic import ValidationError

from src.schemas.agent_context import (
    AccountContext,
    AgentUserContext,
    InvestorProfile,
    PositionContext,
    ReportContext,
)


def test_agent_user_context_defaults_are_account_aware_but_passive():
    context = AgentUserContext()

    assert context.schema_version == "2026-05-02"
    assert context.investor.risk_preference == "balanced"
    assert context.investor.preferred_markets == ["cn"]
    assert context.accounts == []
    assert context.positions == []
    assert context.report.intent == "auto"
    assert context.report.analysis_mode == "normal"
    assert context.metadata == {}


def test_agent_user_context_accepts_position_review_payload():
    context = AgentUserContext(
        investor=InvestorProfile(
            risk_preference="conservative",
            max_single_position_pct=20,
            default_stop_loss_pct=8,
        ),
        accounts=[
            AccountContext(
                account_id=1,
                account_type="margin",
                margin_mode="margin",
                total_equity=100000,
                available_cash=20000,
                margin_debt=10000,
                maintenance_ratio=180,
                risk_line_ratio=150,
            )
        ],
        positions=[
            PositionContext(
                symbol="600519",
                market="cn",
                account_id=1,
                quantity=100,
                avg_cost=1580,
                last_price=1660,
                market_value=166000,
                position_pct=18,
                stop_loss=1500,
            )
        ],
        report=ReportContext(
            intent="position_review",
            analysis_mode="planning_execute",
            primary_symbol="600519",
            target_symbols=["600519"],
            user_prompt="我这只持仓现在要不要减仓？",
        ),
        metadata={"source": "test"},
    )

    assert context.has_position_for("600519") is True
    assert context.has_position_for(" 600519 ") is True
    assert context.has_position_for("000001") is False
    assert context.accounts[0].account_type == "margin"
    assert context.positions[0].avg_cost == 1580
    assert context.report.intent == "position_review"
    assert context.report.analysis_mode == "planning_execute"


def test_agent_user_context_has_position_requires_non_zero_quantity():
    context = AgentUserContext(
        positions=[
            PositionContext(symbol="600519", quantity=0),
            PositionContext(symbol="000001", quantity=10),
        ]
    )

    assert context.has_position_for("600519") is False
    assert context.has_position_for("000001") is True
    assert context.has_position_for("") is False


def test_agent_user_context_allows_event_impact_intent():
    context = AgentUserContext(report=ReportContext(intent="event_impact"))

    assert context.report.intent == "event_impact"


@pytest.mark.parametrize(
    ("payload", "field_name"),
    [
        ({"investor": {"risk_preference": "reckless"}}, "risk_preference"),
        ({"accounts": [{"account_type": "brokerage"}]}, "account_type"),
        ({"positions": [{"symbol": "600519", "quantity": -1}]}, "quantity"),
        ({"report": {"analysis_mode": "planner"}}, "analysis_mode"),
    ],
)
def test_agent_user_context_rejects_invalid_contract_values(payload, field_name):
    with pytest.raises(ValidationError) as exc_info:
        AgentUserContext(**payload)

    assert field_name in str(exc_info.value)
