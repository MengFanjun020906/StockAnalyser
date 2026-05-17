from src.agent.risk_gate import QuoteState, RiskGateEvaluator, RiskGateInput
from src.schemas.agent_context import AccountContext, InvestorProfile, PositionContext
from src.schemas.agent_signal import TradePlan


def _gate(plan: TradePlan, **kwargs):
    return RiskGateEvaluator().evaluate(RiskGateInput(plan=plan, **kwargs))


def test_t_plus_one_blocks_same_day_sell():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="sell",
            stop_loss_pct=6,
            invalidation_conditions=["跌破成本区"],
        ),
        position=PositionContext(symbol="688469", quantity=100, holding_days=0),
        data_quality="sufficient",
    )

    assert result.status == "blocked"
    assert result.allowed_action == "manual_review"
    assert any(check.rule_id == "a_share_t_plus_one" for check in result.checks)


def test_limit_up_blocks_open_chase():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=10,
            stop_loss_pct=7,
        ),
        quote=QuoteState(symbol="688469", is_limit_up=True),
        data_quality="sufficient",
        l3_confidence=0.8,
    )

    assert result.status == "blocked"
    assert result.allowed_action == "wait"
    assert "涨停" in result.blocked_reasons[0]


def test_limit_down_sell_requires_manual_review_not_fake_execution():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="sell",
            stop_loss_pct=6,
            invalidation_conditions=["风险事件确认"],
        ),
        quote=QuoteState(symbol="688469", is_limit_down=True),
        position=PositionContext(symbol="688469", quantity=100, holding_days=10),
        data_quality="sufficient",
    )

    assert result.status == "manual_review"
    assert result.allowed_action == "manual_review"
    assert result.required_manual_review is True
    assert any(check.rule_id == "a_share_limit_down_no_fake_sell" for check in result.checks)


def test_failed_critical_data_downgrades_active_open():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=10,
            stop_loss_pct=7,
        ),
        data_quality="failed",
        failed_tools=["get_capital_flow"],
        l3_confidence=0.9,
    )

    assert result.status == "blocked"
    assert result.allowed_action == "wait"
    assert any(check.rule_id == "critical_data_quality" for check in result.checks)


def test_missing_stop_loss_or_invalidation_blocks_active_trade():
    result = _gate(
        TradePlan(symbol="688469", action="open", target_position_pct=10),
        data_quality="sufficient",
        l3_confidence=0.9,
    )

    assert result.status == "blocked"
    assert result.allowed_action == "manual_review"
    assert any(check.rule_id == "risk_control_required" for check in result.checks)


def test_low_confidence_downgrades_active_trade_to_wait():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=10,
            stop_loss_pct=7,
        ),
        data_quality="sufficient",
        l3_confidence=0.6,
    )

    assert result.status == "blocked"
    assert result.allowed_action == "wait"
    assert any(check.rule_id == "l3_confidence_gate" for check in result.checks)


def test_position_limits_and_cash_are_enforced():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=25,
            stop_loss_pct=7,
        ),
        investor=InvestorProfile(max_single_position_pct=20, max_total_equity_exposure_pct=70),
        account=AccountContext(total_equity=100000, available_cash=5000),
        current_total_exposure_pct=60,
        data_quality="sufficient",
        l3_confidence=0.9,
    )

    assert result.status == "blocked"
    assert result.allowed_action == "manual_review"
    rule_ids = {check.rule_id for check in result.checks if not check.passed}
    assert {"single_position_limit", "total_exposure_limit", "cash_available"} <= rule_ids


def test_passes_clean_open_plan():
    result = _gate(
        TradePlan(
            symbol="688469",
            action="open",
            target_position_pct=10,
            stop_loss_pct=7,
            invalidation_conditions=["跌破 5 日线且放量"],
        ),
        investor=InvestorProfile(max_single_position_pct=20, max_total_equity_exposure_pct=80),
        account=AccountContext(total_equity=100000, available_cash=30000),
        quote=QuoteState(symbol="688469"),
        current_total_exposure_pct=40,
        data_quality="sufficient",
        l3_confidence=0.85,
    )

    assert result.status == "passed"
    assert result.allowed_action == "open"
    assert result.passed is True
