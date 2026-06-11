# -*- coding: utf-8 -*-
"""
Agent Executor — ReAct loop with tool calling.

Orchestrates the LLM + tools interaction loop:
1. Build system prompt (persona + tools + skills)
2. Send to LLM with tool declarations
3. If tool_call → execute tool → feed result back
4. If text → parse as final answer
5. Loop until final answer or max_steps

The core execution loop is delegated to :mod:`src.agent.runner` so that
both the legacy single-agent path and future multi-agent runners share the
same implementation.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.agent.debate import DebateResult, format_debate_appendix, run_adversarial_debate, should_run_debate
from src.agent.llm_adapter import LLMToolAdapter
from src.agent.planner import build_planning_result
from src.agent.planning_prompts import PromptBuildOptions, build_planning_system_prompt
from src.agent.runner import run_agent_loop, parse_dashboard_json
from src.agent.stock_selection import run_stock_selection_pipeline, should_run_stock_selection
from src.agent.tools.registry import ToolRegistry
from src.schemas.agent_context import AgentUserContext
from src.report_language import normalize_report_language
from src.market_context import get_market_role, get_market_guidelines

logger = logging.getLogger(__name__)


# ============================================================
# Agent result
# ============================================================

@dataclass
class AgentResult:
    """Result from an agent execution run."""
    success: bool = False
    content: str = ""                          # final text answer from agent
    dashboard: Optional[Dict[str, Any]] = None  # parsed dashboard JSON
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)  # execution trace
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    model: str = ""                            # comma-separated models used (supports fallback)
    error: Optional[str] = None
    debate: Optional[Dict[str, Any]] = None
    stock_selection: Optional[Dict[str, Any]] = None


# ============================================================
# System prompt builder
# ============================================================

LEGACY_DEFAULT_AGENT_SYSTEM_PROMPT = """你是一位专注于趋势交易的{market_role}投资分析 Agent，拥有数据工具和交易技能，负责生成专业的【决策仪表盘】分析报告。

{market_guidelines}

## 工作流程（必须严格按阶段顺序执行，每阶段等工具结果返回后再进入下一阶段）

**第一阶段 · 行情与K线**（首先执行）
- `get_realtime_quote` 获取实时行情
- `get_daily_history` 获取历史K线

**第二阶段 · 技术与筹码**（等第一阶段结果返回后执行）
- `analyze_trend` 获取技术指标
- `get_chip_distribution` 获取筹码分布

**第三阶段 · 情报搜索**（等前两阶段完成后执行）
- `search_stock_news` 搜索最新资讯、减持、业绩预告等风险信号

**第四阶段 · 生成报告**（所有数据就绪后，输出完整决策仪表盘 JSON）

> ⚠️ 每阶段的工具调用必须完整返回结果后，才能进入下一阶段。禁止将不同阶段的工具合并到同一次调用中。
{default_skill_policy_section}

## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **系统化分析** — 严格按工作流程分阶段执行，每阶段完整返回后再进入下一阶段，**禁止**将不同阶段的工具合并到同一次调用中。
3. **应用交易技能** — 评估每个激活技能的条件，在报告中体现技能判断结果。
4. **输出格式** — 最终响应必须是有效的决策仪表盘 JSON。
5. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
6. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。
7. **精确数值证据约束** — 主力资金、筹码分布、均线、市值、营收、利润、股数等精确值必须逐项来自工具返回；缺少工具证据时写 `N/A` 或“数据缺失”，禁止用看起来合理的小数补齐。
8. **A股交易规则** — A股买入数量必须是 100 股整数倍；不要输出 146 股这类无法下单的买入建议，改用仓位比例或“按100股整数倍”。

{skills_section}

## 输出格式：决策仪表盘 JSON

你的最终响应必须是以下结构的有效 JSON 对象：

