from types import SimpleNamespace
from unittest.mock import patch

from api.v1.endpoints import agent
from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext


def _trace_context(*, holding: bool = False):
    positions = [PositionContext(symbol="600519", quantity=100)] if holding else []
    return {
        "stock_code": "600519",
        "agent_user_context": AgentUserContext(
            positions=positions,
            report=ReportContext(intent="entry_analysis", primary_symbol="600519"),
        ),
    }


def _trace_result():
    return SimpleNamespace(
        debate={"judge_decision": {"final_action": "hold"}},
        stock_selection=None,
    )


def test_trace_composite_payload_uses_primary_symbol_and_position_context():
    expected = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "600519", "as_of": "2026-08-05"},
        "composite_verdict": {
            "verdict_status": "not_evaluable",
            "score": None,
            "scale_max": 20,
            "shadow_mode": True,
        },
    }
    with patch("api.v1.endpoints.agent.score_symbol_composite", return_value=expected) as scorer:
        payload = agent._build_trace_composite_verdict_payload(
            result=_trace_result(),
            context=_trace_context(holding=True),
            tool_calls=[],
        )

    assert payload == expected
    assert scorer.call_args.args == ("600519",)
    assert scorer.call_args.kwargs["holding"] is True
    assert scorer.call_args.kwargs["quote_basis"] == "latest_trading_day"
    assert scorer.call_args.kwargs["snapshot_builder"]._fallback_to_network is True


def test_trace_composite_prefetch_allows_network_history_backfill():
    expected = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "600519", "as_of": "2026-08-09"},
        "composite_verdict": {"score": 15, "shadow_mode": True},
    }
    with patch("api.v1.endpoints.agent.score_symbol_composite", return_value=expected) as scorer:
        payload = agent._prefetch_trace_composite_verdict(_trace_context())

    assert payload == expected
    assert scorer.call_args.args == ("600519",)
    assert scorer.call_args.kwargs["snapshot_builder"]._fallback_to_network is True
    assert scorer.call_args.kwargs["snapshot_builder"]._minimum_history_bars == 520


def test_trace_composite_prefetch_exposes_ambiguous_target_instead_of_silently_skipping():
    context = {
        "_trace_target_resolution": {
            "status": "ambiguous",
            "source": "portfolio_position_names",
            "candidate_symbols": ["002050", "600487"],
        },
        "agent_user_context": AgentUserContext(
            report=ReportContext(intent="qa"),
        ),
    }

    with patch("api.v1.endpoints.agent.score_symbol_composite") as scorer:
        payload = agent._prefetch_trace_composite_verdict(context)

    assert payload["status"] == "failed"
    assert payload["error_code"] == "target_unresolved"
    assert payload["target_resolution"]["candidate_symbols"] == ["002050", "600487"]
    scorer.assert_not_called()


def test_trace_primary_symbol_does_not_fall_back_to_first_portfolio_position():
    context = {
        "agent_user_context": AgentUserContext(
            positions=[
                PositionContext(symbol="002050", quantity=100, stock_name="三花智控"),
                PositionContext(symbol="600487", quantity=300, stock_name="亨通光电"),
            ],
            report=ReportContext(intent="position_review"),
        ),
    }

    symbol = agent._resolve_trace_primary_symbol(
        context,
        context["agent_user_context"],
        result=_trace_result(),
    )

    assert symbol is None


def test_trace_composite_replay_uses_unique_tool_target_when_initial_name_was_unresolved():
    context = {
        "agent_user_context": AgentUserContext(
            positions=[
                PositionContext(symbol="002050", quantity=100, stock_name="三花智控"),
                PositionContext(symbol="600487", quantity=300, stock_name="亨通光电"),
            ],
            report=ReportContext(intent="position_review"),
        ),
    }
    tool_calls = [
        {"tool": "analyze_trend", "success": True, "arguments": {"stock_code": "600487"}},
        {"tool": "analyze_price_structure", "success": True, "arguments": {"stock_code": "600487"}},
    ]
    expected = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "600487"},
        "composite_verdict": {"symbol": "600487", "score": None},
    }

    with patch("api.v1.endpoints.agent.score_symbol_composite", return_value=expected) as scorer:
        payload = agent._build_trace_composite_verdict_payload(
            result=_trace_result(),
            context=context,
            tool_calls=tool_calls,
        )

    assert scorer.call_args.args == ("600487",)
    assert payload["snapshot"]["symbol"] == "600487"


