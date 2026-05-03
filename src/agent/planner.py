# -*- coding: utf-8 -*-
"""Planning-execute helpers for account-aware Agent analysis.

This module is intentionally deterministic: it builds the first planning
envelope from ``AgentUserContext`` and the currently registered tool names.
LLM reasoning still happens inside the existing Agent executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.agent.tools.registry import ToolRegistry
from src.schemas.agent_context import AgentUserContext


CapabilityName = str


CAPABILITY_TOOL_MAP: Dict[CapabilityName, List[str]] = {
    "watchlist_discovery": ["discover_watchlist_candidates"],
    "technical_analysis": [
        "analyze_trend",
        "calculate_ma",
        "get_volume_analysis",
        "analyze_pattern",
    ],
    "realtime_quote": ["get_realtime_quote"],
    "portfolio_context": ["get_portfolio_snapshot"],
    "news_event": ["search_comprehensive_intel", "search_stock_news"],
    "capital_flow": ["get_capital_flow"],
    "fundamental_analysis": ["get_stock_info"],
    "chip_distribution": ["get_chip_distribution"],
    "regime_detection": [
        "get_market_indices",
        "get_sector_rankings",
        "get_volume_analysis",
    ],
    "market_context": ["get_market_indices", "get_sector_rankings"],
    "backtest_memory": [
        "get_skill_backtest_summary",
        "get_strategy_backtest_summary",
        "get_stock_backtest_summary",
    ],
}


CAPABILITY_PURPOSES: Dict[CapabilityName, str] = {
    "watchlist_discovery": "在用户未提供股票代码时生成可继续分析的候选股池。",
    "technical_analysis": "判断趋势、均线、量价和形态是否支持行动。",
    "realtime_quote": "确认当前价格、涨跌幅和盘中状态。",
    "portfolio_context": "读取账户、仓位、成本、浮盈亏和风险约束。",
    "news_event": "排查近期公告、新闻、风险事件和催化因素。",
    "capital_flow": "观察资金流向是否支持当前信号。",
    "fundamental_analysis": "补充公司基本面和估值背景。",
    "chip_distribution": "评估筹码成本、集中度和获利盘压力。",
    "regime_detection": "用指数、板块和量价数据判断市场环境对动作的约束。",
    "market_context": "补充指数和板块轮动背景。",
    "backtest_memory": "参考策略或股票历史表现，校准信号可靠性。",
}


@dataclass(frozen=True)
class CapabilityToolPlan:
    """Actual tools selected for one capability."""

    capability: str
    tools: List[str]
    purpose: str
    missing_tools: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanningResult:
    """Planner output consumed by the Agent executor."""

    intent: str
    primary_symbol: Optional[str]
    has_position: bool
    capabilities: List[str]
    tool_plan: List[CapabilityToolPlan]
    required_tools: List[str]
    risk_checks: List[str]
    expected_output: str
    missing_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "intent": self.intent,
            "primary_symbol": self.primary_symbol,
            "has_position": self.has_position,
            "capabilities": list(self.capabilities),
            "required_tools": list(self.required_tools),
            "risk_checks": list(self.risk_checks),
            "expected_output": self.expected_output,
            "tool_execution_plan": [
                {
                    "capability": item.capability,
                    "tools": list(item.tools),
                    "purpose": item.purpose,
                    "missing_tools": list(item.missing_tools),
                }
                for item in self.tool_plan
            ],
            "missing_tools": list(self.missing_tools),
        }


def get_tools_for_capability(
    capability: str,
    *,
    tool_registry: Optional[ToolRegistry] = None,
) -> CapabilityToolPlan:
    """Expand one capability into currently available ToolRegistry tool names."""
    requested = list(CAPABILITY_TOOL_MAP.get(capability, []))
    if tool_registry is None:
        available = requested
        missing: List[str] = []
    else:
        available = [name for name in requested if name in tool_registry]
        missing = [name for name in requested if name not in tool_registry]
    return CapabilityToolPlan(
        capability=capability,
        tools=available,
        purpose=CAPABILITY_PURPOSES.get(capability, "补充本次分析所需证据。"),
        missing_tools=missing,
    )


def build_planning_result(
    context: AgentUserContext,
    *,
    tool_registry: Optional[ToolRegistry] = None,
) -> PlanningResult:
    """Build a first-pass tool execution plan from account/report context."""
    primary_symbol = _resolve_primary_symbol(context)
    intent = _resolve_intent(context, primary_symbol)
    has_position = bool(primary_symbol and context.has_position_for(primary_symbol))
    capabilities = _dedupe(_select_capabilities(intent, has_position, primary_symbol))
    tool_plan = [
        get_tools_for_capability(capability, tool_registry=tool_registry)
        for capability in capabilities
    ]
    required_tools = _dedupe(tool for item in tool_plan for tool in item.tools)
    missing_tools = _dedupe(tool for item in tool_plan for tool in item.missing_tools)

    return PlanningResult(
        intent=intent,
        primary_symbol=primary_symbol,
        has_position=has_position,
        capabilities=capabilities,
        tool_plan=tool_plan,
        required_tools=required_tools,
        risk_checks=_select_risk_checks(intent, has_position),
        expected_output=_expected_output_for_intent(intent, has_position),
        missing_tools=missing_tools,
    )


def _resolve_primary_symbol(context: AgentUserContext) -> Optional[str]:
    report = context.report
    if report.primary_symbol:
        return report.primary_symbol.strip()
    for symbol in report.target_symbols:
        if symbol and symbol.strip():
            return symbol.strip()
    for position in context.positions:
        if position.symbol and position.quantity > 0:
            return position.symbol.strip()
    return None


def _resolve_intent(context: AgentUserContext, primary_symbol: Optional[str]) -> str:
    requested = context.report.intent
    if requested != "auto":
        return requested
    if primary_symbol and context.has_position_for(primary_symbol):
        return "position_review"
    if len(context.report.target_symbols) > 1:
        return "watchlist_scan"
    return "entry_analysis" if primary_symbol else "qa"


def _select_capabilities(intent: str, has_position: bool, primary_symbol: Optional[str]) -> List[str]:
    if intent == "risk_review":
        return ["portfolio_context", "regime_detection", "market_context"]
    if intent == "watchlist_scan":
        return [
            "watchlist_discovery",
            "realtime_quote",
            "technical_analysis",
            "market_context",
            "news_event",
            "capital_flow",
        ]
    if intent == "event_impact":
        capabilities = ["news_event", "realtime_quote", "regime_detection", "capital_flow"]
        if has_position:
            capabilities.insert(0, "portfolio_context")
        return capabilities
    if intent == "qa" and not primary_symbol:
        return ["portfolio_context"] if has_position else []

    capabilities = [
        "realtime_quote",
        "technical_analysis",
        "chip_distribution",
        "capital_flow",
        "news_event",
    ]
    if has_position or intent == "position_review":
        capabilities.insert(0, "portfolio_context")
    if intent in {"position_review", "entry_analysis"}:
        capabilities.append("regime_detection")
    if intent == "entry_analysis":
        capabilities.append("fundamental_analysis")
    return capabilities


def _select_risk_checks(intent: str, has_position: bool) -> List[str]:
    checks = ["negative_news", "data_quality"]
    if intent in {"position_review", "risk_review"} or has_position:
        checks = [
            "position_size",
            "drawdown",
            "margin_pressure",
            "stop_loss_distance",
        ] + checks
    if intent in {"entry_analysis", "watchlist_scan", "event_impact"}:
        checks.extend(["market_regime", "liquidity", "chase_high_risk"])
    return _dedupe(checks)


def _expected_output_for_intent(intent: str, has_position: bool) -> str:
    if intent == "auto":
        return "position_review_report" if has_position else "entry_analysis_report"
    return f"{intent}_report"


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = [
    "CAPABILITY_TOOL_MAP",
    "CAPABILITY_PURPOSES",
    "CapabilityToolPlan",
    "PlanningResult",
    "build_planning_result",
    "get_tools_for_capability",
]
