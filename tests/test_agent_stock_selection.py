import json
import sys
from unittest.mock import MagicMock

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()
try:
    import json_repair  # noqa: F401
except ModuleNotFoundError:
    json_repair_stub = MagicMock()
    json_repair_stub.repair_json.side_effect = lambda content, **_kwargs: content
    sys.modules["json_repair"] = json_repair_stub

from src.agent.executor import AgentExecutor
from src.agent.llm_adapter import LLMResponse
from src.agent.stock_selection import run_stock_selection_pipeline, should_run_stock_selection
from src.agent.multi_expert import AgentState, EvidenceBundle, ExpertOpinion
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry
from src.schemas.agent_context import AccountContext, AgentUserContext, InvestorProfile, ReportContext


def _registry():
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [
                {"code": "600001", "name": "测试一", "market": "cn", "source": "test"},
                {"code": "600002", "name": "测试二", "market": "cn", "source": "test"},
            ][:limit],
        },
    ))
    registry.register(ToolDefinition(
        name="detect_market_regime",
        description="regime",
        parameters=[],
        handler=lambda market="cn", persist=True: {
            "status": "ok",
            "market": market,
            "regime": "trending_up",
            "risk_level": "medium",
            "volatility_bucket": "normal",
            "sentiment_state": "neutral",
            "wyckoff_phase": "markup",
            "risk_multiplier": 1.0,
            "strategy_hints": ["趋势向上时可接受回踩确认后的顺势策略。"],
            "data_quality": "limited",
        },
    ))
    registry.register(ToolDefinition(
        name="get_market_indices",
        description="indices",
        parameters=[],
        handler=lambda region="cn": {"region": region, "indices": []},
    ))
    registry.register(ToolDefinition(
        name="get_sector_rankings",
        description="sectors",
        parameters=[],
        handler=lambda top_n=10: {"top_sectors": [{"name": "测试板块", "change_pct": 5.0}]},
    ))
    registry.register(ToolDefinition(
        name="get_realtime_quote",
        description="quote",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "name": f"{stock_code}名称",
            "price": 10.0,
            "change_pct": 1.2,
            "turnover_rate": 3.2,
            "volume_ratio": 1.1,
            "market_session": "closed_non_trading_day",
            "quote_trade_date": "2026-05-01",
        },
    ))
    registry.register(ToolDefinition(
        name="analyze_trend",
        description="trend",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "trend_status": "多头排列",
            "bias_ma5": 1.5,
            "support_levels": [9.5],
            "resistance_levels": [11.0],
        },
    ))
    registry.register(ToolDefinition(
        name="analyze_price_structure",
        description="structure",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "code": stock_code,
            "status": "ok",
            "chan": {"pen_count": 3, "center_count": 1, "latest_pens": [{"direction": "up"}]},
            "smc": {"swing_count": 5, "bos": {"status": "bullish"}, "choch": {"status": "none"}},
        },
    ))
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"stock_code": stock_code, "status": "failed", "errors": ["timeout"]},
    ))
    registry.register(ToolDefinition(
        name="get_stock_info",
        description="info",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "name": f"{stock_code}名称", "belong_boards": []},
    ))
    registry.register(ToolDefinition(
        name="get_chip_distribution",
        description="chip",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"code": stock_code, "avg_cost": 9.8, "cost_90_low": 9.0},
    ))
    registry.register(ToolDefinition(
        name="search_comprehensive_intel",
        description="intel",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {"report": f"{stock_code} 无重大利空"},
    ))
    return registry


def _context():
    return AgentUserContext(
        investor=InvestorProfile(
            risk_preference="balanced",
            trading_horizon="long_term",
            max_single_position_pct=20,
            max_total_equity_exposure_pct=80,
            default_stop_loss_pct=8,
        ),
        accounts=[
            AccountContext(
                account_id=1,
                account_name="5w账户",
                total_equity=50000,
                available_cash=50000,
                total_market_value=0,
            )
        ],
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="watchlist_scan",
            include_watchlist_ranking=True,
        ),
    )


def _json_response(payload, tokens=1):
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        usage={"total_tokens": tokens},
        provider="openai",
        model="openai/test",
    )