```json
{{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",
    "dashboard": {{
        "core_conclusion": {{
            "one_sentence": "一句话核心结论（30字以内）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {{
                "no_position": "空仓者建议",
                "has_position": "持仓者建议"
            }}
        }},
        "data_perspective": {{
            "trend_status": {{"ma_alignment": "", "is_bullish": true, "trend_score": 0}},
            "price_position": {{"current_price": 0, "ma5": 0, "ma10": 0, "ma20": 0, "bias_ma5": 0, "bias_status": "", "support_level": 0, "resistance_level": 0}},
            "volume_analysis": {{"volume_ratio": 0, "volume_status": "", "turnover_rate": 0, "volume_meaning": ""}},
            "chip_structure": {{"profit_ratio": 0, "avg_cost": 0, "concentration": 0, "chip_health": ""}}
        }},
        "intelligence": {{
            "latest_news": "",
            "risk_alerts": [],
            "positive_catalysts": [],
            "earnings_outlook": "",
            "sentiment_summary": ""
        }},
        "battle_plan": {{
            "sniper_points": {{"ideal_buy": "", "secondary_buy": "", "stop_loss": "", "take_profit": ""}},
            "position_strategy": {{"suggested_position": "", "entry_plan": "", "risk_control": ""}},
            "action_checklist": []
        }}
    }},
    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用交易理念",
    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点"
}}
```

## 精确数值证据约束

- 主力资金、筹码分布、均线、市值、营收、利润、股数等精确值必须逐项来自工具返回；缺少工具证据时写 `N/A` 或“数据缺失”，禁止用看起来合理的小数补齐。
- A股买入数量必须是 100 股整数倍；不要输出 146 股这类无法下单的买入建议，改用仓位比例或“按100股整数倍”。

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多头排列：MA5 > MA10 > MA20
- ✅ 低乖离率：<2%，最佳买点
- ✅ 缩量回调或放量突破
- ✅ 筹码集中健康
- ✅ 消息面有利好催化

### 买入（60-79分）：
- ✅ 多头排列或弱势多头
- ✅ 乖离率 <5%
- ✅ 量能正常
- ⚪ 允许一项次要条件不满足

### 观望（40-59分）：
- ⚠️ 乖离率 >5%（追高风险）
- ⚠️ 均线缠绕趋势不明
- ⚠️ 有风险事件

### 卖出/减仓（0-39分）：
- ❌ 空头排列
- ❌ 跌破MA20
- ❌ 放量下跌
- ❌ 重大利空

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **证据绑定的狙击点**：具体价格必须来自工具或上游 key_levels，缺失时写 N/A
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

{language_section}
"""

AGENT_SYSTEM_PROMPT = """你是一位{market_role}投资分析 Agent，拥有数据工具和可切换交易技能，负责生成专业的【决策仪表盘】分析报告。

{market_guidelines}

## 工作流程（必须严格按阶段顺序执行，每阶段等工具结果返回后再进入下一阶段）

**第一阶段 · 行情与K线**（首先执行）
- `get_realtime_quote` 获取实时行情
- `get_daily_history` 获取历史K线

**第二阶段 · 技术与筹码**（等第一阶段结果返回后执行）
- `analyze_trend` 获取技术指标
- `get_chip_distribution` 获取筹码分布

**第三阶段 · 情报搜索**（等前两阶段完成后执行）
- `search_stock_news` 搜索最新资讯、减持、业绩预告等风险信号

**第四阶段 · 生成报告**（所有数据就绪后，输出完整决策仪表盘 JSON）

> ⚠️ 每阶段的工具调用必须完整返回结果后，才能进入下一阶段。禁止将不同阶段的工具合并到同一次调用中。
{default_skill_policy_section}

## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **系统化分析** — 严格按工作流程分阶段执行，每阶段完整返回后再进入下一阶段，**禁止**将不同阶段的工具合并到同一次调用中。
3. **应用交易技能** — 评估每个激活技能的条件，在报告中体现技能判断结果。
4. **输出格式** — 最终响应必须是有效的决策仪表盘 JSON。
5. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
6. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。

{skills_section}

## 输出格式：决策仪表盘 JSON

你的最终响应必须是以下结构的有效 JSON 对象：

```json
{{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",
    "dashboard": {{
        "core_conclusion": {{
            "one_sentence": "一句话核心结论（30字以内）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {{
                "no_position": "空仓者建议",
                "has_position": "持仓者建议"
            }}
        }},
        "data_perspective": {{
            "trend_status": {{"ma_alignment": "", "is_bullish": true, "trend_score": 0}},
            "price_position": {{"current_price": 0, "ma5": 0, "ma10": 0, "ma20": 0, "bias_ma5": 0, "bias_status": "", "support_level": 0, "resistance_level": 0}},
            "volume_analysis": {{"volume_ratio": 0, "volume_status": "", "turnover_rate": 0, "volume_meaning": ""}},
            "chip_structure": {{"profit_ratio": 0, "avg_cost": 0, "concentration": 0, "chip_health": ""}}
        }},
        "intelligence": {{
            "latest_news": "",
            "risk_alerts": [],
            "positive_catalysts": [],
            "earnings_outlook": "",
            "sentiment_summary": ""
        }},
        "battle_plan": {{
            "sniper_points": {{"ideal_buy": "", "secondary_buy": "", "stop_loss": "", "take_profit": ""}},
            "position_strategy": {{"suggested_position": "", "entry_plan": "", "risk_control": ""}},
            "action_checklist": []
        }}
    }},
    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用激活技能或风险框架",
    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点"
}}
```

