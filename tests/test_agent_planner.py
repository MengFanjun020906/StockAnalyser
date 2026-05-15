from src.agent.planner import build_planning_result, get_tools_for_capability
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry
from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} test tool",
                parameters=[ToolParameter(name="stock_code", type="string", description="stock")],
                handler=lambda **kwargs: kwargs,
            )
        )
    return registry


def test_get_tools_for_capability_filters_missing_registry_tools():
    plan = get_tools_for_capability(
        "technical_analysis",
        tool_registry=_registry("analyze_trend", "calculate_ma"),
    )

    assert plan.tools == ["analyze_trend", "calculate_ma"]
    assert "get_volume_analysis" in plan.missing_tools
    assert "analyze_price_structure" in plan.missing_tools
    assert plan.capability == "technical_analysis"


def test_get_tools_for_capability_includes_extended_capital_flow_tools():
    plan = get_tools_for_capability(
        "capital_flow",
        tool_registry=_registry(
            "get_capital_flow",
            "get_market_capital_flow",
            "get_northbound_capital_flow",
            "get_margin_trading_summary",
        ),
    )

    assert plan.tools == [
        "get_capital_flow",
        "get_market_capital_flow",
        "get_northbound_capital_flow",
        "get_margin_trading_summary",
    ]
    assert plan.missing_tools == []


def test_get_tools_for_capability_prefers_detect_market_regime():
    plan = get_tools_for_capability(
        "regime_detection",
        tool_registry=_registry(
            "detect_market_regime",
            "get_market_indices",
            "get_sector_rankings",
            "get_volume_analysis",
        ),
    )

    assert plan.tools[0] == "detect_market_regime"
    assert plan.missing_tools == []


def test_build_planning_result_selects_position_review_capabilities():
    context = AgentUserContext(
        positions=[PositionContext(symbol="600519", quantity=100, avg_cost=1500)],
        report=ReportContext(
            analysis_mode="planning_execute",
            primary_symbol="600519",
            target_symbols=["600519"],
        ),
    )

    result = build_planning_result(
        context,
        tool_registry=_registry(
            "get_portfolio_snapshot",
            "get_realtime_quote",
            "analyze_trend",
            "analyze_price_structure",
            "get_capital_flow",
            "get_market_capital_flow",
            "get_northbound_capital_flow",
            "get_margin_trading_summary",
            "detect_market_regime",
            "get_market_indices",
            "get_sector_rankings",
        ),
    )

    assert result.intent == "position_review"
    assert result.has_position is True
    assert result.expected_output == "position_review_report"
    assert result.capabilities[0] == "portfolio_context"
    assert "regime_detection" in result.capabilities
    assert "get_portfolio_snapshot" in result.required_tools
    assert "margin_pressure" in result.risk_checks
    assert "get_market_capital_flow" in result.required_tools
    assert "detect_market_regime" in result.required_tools
    assert "analyze_price_structure" in result.required_tools


def test_build_planning_result_defaults_to_entry_analysis_without_position():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            primary_symbol="AAPL",
            target_symbols=["AAPL"],
        ),
    )

    result = build_planning_result(context, tool_registry=_registry("get_realtime_quote", "get_stock_info"))

    assert result.intent == "entry_analysis"
    assert result.has_position is False
    assert result.expected_output == "entry_analysis_report"
    assert "portfolio_context" not in result.capabilities
    assert "fundamental_analysis" in result.capabilities


def test_build_planning_result_watchlist_scan_starts_with_candidate_discovery():
    context = AgentUserContext(
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="watchlist_scan",
        ),
    )

    result = build_planning_result(
        context,
        tool_registry=_registry("discover_watchlist_candidates", "detect_market_regime", "get_realtime_quote", "analyze_trend"),
    )

    assert result.intent == "watchlist_scan"
    assert result.capabilities[0] == "watchlist_discovery"
    assert result.required_tools[0] == "discover_watchlist_candidates"
    assert "regime_detection" in result.capabilities
    assert "detect_market_regime" in result.required_tools