def test_trace_composite_rejects_prefetched_symbol_mismatch():
    context = _trace_context()
    context["trace_composite_verdict"] = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "002050", "as_of": "2026-08-11"},
        "composite_verdict": {"symbol": "002050", "score": None, "shadow_mode": True},
    }

    payload = agent._build_trace_composite_verdict_payload(
        result=_trace_result(),
        context=context,
        tool_calls=[{
            "tool": "analyze_trend",
            "success": True,
            "arguments": {"stock_code": "600519"},
        }],
    )

    assert payload["status"] == "failed"
    assert payload["error_code"] == "target_symbol_mismatch"
    assert payload["expected_symbol"] == "600519"
    assert payload["actual_symbol"] == "002050"


def test_trace_history_quarantines_stored_composite_and_risk_gate_target_mismatch():
    context = {
        "agent_user_context": AgentUserContext(
            positions=[
                PositionContext(symbol="002050", quantity=100, stock_name="三花智控"),
                PositionContext(symbol="600487", quantity=300, stock_name="亨通光电"),
            ],
            report=ReportContext(intent="position_review"),
        ).model_dump(mode="json"),
    }
    tool_calls = [
        {"tool": "analyze_trend", "arguments": {"stock_code": "600487"}},
        {"tool": "analyze_price_structure", "arguments": {"stock_code": "600487"}},
    ]
    composite = {
        "status": "ok",
        "snapshot": {"symbol": "002050"},
        "composite_verdict": {"symbol": "002050", "score": None},
    }
    risk_gate = {
        "trade_plan": {"symbol": "002050", "action": "hold"},
        "risk_gate": {"allowed_action": "hold", "checks": []},
    }

    quarantined_composite, quarantined_gate = agent._quarantine_stored_trace_target_mismatch(
        context=context,
        tool_calls=tool_calls,
        composite_payload=composite,
        risk_gate_payload=risk_gate,
    )

    assert quarantined_composite["status"] == "failed"
    assert quarantined_composite["error_code"] == "target_symbol_mismatch"
    assert quarantined_composite["expected_symbol"] == "600487"
    assert quarantined_composite["actual_symbol"] == "002050"
    assert quarantined_composite["mismatch_source"] == "stored_artifact"
    assert quarantined_gate["status"] == "failed"
    assert quarantined_gate["error_code"] == "target_symbol_mismatch"
    assert quarantined_gate["expected_symbol"] == "600487"
    assert quarantined_gate["actual_symbol"] == "002050"


def test_trace_history_enriches_legacy_provisional_score_without_promoting_official_verdict():
    payload = {
        "status": "ok",
        "snapshot": {
            "symbol": "600330",
            "as_of": "2026-08-11",
            "quote_basis": "latest_trading_day",
            "data_quality": "limited",
            "s4": {
                "status": "partial",
                "weekly_provisional": True,
                "region_pass": False,
                "resonance_count": 2,
                "volume_ratio_5d": 0.78635,
                "volatility_pct": 8.422227,
                "gain_10d_pct": 25.404531,
                "golden_cross_today": False,
                "all_ok": False,
            },
            "chan": {"status": "partial", "signals": []},
        },
        "composite_verdict": {
            "symbol": "600330",
            "as_of": "2026-08-11",
            "verdict_status": "not_evaluable",
            "score": None,
            "scale_max": 20,
            "components": {
                "s4": {"status": "partial", "score": None, "scale_max": 10},
                "chan": {"status": "partial", "score": 0, "scale_max": 10},
            },
        },
    }

    enriched = agent._enrich_legacy_trace_provisional_score(payload)

    assert enriched is not payload
    assert enriched["composite_verdict"]["score"] is None
    assert enriched["composite_verdict"]["verdict_status"] == "not_evaluable"
    assert enriched["composite_verdict"]["provisional_score"] == 0
    assert enriched["composite_verdict"]["components"]["s4"]["provisional_score"] == 0
    assert enriched["composite_verdict"]["components"]["s4"]["provisional_breakdown"] == {
        "region": 0,
        "resonance": 0,
        "volume_ratio": 0,
        "volatility": 0,
        "gain_10d": 0,
        "golden_cross": 0,
    }
    assert enriched["composite_verdict"]["formalization_reason_codes"] == [
        "S4_WEEKLY_BAR_UNCONFIRMED"
    ]
    assert "BACKTEST_UNAVAILABLE" in enriched["composite_verdict"]["quality_warning_codes"]
    assert "provisional_score" not in payload["composite_verdict"]