## 评分标准

### 强烈买入（80-100分）：
- ✅ 多个激活技能同时支持积极结论
- ✅ 上行空间、触发条件与风险回报清晰
- ✅ 关键风险已排查，仓位与止损计划明确
- ✅ 重要数据和情报结论彼此一致

### 买入（60-79分）：
- ✅ 主信号偏积极，但仍有少量待确认项
- ✅ 允许存在可控风险或次优入场点
- ✅ 需要在报告中明确补充观察条件

### 观望（40-59分）：
- ⚠️ 信号分歧较大，或缺乏足够确认
- ⚠️ 风险与机会大致均衡
- ⚠️ 更适合等待触发条件或回避不确定性

### 卖出/减仓（0-39分）：
- ❌ 主要结论转弱，风险明显高于收益
- ❌ 触发了止损/失效条件或重大利空
- ❌ 现有仓位更需要保护而不是进攻

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出

{language_section}
"""

LEGACY_DEFAULT_CHAT_SYSTEM_PROMPT = """你是一位专注于趋势交易的{market_role}投资分析 Agent，拥有数据工具和交易技能，负责解答用户的股票投资问题。

{market_guidelines}

## 分析工作流程（必须严格按阶段执行，禁止跳步或合并阶段）

当用户询问某支股票时，必须按以下四个阶段顺序调用工具，每阶段等工具结果全部返回后再进入下一阶段：

**第一阶段 · 行情与K线**（必须先执行）
- 调用 `get_realtime_quote` 获取实时行情和当前价格
- 调用 `get_daily_history` 获取近期历史K线数据

**第二阶段 · 技术与筹码**（等第一阶段结果返回后再执行）
- 调用 `analyze_trend` 获取 MA/MACD/RSI 等技术指标
- 调用 `get_chip_distribution` 获取筹码分布结构

**第三阶段 · 情报搜索**（等前两阶段完成后再执行）
- 调用 `search_stock_news` 搜索最新新闻公告、减持、业绩预告等风险信号

**第四阶段 · 综合分析**（所有工具数据就绪后生成回答）
- 基于上述真实数据，结合激活技能进行综合研判，输出投资建议

> ⚠️ 禁止将不同阶段的工具合并到同一次调用中（例如禁止在第一次调用中同时请求行情、技术指标和新闻）。
{default_skill_policy_section}

## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **应用交易技能** — 评估每个激活技能的条件，在回答中体现技能判断结果。
3. **自由对话** — 根据用户的问题，自由组织语言回答，不需要输出 JSON。
4. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
5. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。

{skills_section}
{language_section}
"""

CHAT_SYSTEM_PROMPT = """你是一位{market_role}投资分析 Agent，拥有数据工具和可切换交易技能，负责解答用户的股票投资问题。

{market_guidelines}

## 分析工作流程（必须严格按阶段执行，禁止跳步或合并阶段）

当用户询问某支股票时，必须按以下四个阶段顺序调用工具，每阶段等工具结果全部返回后再进入下一阶段：

**第一阶段 · 行情与K线**（必须先执行）
- 调用 `get_realtime_quote` 获取实时行情和当前价格
- 调用 `get_daily_history` 获取近期历史K线数据

**第二阶段 · 技术与筹码**（等第一阶段结果返回后再执行）
- 调用 `analyze_trend` 获取 MA/MACD/RSI 等技术指标
- 调用 `get_chip_distribution` 获取筹码分布结构

**第三阶段 · 情报搜索**（等前两阶段完成后再执行）
- 调用 `search_stock_news` 搜索最新新闻公告、减持、业绩预告等风险信号