def test_should_run_stock_selection_only_for_planning_watchlist():
    assert should_run_stock_selection(_context()) is True
    context = _context()
    context.report.intent = "entry_analysis"
    assert should_run_stock_selection(context) is False


def test_stock_selection_pipeline_runs_all_stages_with_summaries():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "strategy": "hot_sector",
            "market": "cn",
            "candidate_count": 2,
            "summary": {"candidate_codes": ["600001", "600002"], "main_limitations": []},
            "full": {"candidates": [{"code": "600001", "name": "测试一"}, {"code": "600002", "name": "测试二"}], "missing_evidence": []},
            "full_ref": None,
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "monitor_targets": [], "rejected_targets": [], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80, "score_breakdown": {}}]},
            "full_ref": None,
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "weak", "no_chase_line": "11", "stop_loss": "9"},
            "full": {"stock": {"code": "600001", "name": "测试一", "market": "cn", "data_status": "ok"}, "action_bias": "wait", "entry_quality": {"no_chase_line": "11", "stop_loss": "9"}, "missing_evidence": ["capital_flow"]},
            "full_ref": None,
        }),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "recommended_position_count": 0, "initial_total_position_pct": 0, "reserved_cash_pct": 100, "core_reason": "等待确认", "main_constraint": "资金面缺失"},
            "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "wait", "initial_position_pct": 0, "entry_condition": "回踩确认", "stop_loss_condition": "9", "review_trigger": "下个交易日"}], "risk_controls": []},
            "full_ref": None,
        }),
        _json_response({
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "资金面缺失，不应立即买入", "top_risk_points": ["资金面缺失"], "top_evidence_gaps": ["capital_flow"], "recommended_verdict": "accept_with_changes"},
            "full": {"opposing_thesis": {"risk_points": ["资金面缺失"]}, "missing_evidence": ["capital_flow"]},
            "full_ref": None,
        }),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "等待确认", "next_step": "render_final_report"},
            "full": {"winner": "mixed", "risk_controls": ["不追高"]},
            "full_ref": None,
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-run",
    )

    assert result.success is True
    assert "选股与持仓配置报告" in result.final_markdown
    assert result.context.stage_summary("judge_decision")["final_action"] == "wait"
    assert result.context.stages["candidate_discovery"].full_ref == "candidate_discovery.json"
    assert adapter.call_text.call_count == 6
    assert result.total_tokens == 6
    assert any(call["tool"] == "discover_watchlist_candidates" for call in result.tool_calls_log)
    assert any(call["tool"] == "detect_market_regime" for call in result.tool_calls_log)
    assert result.final_report_json["market_regime"]["regime"] == "trending_up"
    assert result.final_report_json["orchestration_mode"] == "legacy"
    assert result.final_report_json["expert_state"] is None
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)
    assert "get_capital_flow" in result.context.evidence_ledger["summary"]["failed_tools"]


def test_expert_state_schema_serializes_for_trace():
    state = AgentState(
        task="选股",
        market="cn",
        orchestration_mode="expert_graph",
        evidence_bundle=EvidenceBundle(candidate_pool=[{"code": "600001", "name": "测试一"}]),
    )
    state.add_opinion(ExpertOpinion(
        expert_name="technical_expert",
        dimension="technical",
        verdict="support",
        confidence=0.8,
        summary="技术面支持",
    ))

    payload = state.to_trace_dict()

    assert payload["orchestration_mode"] == "expert_graph"
    assert payload["evidence_bundle"]["candidate_pool"][0]["name"] == "测试一"
    assert payload["expert_opinions"]["technical_expert"]["verdict"] == "support"