def test_trace_history_quarantines_legacy_debate_marked_success_after_parse_failure():
    payload = {
        "enabled": True,
        "success": True,
        "error": None,
        "judge_decision": {"winner": "insufficient_data", "final_action": "monitor"},
        "debug_outputs": {
            "primary_report_raw": "过短的旧主报告",
            "judge_raw": "",
            "judge_parse_error": "llm_json_parse_failed:judge_decision",
        },
    }

    quarantined = agent._quarantine_legacy_debate_parse_failure(payload)

    assert quarantined is not payload
    assert quarantined["success"] is False
    assert quarantined["error"] == "llm_json_parse_failed:judge_decision"
    assert quarantined["read_time_quarantine"] == {
        "type": "legacy_debate_parse_failure",
        "persisted": False,
    }
    assert payload["success"] is True


def test_trace_history_refreshes_successful_debate_appendix_at_read_time():
    content = (
        "## 最终结论\n\n等待。\n\n"
        "## 对抗式辩论裁决\n\n"
        "| 维度 | 结论 | 权重 | 证据摘要 | 缺口 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 基本面 | - | - | 业绩预告首亏 | - |\n\n"
        "<!-- deterministic-price-plan:start -->\n"
        "## 确定性价格计划\n"
        "<!-- deterministic-price-plan:end -->"
    )
    debate = {
        "enabled": True,
        "success": True,
        "primary_thesis": {},
        "opposing_thesis": {},
        "judge_decision": {
            "winner": "opposing",
            "final_action": "reject",
            "decision_summary": "基本面否决执行",
            "dimension_assessments": [
                {
                    "dimension": "fundamental",
                    "plan_verdict": "hard_veto",
                    "summary": "业绩预告首亏",
                    "missing": [],
                }
            ],
        },
    }

    refreshed = agent._refresh_trace_debate_presentation(content, debate)

    assert refreshed.count("## 对抗式辩论裁决") == 1
    assert "| 维度 | 对价格计划结论 | 证据摘要 | 缺口 |" in refreshed
    assert "| 基本面 | 硬否决 | 业绩预告首亏 | - |" in refreshed
    assert "| 维度 | 结论 | 权重 |" not in refreshed
    assert refreshed.index("## 对抗式辩论裁决") < refreshed.index(
        "<!-- deterministic-price-plan:start -->"
    )


def test_trace_risk_gate_fails_closed_when_composite_target_mismatches():
    composite_payload = {
        "status": "failed",
        "error_code": "target_symbol_mismatch",
        "expected_symbol": "600519",
        "actual_symbol": "600487",
        "observed_tool_symbols": ["600487"],
    }

    payload = agent._build_trace_risk_gate_payload(
        result=_trace_result(),
        context=_trace_context(),
        tool_calls=[{
            "tool": "analyze_trend",
            "success": True,
            "arguments": {"stock_code": "600487"},
        }],
        composite_payload=composite_payload,
    )

    assert payload["status"] == "failed"
    assert payload["error_code"] == "target_symbol_mismatch"
    assert payload["expected_symbol"] == "600519"
    assert payload["actual_symbol"] == "600487"


def _deterministic_plan_projection(result):
    from datetime import date
    from decimal import Decimal

    from src.agent.pricing.schemas import GenerationStatus, ScenarioRole

    scenario = SimpleNamespace(
        scenario_id="scenario-math",
        role=ScenarioRole.PRIMARY,
        source_families=["trend", "price_structure"],
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("96"),
        target_1=Decimal("108"),
        invalidation_rules=[SimpleNamespace(rule_type="close_at_or_below_stop")],
        confirmation_deadline=date(2026, 8, 20),
        confirmation_valid_trading_days=5,
        order_valid_trading_days=2,
    )
    price_plan = SimpleNamespace(
        symbol="600519",
        plan_id="plan-math",
        scenarios=[scenario],
        generation_status=GenerationStatus.GENERATED,
    )
    account_decision = SimpleNamespace(
        execution_status="risk_blocked",
        reason_codes=["RISK_GATE_PENDING"],
        target_position_pct=Decimal("10"),
    )
    context = _trace_context()
    return agent._project_price_plan_to_legacy_trade_plan(
        price_plan=price_plan,
        result=result,
        context=context,
        agent_user_context=context["agent_user_context"],
        selected_scenario_id="scenario-math",
        account_decision=account_decision,
    )