**第四阶段 · 综合分析**（所有工具数据就绪后生成回答）
- 基于上述真实数据，结合激活技能进行综合研判，输出投资建议

> ⚠️ 禁止将不同阶段的工具合并到同一次调用中（例如禁止在第一次调用中同时请求行情、技术指标和新闻）。
{default_skill_policy_section}

## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **应用交易技能** — 评估每个激活技能的条件，在回答中体现技能判断结果。
3. **自由对话** — 根据用户的问题，自由组织语言回答，不需要输出 JSON。
4. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
5. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。

{skills_section}
{language_section}
"""


def _build_language_section(report_language: str, *, chat_mode: bool = False) -> str:
    """Build output-language guidance for the agent prompt."""
    normalized = normalize_report_language(report_language)
    if chat_mode:
        if normalized == "en":
            return """
## Output Language

- Reply in English.
- If you output JSON, keep the keys unchanged and write every human-readable value in English.
"""
        return """
## 输出语言

- 默认使用中文回答。
- 若输出 JSON，键名保持不变，所有面向用户的文本值使用中文。
"""

    if normalized == "en":
        return """
## Output Language

- Keep every JSON key unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all dashboard text, checklist items, and summaries.
"""

    return """
## 输出语言

- 所有 JSON 键名保持不变。
- `decision_type` 必须保持为 `buy|hold|sell`。
- 所有面向用户的人类可读文本值必须使用中文。
"""


# ============================================================
# Agent Executor
# ============================================================

class AgentExecutor:
    """ReAct agent loop with tool calling.

    Usage::

        executor = AgentExecutor(tool_registry, llm_adapter)
        result = executor.run("Analyze stock 600519")
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        llm_adapter: LLMToolAdapter,
        skill_instructions: str = "",
        default_skill_policy: str = "",
        use_legacy_default_prompt: bool = False,
        max_steps: int = 10,
        timeout_seconds: Optional[float] = None,
        tool_call_timeout_seconds: Optional[float] = None,
        orchestration_mode: str = "legacy",
    ):
        self.tool_registry = tool_registry
        self.llm_adapter = llm_adapter
        self.skill_instructions = skill_instructions
        self.default_skill_policy = default_skill_policy
        self.use_legacy_default_prompt = use_legacy_default_prompt
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self.orchestration_mode = orchestration_mode

    def _build_system_prompt(
        self,
        context: Optional[Dict[str, Any]],
        *,
        chat_mode: bool = False,
    ) -> str:
        """Build the correct system prompt for normal or planning execution."""
        skills_section = ""
        if self.skill_instructions:
            skills_section = f"## 激活的交易技能\n\n{self.skill_instructions}"
        default_skill_policy_section = ""
        if self.default_skill_policy:
            default_skill_policy_section = f"\n{self.default_skill_policy}\n"
        report_language = normalize_report_language((context or {}).get("report_language", "zh"))
        stock_code = (context or {}).get("stock_code", "")
        market_role = get_market_role(stock_code, report_language)
        market_guidelines = get_market_guidelines(stock_code, report_language)
        agent_user_context = _coerce_agent_user_context((context or {}).get("agent_user_context"))
        planning_mode = _is_planning_execute_context(agent_user_context)

        if planning_mode:
            extra_sections = [
                f"## 市场角色\n\n你当前以{market_role}身份分析本次请求。",
                market_guidelines,
                _build_language_section(report_language, chat_mode=chat_mode),
            ]
            if default_skill_policy_section:
                extra_sections.append(default_skill_policy_section)
            if skills_section:
                extra_sections.append(skills_section)
            return build_planning_system_prompt(
                PromptBuildOptions(
                    language="en" if report_language == "en" else "zh",
                    extra_instructions="\n\n".join(section.strip() for section in extra_sections if section.strip()),
                )
            )

        if chat_mode:
            prompt_template = (
                LEGACY_DEFAULT_CHAT_SYSTEM_PROMPT
                if self.use_legacy_default_prompt
                else CHAT_SYSTEM_PROMPT
            )
        else:
            prompt_template = (
                LEGACY_DEFAULT_AGENT_SYSTEM_PROMPT
                if self.use_legacy_default_prompt
                else AGENT_SYSTEM_PROMPT
            )
        return prompt_template.format(
            market_role=market_role,
            market_guidelines=market_guidelines,
            default_skill_policy_section=default_skill_policy_section,
            skills_section=skills_section,
            language_section=_build_language_section(report_language, chat_mode=chat_mode),
        )

    def run(self, task: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """Execute the agent loop for a given task.

        Args:
            task: The user task / analysis request.
            context: Optional context dict (e.g., {"stock_code": "600519"}).

        Returns:
            AgentResult with parsed dashboard or error.
        """
        system_prompt = self._build_system_prompt(context, chat_mode=False)

        # Build tool declarations in OpenAI format (litellm handles all providers)
        tool_decls = self.tool_registry.to_openai_tools()

        # Initialize conversation
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_user_message(task, context)},
        ]

        return self._run_loop(
            messages,
            tool_decls,
            parse_dashboard=True,
            original_task=task,
            context=context,
        )

    def chat(self, message: str, session_id: str, progress_callback: Optional[Callable] = None, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """Execute the agent loop for a free-form chat message.

        Args:
            message: The user's chat message.
            session_id: The conversation session ID.
            progress_callback: Optional callback for streaming progress events.
            context: Optional context dict from previous analysis for data reuse.

        Returns:
            AgentResult with the text response.
        """
        from src.agent.conversation import conversation_manager

        system_prompt = self._build_system_prompt(context, chat_mode=True)

        # Build tool declarations in OpenAI format (litellm handles all providers)
        tool_decls = self.tool_registry.to_openai_tools()

        # Get conversation history
        session = conversation_manager.get_or_create(session_id)
        history = session.get_history()

        # Initialize conversation
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history)

        # Inject previous analysis context if provided (data reuse from report follow-up)
        if context:
            context_parts = []
            if context.get("stock_code"):
                context_parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                context_parts.append(f"股票名称: {context['stock_name']}")
            if context.get("previous_price"):
                context_parts.append(f"上次分析价格: {context['previous_price']}")
            if context.get("previous_change_pct"):
                context_parts.append(f"上次涨跌幅: {context['previous_change_pct']}%")
            if context.get("previous_analysis_summary"):
                summary = context["previous_analysis_summary"]
                summary_text = json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else str(summary)
                context_parts.append(f"上次分析摘要:\n{summary_text}")
            if context.get("previous_strategy"):
                strategy = context["previous_strategy"]
                strategy_text = json.dumps(strategy, ensure_ascii=False) if isinstance(strategy, dict) else str(strategy)
                context_parts.append(f"上次策略分析:\n{strategy_text}")
            if context_parts:
                context_msg = "[系统提供的历史分析上下文，可供参考对比]\n" + "\n".join(context_parts)
                messages.append({"role": "user", "content": context_msg})
                messages.append({"role": "assistant", "content": "好的，我已了解该股票的历史分析数据。请告诉我你想了解什么？"})

        agent_user_context = _coerce_agent_user_context((context or {}).get("agent_user_context"))
        if _is_planning_execute_context(agent_user_context):
            messages.append({"role": "user", "content": self._build_user_message(message, context)})
        else:
            messages.append({"role": "user", "content": message})

        # Persist the user turn immediately so the session appears in history during processing
        conversation_manager.add_message(session_id, "user", message)

        result = self._run_loop(
            messages,
            tool_decls,
            parse_dashboard=False,
            progress_callback=progress_callback,
            original_task=message,
            context=context,
        )

        # Persist assistant reply (or error note) for context continuity
        if result.success:
            conversation_manager.add_message(session_id, "assistant", result.content)
        else:
            error_note = f"[分析失败] {result.error or '未知错误'}"
            conversation_manager.add_message(session_id, "assistant", error_note)

        return result

    def _run_loop(
        self,
        messages: List[Dict[str, Any]],
        tool_decls: List[Dict[str, Any]],
        parse_dashboard: bool,
        progress_callback: Optional[Callable] = None,
        *,
        original_task: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """Delegate to the shared runner and adapt the result.

        This preserves the exact same observable behaviour as the original
        inline implementation while sharing the single authoritative loop
        in :mod:`src.agent.runner`.
        """
        stock_selection = self._maybe_run_stock_selection(
            context=context,
            original_task=original_task,
            progress_callback=progress_callback,
        )
        if stock_selection and (stock_selection.success or stock_selection.final_report_json):
            return AgentResult(
                success=True,
                content=stock_selection.final_markdown,
                dashboard=None,
                tool_calls_log=stock_selection.tool_calls_log,
                total_steps=len(stock_selection.tool_calls_log),
                total_tokens=stock_selection.total_tokens,
                provider="agent",
                model=", ".join(stock_selection.models_used),
                error=None,
                debate=None,
                stock_selection=stock_selection.to_dict(),
            )

        loop_result = run_agent_loop(
            messages=messages,
            tool_registry=self.tool_registry,
            llm_adapter=self.llm_adapter,
            max_steps=self.max_steps,
            progress_callback=progress_callback,
            max_wall_clock_seconds=self.timeout_seconds,
            tool_call_timeout_seconds=self.tool_call_timeout_seconds,
            tool_argument_guard=_build_context_tool_argument_guard(context),
        )

        model_str = loop_result.model

        debate = self._maybe_run_debate(
            loop_content=loop_result.content,
            loop_tool_calls=loop_result.tool_calls_log,
            context=context,
            original_task=original_task,
            progress_callback=progress_callback,
        )
        final_content = loop_result.content
        agent_user_context = _coerce_agent_user_context((context or {}).get("agent_user_context"))
        if _is_planning_execute_context(agent_user_context):
            final_content = _normalize_planning_execute_final_content(final_content)
        if debate and debate.success:
            final_content = f"{final_content.rstrip()}\n\n{format_debate_appendix(debate)}".rstrip()
            debate.debug_outputs["final_report_with_debate"] = final_content
            loop_result.total_tokens += debate.total_tokens
            for model in debate.models_used:
                if model and model not in loop_result.models_used:
                    loop_result.models_used.append(model)
        model_str = loop_result.model

        if parse_dashboard and loop_result.success:
            dashboard = parse_dashboard_json(final_content)
            return AgentResult(
                success=dashboard is not None,
                content=final_content,
                dashboard=dashboard,
                tool_calls_log=loop_result.tool_calls_log,
                total_steps=loop_result.total_steps,
                total_tokens=loop_result.total_tokens,
                provider=loop_result.provider,
                model=model_str,
                error=None if dashboard else "Failed to parse dashboard JSON from agent response",
                debate=debate.to_dict() if debate else None,
                stock_selection=stock_selection.to_dict() if stock_selection else None,
            )

        return AgentResult(
            success=loop_result.success,
            content=final_content,
            dashboard=None,
            tool_calls_log=loop_result.tool_calls_log,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
            provider=loop_result.provider,
            model=model_str,
            error=loop_result.error,
            debate=debate.to_dict() if debate else None,
            stock_selection=stock_selection.to_dict() if stock_selection else None,
        )

    def _maybe_run_stock_selection(
        self,
        *,
        context: Optional[Dict[str, Any]],
        original_task: str,
        progress_callback: Optional[Callable],
    ):
        context = context or {}
        agent_user_context = _coerce_agent_user_context(context.get("agent_user_context"))
        if not should_run_stock_selection(agent_user_context):
            return None
        return run_stock_selection_pipeline(
            task=original_task,
            agent_user_context=agent_user_context,
            tool_registry=self.tool_registry,
            llm_adapter=self.llm_adapter,
            timeout_seconds=self.timeout_seconds,
            progress_callback=progress_callback,
            run_id=context.get("session_id") or "selection-run",
            orchestration_mode=self.orchestration_mode,
            candidate_discovery_mode=context.get("candidate_discovery_mode"),
        )

    def _maybe_run_debate(
        self,
        *,
        loop_content: str,
        loop_tool_calls: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]],
        original_task: str,
        progress_callback: Optional[Callable],
    ) -> Optional[DebateResult]:
        if not loop_content or not loop_tool_calls:
            return None
        context = context or {}
        agent_user_context = _coerce_agent_user_context(context.get("agent_user_context"))
        if not should_run_debate(agent_user_context=agent_user_context, tool_calls=loop_tool_calls):
            return None
        planner = None
        if agent_user_context is not None:
            planner = build_planning_result(agent_user_context, tool_registry=self.tool_registry).to_dict()
        return run_adversarial_debate(
            task=original_task,
            primary_report=loop_content,
            agent_user_context=agent_user_context,
            planner=planner,
            tool_calls=loop_tool_calls,
            llm_adapter=self.llm_adapter,
            timeout_seconds=self.timeout_seconds,
            progress_callback=progress_callback,
        )

    def _build_user_message(self, task: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Build the initial user message."""
        parts = [task]
        if context:
            report_language = normalize_report_language(context.get("report_language", "zh"))
            agent_user_context = _coerce_agent_user_context(context.get("agent_user_context"))
            if agent_user_context is not None:
                planning_result = build_planning_result(
                    agent_user_context,
                    tool_registry=self.tool_registry,
                )
                parts.append("\n[系统生成的 AgentUserContext]\n" + agent_user_context.model_dump_json(indent=2))
                parts.append("\n[系统生成的 Planner 工具执行计划]\n" + json.dumps(planning_result.to_dict(), ensure_ascii=False, indent=2))
                parts.append(
                    "\n请先遵循 Planner 的 capability -> tools 计划调用必要工具；"
                    "如某工具缺失或返回失败，记录原因并使用已有证据降级分析。"
                )
            if context.get("stock_code"):
                parts.append(f"\n股票代码: {context['stock_code']}")
            if context.get("report_type"):
                parts.append(f"报告类型: {context['report_type']}")
            if report_language == "en":
                parts.append("输出语言: English（所有 JSON 键名保持不变，所有面向用户的文本值使用英文）")
            else:
                parts.append("输出语言: 中文（所有 JSON 键名保持不变，所有面向用户的文本值使用中文）")

            # Inject pre-fetched context data to avoid redundant fetches
            if context.get("realtime_quote"):
                parts.append(f"\n[系统已获取的实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)}")
            if context.get("chip_distribution"):
                parts.append(f"\n[系统已获取的筹码分布]\n{json.dumps(context['chip_distribution'], ensure_ascii=False)}")
            if context.get("news_context"):
                parts.append(f"\n[系统已获取的新闻与舆情情报]\n{context['news_context']}")

        parts.append("\n请使用可用工具获取缺失的数据（如历史K线、新闻等），然后以决策仪表盘 JSON 格式输出分析结果。")
        return "\n".join(parts)


def _coerce_agent_user_context(value: Any) -> Optional[AgentUserContext]:
    """Return an AgentUserContext from a model/dict payload, or None if absent."""
    if value is None:
        return None
    if isinstance(value, AgentUserContext):
        return value
    if isinstance(value, dict):
        return AgentUserContext(**value)
    return None


def _build_context_tool_argument_guard(
    context: Optional[Dict[str, Any]],
) -> Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
    """Keep account and stock identity scoped to the injected user context."""
    agent_user_context = _coerce_agent_user_context((context or {}).get("agent_user_context"))
    if agent_user_context is None:
        return None

    account_ids = [
        account.account_id
        for account in agent_user_context.accounts
        if account.account_id is not None
    ]
    selected_account_id = account_ids[0] if len(account_ids) == 1 else None
    position_names = _position_name_lookup(agent_user_context)

    def guard(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        guarded = dict(arguments or {})
        if (
            tool_name == "get_portfolio_snapshot"
            and selected_account_id is not None
            and guarded.get("account_id") in (None, "")
        ):
            guarded["account_id"] = selected_account_id
        if tool_name in {"search_stock_news", "search_comprehensive_intel", "score_stock_news_sentiment"}:
            code = str(guarded.get("stock_code") or "").strip()
            if code and code in position_names:
                guarded["stock_name"] = position_names[code]
        return guarded

    return guard


def _position_name_lookup(agent_user_context: AgentUserContext) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for position in agent_user_context.positions:
        code = str(position.symbol or "").strip()
        if not code:
            continue
        raw_name = getattr(position, "stock_name", None) or getattr(position, "name", None)
        if not raw_name and getattr(position, "model_extra", None):
            raw_name = position.model_extra.get("stock_name") or position.model_extra.get("name")
        if not raw_name:
            continue
        name = str(raw_name).strip()
        if name:
            result[code] = name
    return result


def _is_planning_execute_context(context: Optional[AgentUserContext]) -> bool:
    return bool(context and context.report.analysis_mode == "planning_execute")


_FINAL_OUTPUT_STEP_TITLE_RE = re.compile(r"(?m)^(第[一二三四五六七八九十]+(?:步|阶段))[:：][ \t]*")


def _normalize_planning_execute_final_content(content: str) -> str:
    """Remove execution-step labels from final markdown while keeping analysis text."""
    if not content:
        return content
    return _FINAL_OUTPUT_STEP_TITLE_RE.sub("", content)