def test_stock_selection_expert_graph_adds_expert_opinions():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": ["600001"], "main_limitations": []},
            "full": {
                "candidates": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "source": "alphasift:capital_heat",
                        "strategy_tags": ["capital_heat", "volume_breakout"],
                        "reason_dimensions": [
                            {"dimension": "strategy", "label": "策略", "detail": "AlphaSift YAML 多因子策略入池：资金热度"},
                            {"dimension": "capital", "label": "资金面", "detail": "成交额=10亿"},
                        ],
                    }
                ],
                "missing_evidence": [],
            },
        }),
        _json_response({
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": ["600001"], "monitor_targets": [], "rejected_targets": [], "main_limitations": []},
            "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80}]},
        }),
        _json_response({
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {"code": "600001", "name": "测试一", "action_bias": "wait", "action_strength": "weak"},
            "full": {
                "stock": {"code": "600001", "name": "测试一"},
                "dimension_summary": {
                    "technical": {"verdict": "support", "summary": "多头排列"},
                    "capital_flow": {"verdict": "tool_failed", "summary": "资金流失败"},
                    "fundamental": {"verdict": "neutral", "summary": "基本面中性"},
                    "news_event": {"verdict": "neutral", "summary": "无重大利空"},
                },
                "missing_evidence": ["capital_flow"],
                "tool_failures": [{"tool": "get_capital_flow", "error": ["timeout"]}],
            },
        }),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "wait", "recommended_position_count": 0, "initial_total_position_pct": 0, "core_reason": "等待资金确认"},
            "full": {"positions_plan": [], "risk_controls": ["资金流失败时不追高"]},
        }),
        _json_response({
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "资金流缺失，不应追高"},
            "full": {"opposing_thesis": {}},
        }),
        _json_response({
            "stage": "judge_decision",
            "status": "ok",
            "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "等待确认", "next_step": "render_final_report"},
            "full": {"winner": "mixed"},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=_registry(),
        llm_adapter=adapter,
        run_id="test-expert-graph",
        orchestration_mode="expert_graph",
    )

    expert_state = result.final_report_json["expert_state"]
    assert result.success is True
    assert result.final_report_json["orchestration_mode"] == "expert_graph"
    assert expert_state["orchestration_mode"] == "expert_graph"
    assert "technical_expert" in expert_state["expert_opinions"]
    assert "capital_chip_expert" in expert_state["expert_opinions"]
    assert expert_state["expert_opinions"]["candidate_discovery_expert"]["candidate_impacts"][0]["name"] == "测试一"
    assert result.context.expert_state is not None


def test_stock_selection_expert_graph_runs_when_candidate_pool_empty():
    registry = _registry()
    registry.register(ToolDefinition(
        name="discover_watchlist_candidates",
        description="discover",
        parameters=[],
        handler=lambda market="cn", seed_symbols=None, limit=8: {
            "status": "ok",
            "market": market,
            "candidates": [],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({
            "stage": "candidate_discovery",
            "status": "ok",
            "summary": {"candidate_codes": [], "main_limitations": ["候选池为空"]},
            "full": {"candidates": [], "missing_evidence": ["discover_watchlist_candidates"]},
        }),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-empty-expert-graph",
        orchestration_mode="expert_graph",
    )

    expert_state = result.final_report_json["expert_state"]
    assert result.success is True
    assert result.final_report_json["orchestration_mode"] == "expert_graph"
    assert expert_state["orchestration_mode"] == "expert_graph"
    assert expert_state["expert_opinions"]["candidate_discovery_expert"]["verdict"] == "insufficient_data"
    assert result.context.expert_state is not None


def test_executor_uses_stock_selection_for_watchlist_scan():
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": []}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "missing_evidence": []}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    executor = AgentExecutor(_registry(), adapter, max_steps=1)
    result = executor._run_loop(
        messages=[],
        tool_decls=[],
        parse_dashboard=False,
        original_task="我现在有5w元，你帮我选股",
        context={"agent_user_context": _context()},
    )

    assert result.success is True
    assert result.stock_selection["success"] is True
    assert "选股与持仓配置报告" in result.content
    assert not adapter.call_with_tools.called


def test_stock_selection_regime_risk_off_downgrades_open_plan():
    registry = _registry()
    registry.register(ToolDefinition(
        name="detect_market_regime",
        description="regime",
        parameters=[],
        handler=lambda market="cn", persist=True: {
            "status": "ok",
            "market": market,
            "regime": "risk_off",
            "risk_level": "high",
            "volatility_bucket": "extreme",
            "sentiment_state": "fear",
            "wyckoff_phase": "markdown",
            "risk_multiplier": 1.8,
            "strategy_hints": ["risk_off 下优先降低风险暴露，开仓需等待市场确认。"],
            "data_quality": "limited",
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一", "screening_result": "deep_dive", "score": 80, "score_breakdown": {}}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "open", "action_strength": "medium"}, "full": {"stock": {"code": "600001", "name": "测试一"}, "action_bias": "open", "missing_evidence": []}}),
        _json_response({
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {"portfolio_action": "open", "recommended_position_count": 1, "initial_total_position_pct": 10, "reserved_cash_pct": 90, "core_reason": "模型认为可开仓", "main_constraint": ""},
            "full": {"positions_plan": [{"rank": 1, "code": "600001", "name": "测试一", "action": "open", "action_strength": "medium", "initial_position_pct": 10, "initial_amount": 5000, "entry_condition": "突破", "stop_loss_condition": "跌破9", "review_trigger": "明日"}], "risk_controls": []},
        }),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept_with_changes", "final_action": "wait", "decision_summary": "risk_off 等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-regime-risk-off",
    )

    allocation = result.final_report_json["portfolio_allocation"]
    assert allocation["summary"]["portfolio_action"] == "wait"
    assert allocation["summary"]["initial_total_position_pct"] == 0
    assert allocation["full"]["positions_plan"][0]["action"] == "wait"
    assert allocation["full"]["positions_plan"][0]["initial_position_pct"] == 0
    assert allocation["full"]["market_regime_constraint"]["regime"] == "risk_off"


def test_stock_selection_marks_partial_capital_flow_without_data_failed():
    registry = _registry()
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "stock_code": stock_code,
            "status": "partial",
            "main_net_inflow": None,
            "inflow_5d": None,
            "inflow_10d": None,
            "sector_rankings": {"top_inflow_sectors": [], "top_outflow_sectors": []},
            "errors": ["capital flow fetch failed"],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-partial-flow",
    )

    assert result.success is True
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)


def test_stock_selection_marks_capital_flow_with_errors_failed_even_with_data():
    registry = _registry()
    registry.register(ToolDefinition(
        name="get_capital_flow",
        description="flow",
        parameters=[ToolParameter(name="stock_code", type="string", description="code")],
        handler=lambda stock_code: {
            "stock_code": stock_code,
            "status": "ok",
            "main_net_inflow": 123.4,
            "inflow_5d": None,
            "inflow_10d": None,
            "sector_rankings": {"top_inflow_sectors": [], "top_outflow_sectors": []},
            "errors": ["capital flow stage timeout"],
        },
    ))
    adapter = MagicMock()
    adapter.call_text.side_effect = [
        _json_response({"stage": "candidate_discovery", "status": "ok", "summary": {"candidate_codes": ["600001"]}, "full": {"candidates": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "candidate_screening", "status": "ok", "summary": {"deep_dive_targets": ["600001"]}, "full": {"shortlist": [{"code": "600001", "name": "测试一"}]}}),
        _json_response({"stage": "single_stock_deep_dive", "status": "ok", "summary": {"code": "600001", "name": "测试一", "action_bias": "wait"}, "full": {"stock": {"code": "600001", "name": "测试一"}}}),
        _json_response({"stage": "portfolio_allocation", "status": "ok", "summary": {"portfolio_action": "wait", "core_reason": "等待"}, "full": {"positions_plan": []}}),
        _json_response({"stage": "adversarial_review", "status": "ok", "summary": {"opposing_summary": "等待"}, "full": {"opposing_thesis": {}}}),
        _json_response({"stage": "judge_decision", "status": "ok", "summary": {"primary_plan_verdict": "accept", "final_action": "wait", "decision_summary": "等待", "next_step": "render_final_report"}, "full": {"winner": "mixed"}}),
    ]

    result = run_stock_selection_pipeline(
        task="我现在有5w元，帮我选股",
        agent_user_context=_context(),
        tool_registry=registry,
        llm_adapter=adapter,
        run_id="test-flow-errors",
    )

    assert result.success is True
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)