def test_trace_trade_plan_ignores_agent_price_sidecar():
    result = SimpleNamespace(
        dashboard=None,
        debate=None,
        stock_selection=None,
        trade_plan={
            "symbol": "600519",
            "action": "open",
            "entry_zone_low": 1470.0,
            "entry_zone_high": 1500.0,
            "stop_loss_price": 1420.0,
            "invalidation_conditions": ["日线收盘跌破 1420"],
        },
    )
    plan = _deterministic_plan_projection(result)

    assert plan.action == "wait"
    assert plan.current_action == "wait"
    assert plan.entry_zone_low == 100.0
    assert plan.entry_zone_high == 101.0
    assert plan.stop_loss_price == 96.0
    assert plan.take_profit_price == 108.0
    assert plan.metadata["price_source"] == "deterministic_price_plan"
    assert not hasattr(agent, "_build_trace_trade_plan")


def test_trace_trade_plan_ignores_dashboard_entry_zone():
    result = SimpleNamespace(
        dashboard={
            "decision_type": "buy",
            "dashboard": {
                "sniper_points": {
                    "ideal_buy": 1500.0,
                    "secondary_buy": 1470.0,
                    "stop_loss": 1420.0,
                }
            },
        },
        debate=None,
        stock_selection=None,
        trade_plan=None,
    )
    plan = _deterministic_plan_projection(result)

    assert plan.action == "open"
    assert plan.entry_zone_low == 100.0
    assert plan.entry_zone_high == 101.0
    assert plan.stop_loss_price == 96.0
    assert plan.metadata["plan_id"] == "plan-math"


def test_trace_finalize_reapplies_market_regime_to_prefetched_snapshot():
    context = _trace_context()
    context["trace_composite_verdict"] = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "600519", "as_of": "2026-08-09"},
        "composite_verdict": {"score": 15, "shadow_mode": True},
    }
    rescored = SimpleNamespace(
        model_dump=lambda **_kwargs: {
            "score": 15,
            "shadow_mode": True,
            "market_coefficient": 0.3,
        }
    )
    tool_calls = [{
        "tool": "detect_market_regime",
        "success": True,
        "result_json": {"regime": "risk_off", "volatility_bucket": "high"},
    }]

    with patch("api.v1.endpoints.agent.evaluate_composite_verdict", return_value=rescored) as evaluator:
        payload = agent._build_trace_composite_verdict_payload(
            result=_trace_result(),
            context=context,
            tool_calls=tool_calls,
        )

    assert payload["composite_verdict"]["market_coefficient"] == 0.3
    assert evaluator.call_args.kwargs["market_regime"] == {
        "regime": "risk_off",
        "volatility_bucket": "high",
    }


def test_trace_risk_gate_records_composite_shadow_audit_without_changing_action():
    composite_payload = {
        "schema_version": "stock-composite-shadow.v1",
        "status": "ok",
        "snapshot": {"symbol": "600519", "as_of": "2026-08-05"},
        "composite_verdict": {
            "verdict_status": "not_evaluable",
            "score": None,
            "scale_max": 20,
            "shadow_mode": True,
            "target_position_cap_pct": None,
        },
    }

    payload = agent._build_trace_risk_gate_payload(
        result=_trace_result(),
        context=_trace_context(),
        tool_calls=[],
        composite_payload=composite_payload,
    )

    assert payload["risk_gate"]["allowed_action"] == "hold"
    check = next(
        item
        for item in payload["risk_gate"]["checks"]
        if item["rule_id"] == "composite_verdict_ceiling"
    )
    assert check["passed"] is True
    assert check["details"]["clamp_applied"] is False
