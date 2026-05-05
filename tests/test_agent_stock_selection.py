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
    capital_flow_calls = [call for call in result.tool_calls_log if call["tool"] == "get_capital_flow"]
    assert capital_flow_calls
    assert all(call["success"] is False for call in capital_flow_calls)
    assert "get_capital_flow" in result.context.evidence_ledger["summary"]["failed_tools"]


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
