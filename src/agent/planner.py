# -*- coding: utf-8 -*-
"""Planning-execute helpers for account-aware Agent analysis.

This module is intentionally deterministic: it builds the first planning
envelope from ``AgentUserContext`` and the currently registered tool names.
LLM reasoning still happens inside the existing Agent executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.agent.tools.registry import ToolRegistry
from src.schemas.agent_context import AgentUserContext


CapabilityName = str


CAPABILITY_TOOL_MAP: Dict[CapabilityName, List[str]] = {
    "watchlist_discovery": ["discover_watchlist_candidates"],
    "technical_analysis": [
        "analyze_trend",
        "calculate_ma",
        "get_tushare_stk_factor",
        "get_volume_analysis",
        "analyze_pattern",
        "analyze_price_structure",
    ],
    "realtime_quote": ["get_realtime_quote"],
    "portfolio_context": ["get_portfolio_snapshot"],
    "news_event": ["search_comprehensive_intel", "search_stock_news"],
    "sentiment_analysis": [
        "get_market_sentiment_snapshot",
        "get_sentiment_heat_candidates",
        "scan_global_risk_events",
        "score_stock_news_sentiment",
    ],
    "capital_flow": [
        "get_capital_flow",
        "get_board_capital_flow",
        "get_market_capital_flow",
        "get_tushare_moneyflow_mkt_dc",
        "get_northbound_capital_flow",
        "get_margin_trading_summary",
    ],
    "fundamental_analysis": [
        "get_stock_info",
        "get_tushare_daily_basic",
        "get_tushare_financial_indicators",
        "get_tushare_financial_statements",
    ],
    "chip_distribution": ["get_chip_distribution"],
    "symbol_regime_probability": ["get_symbol_regime_probability"],
    "regime_detection": [
        "detect_market_regime",
        "get_market_sentiment_snapshot",
        "get_market_indices",
        "get_sector_rankings",
        "get_volume_analysis",
        "scan_global_risk_events",
    ],
    "market_context": ["get_market_indices", "get_sector_rankings", "get_board_capital_flow"],
    "backtest_memory": [
        "get_skill_backtest_summary",
        "get_strategy_backtest_summary",
        "get_stock_backtest_summary",
    ],
}


CAPABILITY_PURPOSES: Dict[CapabilityName, str] = {
    "watchlist_discovery": "在用户未提供股票代码时生成可继续分析的候选股池。",
    "technical_analysis": "判断趋势、均线、量价、K线形态、缠论和 SMC 价格结构是否支持行动。",
    "realtime_quote": "确认当前价格、涨跌幅和盘中状态。",
    "portfolio_context": "读取账户、仓位、成本、浮盈亏和风险约束。",
    "news_event": "排查近期公告、新闻、风险事件和催化因素。",
    "sentiment_analysis": "汇总市场情绪、人气热度和全球风险，约束是否追涨、开仓强度和候选池热度来源。",
    "capital_flow": "观察个股主力、市场资金、北向资金和两融环境是否支持当前信号。",
    "fundamental_analysis": "补充公司基本面和估值背景。",
    "chip_distribution": "评估筹码成本、集中度和获利盘压力。",
    "symbol_regime_probability": "补充单股在当前市场 regime 下的历史 forward-return、路径画像和买回参考弱证据。",
    "regime_detection": "用指数、板块和量价数据判断市场环境对动作的约束。",
    "market_context": "补充指数和板块轮动背景。",
    "backtest_memory": "参考策略或股票历史表现，校准信号可靠性。",
}


CAPABILITY_EXPECTED_RESULTS: Dict[CapabilityName, str] = {
    "watchlist_discovery": "候选池 candidates、入池理由、召回来源、质量标记、硬排除项和容量约束。",
    "technical_analysis": "趋势方向、均线/量价/形态/价格结构、关键支撑压力和可能失效条件。",
    "realtime_quote": "当前或最新可用价格、涨跌幅、成交量、行情日期和交易时段新鲜度。",
    "portfolio_context": "账户权益、现金、目标持仓数量/成本/仓位/浮盈亏和账户风险约束。",
    "news_event": "近期公告、新闻、监管、减持、业绩或主题催化及其正负面方向。",
    "sentiment_analysis": "市场情绪快照、热度候选、全球风险事件、正负面强度和数据源质量。",
    "capital_flow": "个股主力资金、板块/市场资金、北向资金和两融环境的方向、持续性与口径说明。",
    "fundamental_analysis": "公司基本信息、估值、盈利质量、财务指标和报表风险点。",
    "chip_distribution": "筹码成本、获利盘比例、集中度、支撑位和压力位。",
    "symbol_regime_probability": "当前 market regime 下的 forward-return 分布、路径画像和买回参考弱证据。",
    "regime_detection": "市场 regime、指数趋势、波动、广度、流动性和整体风险等级。",
    "market_context": "指数强弱、板块轮动、行业位置和大盘/板块资金背景。",
    "backtest_memory": "策略、技能或个股历史信号表现，用于校准本次证据权重。",
}


CAPABILITY_DOWNSTREAM_USES: Dict[CapabilityName, str] = {
    "watchlist_discovery": "仅作为 L1 入池依据，交给候选筛选和逐股深度分析，不直接形成买入结论。",
    "technical_analysis": "用于判断行动方向、关键价位、止损/加仓条件和是否存在趋势失效。",
    "realtime_quote": "用于校准报告中的当前价、追高风险、仓位动作和行情时效说明。",
    "portfolio_context": "用于约束仓位、回撤、止损距离、加减仓空间和账户适配性。",
    "news_event": "用于验证催化或风险是否足以推翻技术/资金假设，并写入风险提醒。",
    "sentiment_analysis": "用于调节候选池权重和动作强度；情绪热度不能单独形成买入结论。",
    "capital_flow": "用于确认承接、出货、市场资金水位和板块共振，不同口径必须分开解释。",
    "fundamental_analysis": "用于中线逻辑、估值安全边际和是否允许从短线信号升级为持有逻辑。",
    "chip_distribution": "用于判断套牢/获利盘压力、支撑位可靠性和止损距离。",
    "symbol_regime_probability": "仅作为概率弱证据，辅助点位和复查节奏，不能覆盖硬风控。",
    "regime_detection": "用于约束是否开仓、是否降仓、是否降低结论强度和整体风险等级。",
    "market_context": "用于解释个股表现是否受大盘或板块拖累/共振。",
    "backtest_memory": "用于降低或提高策略信号权重，不直接作为买卖依据。",
}


CAPABILITY_FAILURE_FALLBACKS: Dict[CapabilityName, str] = {
    "watchlist_discovery": "若候选池为空、失败或只有 fallback，最终降级为观察池/候选不足，不输出具体买入组合。",
    "technical_analysis": "若核心技术工具失败，使用已有行情/结构证据降级；仍缺关键价位时不得给强动作。",
    "realtime_quote": "若实时行情失败，使用最新可用历史价并显式标注时效；无价格时不得给盘中动作。",
    "portfolio_context": "若账户上下文缺失，按无持仓/未知仓位保守输出，不给账户级加减仓比例。",
    "news_event": "若新闻/公告失败，明确事件风险未覆盖，降低催化或风险判断强度。",
    "sentiment_analysis": "若情绪或全球风险工具失败，保留技术/资金主链，但不得声称市场风险偏好或热点确认。",
    "capital_flow": "若资金工具失败，保留技术/事件判断但不得声称资金确认；不同来源部分失败时标注口径缺口。",
    "fundamental_analysis": "若财务/估值数据缺失，入场结论不得升级为中线强持有逻辑。",
    "chip_distribution": "若筹码数据缺失，支撑/压力只能使用技术位替代并降低确定性。",
    "symbol_regime_probability": "若概率工具失败，删除买回参考弱证据，不影响硬风控判断。",
    "regime_detection": "若市场 regime 缺失，使用指数/板块工具替代；仍缺失时降低所有主动交易动作。",
    "market_context": "若板块/指数背景缺失，报告标注环境证据不足，不把个股信号外推到板块共振。",
    "backtest_memory": "若历史信号缺失，只移除校准项，不阻断主证据链。",
}


@dataclass(frozen=True)
class CapabilityToolPlan:
    """Actual tools selected for one capability."""

    capability: str
    tools: List[str]
    purpose: str
    expected_result: str
    downstream_use: str
    fallback_on_failure: str
    next_step: str = ""
    required: bool = True
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
    main_dimension: Optional[str] = None
    supporting_dimensions: List[str] = field(default_factory=list)
    skipped_dimensions: List[Dict[str, str]] = field(default_factory=list)
    hypotheses: List[Dict[str, str]] = field(default_factory=list)
    stop_conditions: List[str] = field(default_factory=list)
    replan_policy: Dict[str, Any] = field(default_factory=dict)
    missing_tools: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "intent": self.intent,
            "primary_symbol": self.primary_symbol,
            "has_position": self.has_position,
            "main_dimension": self.main_dimension,
            "supporting_dimensions": list(self.supporting_dimensions),
            "skipped_dimensions": [dict(item) for item in self.skipped_dimensions],
            "hypotheses": [dict(item) for item in self.hypotheses],
            "capabilities": list(self.capabilities),
            "required_tools": list(self.required_tools),
            "risk_checks": list(self.risk_checks),
            "stop_conditions": list(self.stop_conditions),
            "replan_policy": dict(self.replan_policy),
            "expected_output": self.expected_output,
            "tool_execution_plan": [
                {
                    "capability": item.capability,
                    "tools": list(item.tools),
                    "purpose": item.purpose,
                    "expected_result": item.expected_result,
                    "downstream_use": item.downstream_use,
                    "fallback_on_failure": item.fallback_on_failure,
                    "next_step": item.next_step,
                    "required": item.required,
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
        expected_result=CAPABILITY_EXPECTED_RESULTS.get(capability, "返回本次分析可引用的结构化证据摘要。"),
        downstream_use=CAPABILITY_DOWNSTREAM_USES.get(capability, "交给后续证据综合，用于支持或削弱当前假设。"),
        fallback_on_failure=CAPABILITY_FAILURE_FALLBACKS.get(capability, "记录缺失原因并降低最终结论强度。"),
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
    raw_tool_plan = [
        get_tools_for_capability(capability, tool_registry=tool_registry)
        for capability in capabilities
    ]
    tool_plan = [
        replace(item, next_step=_next_step_for_capability(capabilities, index, intent))
        for index, item in enumerate(raw_tool_plan)
    ]
    required_tools = _dedupe(tool for item in tool_plan for tool in item.tools)
    missing_tools = _dedupe(tool for item in tool_plan for tool in item.missing_tools)
    main_dimension = _select_main_dimension(intent, has_position, primary_symbol, capabilities)

    return PlanningResult(
        intent=intent,
        primary_symbol=primary_symbol,
        has_position=has_position,
        main_dimension=main_dimension,
        supporting_dimensions=[capability for capability in capabilities if capability != main_dimension],
        skipped_dimensions=_select_skipped_dimensions(intent, has_position, primary_symbol, capabilities),
        hypotheses=_select_hypotheses(intent, has_position, primary_symbol),
        capabilities=capabilities,
        tool_plan=tool_plan,
        required_tools=required_tools,
        risk_checks=_select_risk_checks(intent, has_position),
        stop_conditions=_select_stop_conditions(intent, has_position, primary_symbol),
        replan_policy=_build_replan_policy(intent),
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
            "regime_detection",
            "sentiment_analysis",
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
    if primary_symbol and intent in {"position_review", "entry_analysis"}:
        capabilities.append("symbol_regime_probability")
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


def _select_main_dimension(
    intent: str,
    has_position: bool,
    primary_symbol: Optional[str],
    capabilities: Sequence[str],
) -> Optional[str]:
    preferred_by_intent = {
        "position_review": "portfolio_context" if has_position else "technical_analysis",
        "entry_analysis": "technical_analysis" if primary_symbol else "watchlist_discovery",
        "watchlist_scan": "watchlist_discovery",
        "event_impact": "news_event",
        "risk_review": "portfolio_context" if has_position else "regime_detection",
        "qa": "portfolio_context" if has_position else None,
    }
    preferred = preferred_by_intent.get(intent)
    if preferred in capabilities:
        return preferred
    return capabilities[0] if capabilities else None


def _select_skipped_dimensions(
    intent: str,
    has_position: bool,
    primary_symbol: Optional[str],
    capabilities: Sequence[str],
) -> List[Dict[str, str]]:
    skipped: List[Dict[str, str]] = []
    if not has_position and "portfolio_context" not in capabilities:
        skipped.append({
            "capability": "portfolio_context",
            "reason": "本次未识别到目标持仓，账户持仓不是主证据；如用户要求仓位分配，仅使用投资者约束。",
        })
    if intent == "position_review" and "fundamental_analysis" not in capabilities:
        skipped.append({
            "capability": "fundamental_analysis",
            "reason": "持仓复盘优先回答仓位、趋势和风险动作；基本面只有在中线逻辑争议时追加。",
        })
    if intent == "watchlist_scan" and primary_symbol:
        skipped.append({
            "capability": "watchlist_discovery",
            "reason": "用户已给出明确标的时不启动全市场候选发现。",
        })
    return skipped


def _select_hypotheses(intent: str, has_position: bool, primary_symbol: Optional[str]) -> List[Dict[str, str]]:
    symbol_text = primary_symbol or "目标标的"
    if intent == "watchlist_scan":
        return [
            {"id": "H1", "text": "候选发现只解决入池召回，不等于买入推荐。"},
            {"id": "H2", "text": "只有逐股证据确认入场条件、风控条件和账户适配后，才允许输出可执行计划。"},
            {"id": "H3", "text": "若候选池不足、只有 fallback 或深度证据冲突，最终应降级为观察池。"},
        ]
    if intent == "event_impact":
        return [
            {"id": "H1", "text": f"{symbol_text} 的事件影响需要先确认公司级事实和时间窗口。"},
            {"id": "H2", "text": "若事件证据与行情/资金反应不一致，应降低结论强度。"},
            {"id": "H3", "text": "若事件属于未验证主题观察，不得直接推导买入动作。"},
        ]
    if intent == "risk_review":
        return [
            {"id": "H1", "text": "账户风险优先由仓位、回撤、现金和市场环境共同决定。"},
            {"id": "H2", "text": "若市场 regime 或持仓集中度恶化，应先降低风险暴露。"},
            {"id": "H3", "text": "若关键账户数据缺失，只能给保守风险提示。"},
        ]
    if intent == "entry_analysis":
        return [
            {"id": "H1", "text": f"{symbol_text} 只有在趋势、资金、事件和基本面不冲突时才可考虑开仓。"},
            {"id": "H2", "text": "若价格偏离支撑、风险收益比不足或市场环境转弱，应等待回撤或拒绝追高。"},
            {"id": "H3", "text": "若主证据缺失，最终动作必须降级为 WAIT/MONITOR/REJECT。"},
        ]
    if intent == "position_review" or has_position:
        return [
            {"id": "H1", "text": f"{symbol_text} 是否继续持有取决于仓位风险、成本缓冲和趋势是否仍支撑。"},
            {"id": "H2", "text": "若价格结构、资金或事件风险转弱，需要降低仓位或设置止损条件。"},
            {"id": "H3", "text": "若市场环境和个股证据共振，才考虑维持或小幅加仓。"},
        ]
    return [
        {"id": "H1", "text": "先确认用户问题是否需要工具证据。"},
        {"id": "H2", "text": "若没有足够上下文或工具证据，不输出强交易结论。"},
    ]


def _select_stop_conditions(intent: str, has_position: bool, primary_symbol: Optional[str]) -> List[str]:
    base = [
        "主维度证据已覆盖用户问题，继续调用工具不会改变最终动作。",
        "关键工具失败后已记录原因，并确认没有可用替代证据。",
        "出现足以改变动作的强反证，应停止扩展并输出保守建议。",
    ]
    if intent == "watchlist_scan":
        return [
            "候选发现已返回可复核候选池，并完成必要逐股行情、技术、消息和资金证据。",
            "候选池为空、只有 fallback 或深度证据不足时，停止强推荐并降级为观察池。",
            *base,
        ]
    if intent in {"position_review", "entry_analysis"} and primary_symbol:
        return [
            "已获得目标标的的价格/时效、技术结构、主要风险事件和必要资金/账户约束。",
            "新增辅助维度不会改变 OPEN/WAIT/HOLD/REDUCE/REJECT 等动作判断。",
            *base,
        ]
    if intent == "risk_review" or has_position:
        return [
            "账户、持仓、仓位集中度、回撤压力和市场环境足以回答风险问题。",
            *base,
        ]
    return base


def _build_replan_policy(intent: str) -> Dict[str, Any]:
    return {
        "mode": "bounded_execute_replan",
        "trigger_conditions": [
            "计划内核心工具失败、超时、空结果或字段不足。",
            "工具结果出现推翻当前假设的强反证。",
            "主维度仍缺关键证据且存在同 capability 替代工具。",
            "已满足停止条件，应停止补工具并进入综合。",
        ],
        "allowed_actions": [
            "补调用同一 capability 内能验证关键缺口的替代工具。",
            "新增只验证强反证或主维度缺口的工具，并记录新增目的。",
            "跳过不会改变动作结论的辅助维度。",
            "记录缺失、影响和保守降级结论。",
        ],
        "forbidden_actions": [
            "同一工具同一参数无新信息重复调用。",
            "为了全面分析扩展无关能力域。",
            "在主维度关键证据缺失时输出强交易动作。",
        ],
        "artifact_update": "新增、跳过、失败或降级路径必须写入 todo.md 执行状态和 evidence_ledger.json。",
        "watchlist_note": (
            "watchlist_scan 的 replan 只能在候选发现、逐股取证、组合配置之间补缺口；"
            "不得把 L1 入池分当作买入推荐。"
        ) if intent == "watchlist_scan" else "",
    }


def _next_step_for_capability(capabilities: Sequence[str], index: int, intent: str) -> str:
    current = capabilities[index] if index < len(capabilities) else ""
    next_capability = capabilities[index + 1] if index + 1 < len(capabilities) else ""
    if next_capability:
        return f"将 {current} 的结果交给 {next_capability}，只补会改变最终动作的缺口。"
    if intent == "watchlist_scan":
        return "进入候选排序、组合配置、反方审查和 Judge 裁决；证据不足则降级观察池。"
    return "进入风险检查、停止条件复核和最终报告综合；证据不足则降低结论强度。"


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
