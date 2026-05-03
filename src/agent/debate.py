# -*- coding: utf-8 -*-
"""Adversarial debate helpers for planning-execute stock analysis.

The debate stage is intentionally evidence-bound: every role receives the same
evidence bundle and is instructed not to fetch or invent extra data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.llm_adapter import LLMResponse, LLMToolAdapter
from src.agent.runner import try_parse_json
from src.schemas.agent_context import AgentUserContext


STANCE_BY_INTENT = {
    "position_review": {
        "primary": "支持持有/加仓/继续跟踪的最强观点",
        "opposing": "反对持有/加仓，主张减仓、止盈、止损或降低暴露的最强观点",
    },
    "entry_analysis": {
        "primary": "支持开仓/等待右侧确认后入场的最强观点",
        "opposing": "反对现在入场，主张等待、拒绝或降低首仓的最强观点",
    },
    "watchlist_scan": {
        "primary": "支持当前候选排序、组合配置和分批执行方案的最强观点",
        "opposing": "反对当前候选排序或仓位配置，主张持币、缩小仓位、替换候选或等待确认的最强观点",
    },
}

DEBATE_INTENTS = {"position_review", "entry_analysis", "watchlist_scan"}


@dataclass
class DebateResult:
    """Structured output from the adversarial debate stage."""

    enabled: bool = False
    success: bool = False
    skipped_reason: Optional[str] = None
    mode: str = "forced_opposition_judge"
    intent: str = "auto"
    primary_thesis: Dict[str, Any] = field(default_factory=dict)
    opposing_thesis: Dict[str, Any] = field(default_factory=dict)
    debate_rounds: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_conflicts: List[str] = field(default_factory=list)
    judge_decision: Dict[str, Any] = field(default_factory=dict)
    debug_outputs: Dict[str, Any] = field(default_factory=dict)
    total_tokens: int = 0
    models_used: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "success": self.success,
            "skipped_reason": self.skipped_reason,
            "mode": self.mode,
            "intent": self.intent,
            "primary_thesis": self.primary_thesis,
            "opposing_thesis": self.opposing_thesis,
            "debate_rounds": self.debate_rounds,
            "unresolved_conflicts": self.unresolved_conflicts,
            "judge_decision": self.judge_decision,
            "debug_outputs": self.debug_outputs,
            "total_tokens": self.total_tokens,
            "models_used": list(dict.fromkeys(self.models_used)),
            "error": self.error,
        }


def should_run_debate(
    *,
    agent_user_context: Optional[AgentUserContext],
    tool_calls: List[Dict[str, Any]],
) -> bool:
    """Return whether the debate stage has enough context to run."""
    if not agent_user_context:
        return False
    if agent_user_context.report.analysis_mode != "planning_execute":
        return False
    intent = agent_user_context.report.intent
    if intent == "auto":
        has_position = bool(
            agent_user_context.report.primary_symbol
            and agent_user_context.has_position_for(agent_user_context.report.primary_symbol)
        )
        intent = "position_review" if has_position else "entry_analysis"
    if intent not in DEBATE_INTENTS:
        return False
    return any(isinstance(call, dict) and call.get("success") is not False for call in tool_calls)


def run_adversarial_debate(
    *,
    task: str,
    primary_report: str,
    agent_user_context: AgentUserContext,
    planner: Optional[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    llm_adapter: LLMToolAdapter,
    timeout_seconds: Optional[float] = None,
    progress_callback: Optional[Any] = None,
) -> DebateResult:
    """Run primary, opposing, and judge roles over one shared evidence bundle."""
    if not should_run_debate(agent_user_context=agent_user_context, tool_calls=tool_calls):
        return DebateResult(enabled=False, skipped_reason="insufficient_context_or_evidence")

    intent = _resolve_intent(agent_user_context)
    bundle = build_shared_evidence_bundle(
        task=task,
        primary_report=primary_report,
        agent_user_context=agent_user_context,
        planner=planner,
        tool_calls=tool_calls,
        intent=intent,
    )
    result = DebateResult(enabled=True, intent=intent)
    result.debug_outputs = {
        "primary_report_raw": primary_report,
        "shared_evidence_bundle": bundle,
    }
    try:
        _emit_debate_event(
            progress_callback,
            "debate_start",
            intent=intent,
            message="开始对抗式 Debate：主观点、强制反方、Judge 裁决。",
        )
        primary_response = _call_role_json(
            llm_adapter,
            _build_primary_prompt(bundle),
            timeout_seconds=timeout_seconds,
        )
        _accumulate_usage(result, primary_response)
        result.debug_outputs["primary_thesis_raw"] = primary_response.content or ""
        result.primary_thesis = _normalize_thesis(
            _parse_or_fallback(primary_response.content, _fallback_primary(bundle)),
            role="primary",
        )
        _emit_debate_event(
            progress_callback,
            "debate_primary_done",
            intent=intent,
            primary_thesis=result.primary_thesis,
            raw_output=result.debug_outputs["primary_thesis_raw"],
        )

        opposing_response = _call_role_json(
            llm_adapter,
            _build_opposing_prompt(bundle, result.primary_thesis),
            timeout_seconds=timeout_seconds,
        )
        _accumulate_usage(result, opposing_response)
        result.debug_outputs["opposing_thesis_raw"] = opposing_response.content or ""
        result.opposing_thesis = _normalize_thesis(
            _parse_or_fallback(opposing_response.content, _fallback_opposing(bundle)),
            role="opposing",
        )
        _emit_debate_event(
            progress_callback,
            "debate_opposing_done",
            intent=intent,
            opposing_thesis=result.opposing_thesis,
            raw_output=result.debug_outputs["opposing_thesis_raw"],
        )

        judge_response = _call_role_json(
            llm_adapter,
            _build_judge_prompt(bundle, result.primary_thesis, result.opposing_thesis),
            timeout_seconds=timeout_seconds,
        )
        _accumulate_usage(result, judge_response)
        result.debug_outputs["judge_raw"] = judge_response.content or ""
        judge = _parse_or_fallback(
            judge_response.content,
            _fallback_judge(result.primary_thesis, result.opposing_thesis),
        )
        result.judge_decision = _normalize_judge(judge)
        result.debate_rounds = [
            {
                "round": 1,
                "primary_argument": result.primary_thesis.get("summary", ""),
                "opposing_argument": result.opposing_thesis.get("summary", ""),
                "judge_focus": result.judge_decision.get("reason", ""),
            }
        ]
        result.unresolved_conflicts = _listify(judge.get("unresolved_conflicts"))
        result.success = True
        _emit_debate_event(
            progress_callback,
            "debate_judge_done",
            intent=intent,
            judge_decision=result.judge_decision,
            raw_output=result.debug_outputs["judge_raw"],
        )
        return result
    except Exception as exc:
        result.success = False
        result.error = str(exc)
        _emit_debate_event(
            progress_callback,
            "debate_error",
            intent=intent,
            message=str(exc),
        )
        return result


def build_shared_evidence_bundle(
    *,
    task: str,
    primary_report: str,
    agent_user_context: AgentUserContext,
    planner: Optional[Dict[str, Any]],
    tool_calls: List[Dict[str, Any]],
    intent: str,
) -> Dict[str, Any]:
    """Build the evidence bundle shared by all debate roles."""
    return {
        "task": task,
        "intent": intent,
        "stance_contract": STANCE_BY_INTENT.get(intent, STANCE_BY_INTENT["entry_analysis"]),
        "primary_report_excerpt": _truncate(primary_report, 4000),
        "agent_user_context": agent_user_context.model_dump(mode="json"),
        "planner": planner or {},
        "evidence_ledger": _tool_calls_to_evidence(tool_calls),
        "rules": [
            "只能使用本 bundle 内的工具证据、用户上下文和原始问题。",
            "不得编造价格、新闻、财务数据、持仓成本或账户信息。",
            "双方必须给出失效条件。",
            "Judge 不能简单折中，必须按证据强弱、账户风险、数据可靠性和用户目标裁决。",
            "必须分别检查账户风险、技术面、资金面、消息面和数据可靠性；缺失的维度要明确写缺失，不能默认忽略。",
        ],
    }


def format_debate_appendix(debate: Optional[DebateResult | Dict[str, Any]]) -> str:
    """Return a Markdown appendix for the final report."""
    if debate is None:
        return ""
    payload = debate.to_dict() if isinstance(debate, DebateResult) else debate
    if not payload.get("enabled") or not payload.get("success"):
        return ""

    primary = payload.get("primary_thesis") or {}
    opposing = payload.get("opposing_thesis") or {}
    judge = payload.get("judge_decision") or {}
    summary = judge.get("decision_summary") or judge.get("reason") or "-"
    reason_points = _listify(judge.get("reason_points"))
    dimensions = _normalize_dimension_assessments(judge.get("dimension_assessments"))
    accepted = _listify(judge.get("accepted_arguments"))
    rejected = _listify(judge.get("rejected_arguments"))
    controls = _listify(judge.get("risk_controls"))

    lines = [
        "",
        "## 对抗式辩论裁决",
        "",
        "| 角色 | 立场 | 建议动作 | 核心理由 | 失效条件 |",
        "| --- | --- | --- | --- | --- |",
        (
            f"| 主观点 | {primary.get('direction') or '-'} | {primary.get('action') or '-'} | "
            f"{_markdown_cell(primary.get('summary') or '-')} | "
            f"{_markdown_cell('; '.join(_listify(primary.get('failure_conditions'))) or '-')} |"
        ),
        (
            f"| 反方 | {opposing.get('direction') or '-'} | {opposing.get('action') or '-'} | "
            f"{_markdown_cell(opposing.get('summary') or '-')} | "
            f"{_markdown_cell('; '.join(_listify(opposing.get('failure_conditions'))) or '-')} |"
        ),
        "",
        "### Judge 最终裁决",
        "",
        f"- 裁决结果：**{judge.get('winner') or '-'}**",
        f"- 最终动作：**{judge.get('final_action') or '-'}**",
        f"- 结论摘要：{summary}",
    ]
    if reason_points:
        lines.extend(["", "#### 裁决理由"])
        lines.extend([f"- {item}" for item in reason_points])
    elif judge.get("reason"):
        lines.extend(["", "#### 裁决理由", f"- {judge.get('reason')}"])
    if dimensions:
        lines.extend([
            "",
            "#### 分维度证据",
            "",
            "| 维度 | 结论 | 权重 | 证据摘要 | 缺口 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for item in dimensions:
            lines.append(
                "| {dimension} | {verdict} | {weight} | {summary} | {missing} |".format(
                    dimension=_markdown_cell(item.get("dimension") or "-"),
                    verdict=_markdown_cell(item.get("verdict") or "-"),
                    weight=_markdown_cell(item.get("weight") or "-"),
                    summary=_markdown_cell(item.get("summary") or "-"),
                    missing=_markdown_cell("; ".join(_listify(item.get("missing"))) or "-"),
                )
            )
    if accepted:
        lines.extend(["", "#### 采纳论点"])
        lines.extend([f"- {item}" for item in accepted])
    if rejected:
        lines.extend(["", "#### 驳回论点"])
        lines.extend([f"- {item}" for item in rejected])
    if controls:
        lines.extend(["", "#### 风控条件"])
        lines.extend([f"- {item}" for item in controls])
    return "\n".join(lines).strip() + "\n"


def _call_role_json(
    llm_adapter: LLMToolAdapter,
    prompt: str,
    *,
    timeout_seconds: Optional[float],
) -> LLMResponse:
    return llm_adapter.call_text(
        [
            {
                "role": "system",
                "content": "你是账户感知股票分析系统中的 Debate Agent。只输出 JSON，不输出 Markdown。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1800,
        timeout=timeout_seconds,
    )


def _build_primary_prompt(bundle: Dict[str, Any]) -> str:
    contract = bundle["stance_contract"]["primary"]
    return f"""基于同一份证据包生成主观点 Thesis。

立场要求：{contract}

必须输出 JSON：
{{
  "direction": "bullish|neutral_bullish",
  "action": "hold|add|open|monitor|wait",
  "summary": "一句话主观点",
  "evidence": ["只引用证据包中的证据"],
  "evidence_by_dimension": {{
    "account_risk": ["账户/持仓证据；没有则写数据缺失"],
    "technical": ["技术面证据；没有则写数据缺失"],
    "capital_flow": ["资金面证据；没有则写数据缺失"],
    "news_event": ["消息面证据；没有则写数据缺失"]
  }},
  "failure_conditions": ["哪些条件会推翻主观点"],
  "account_impact": "账户/持仓/仓位约束如何影响该观点"
}}

证据包：
{json.dumps(bundle, ensure_ascii=False, indent=2, default=str)}
"""


def _build_opposing_prompt(bundle: Dict[str, Any], primary: Dict[str, Any]) -> str:
    contract = bundle["stance_contract"]["opposing"]
    return f"""你是强制反方 Agent。必须站在主观点相反方向，构造最强反证。

反方立场要求：{contract}

限制：
- 不能编造任何新数据。
- 不能另行调用工具。
- 必须直接挑战主观点的证据质量、账户风险、时效性和失效条件。

必须输出 JSON：
{{
  "direction": "bearish|neutral_bearish",
  "action": "reduce|take_profit|stop_loss|wait|reject|monitor",
  "summary": "一句话反方观点",
  "evidence": ["只引用证据包中的反证"],
  "evidence_by_dimension": {{
    "account_risk": ["账户/持仓反证；没有则写数据缺失"],
    "technical": ["技术面反证；没有则写数据缺失"],
    "capital_flow": ["资金面反证；没有则写数据缺失"],
    "news_event": ["消息面反证；没有则写数据缺失"]
  }},
  "failure_conditions": ["哪些条件会推翻反方观点"],
  "primary_challenges": ["主观点哪里证据不足或风险被低估"],
  "account_impact": "账户/持仓/仓位约束如何放大风险"
}}

主观点：
{json.dumps(primary, ensure_ascii=False, indent=2, default=str)}

同一份证据包：
{json.dumps(bundle, ensure_ascii=False, indent=2, default=str)}
"""


def _build_judge_prompt(
    bundle: Dict[str, Any],
    primary: Dict[str, Any],
    opposing: Dict[str, Any],
) -> str:
    return f"""你是 Judge Agent。你不能简单折中，必须基于证据强弱、账户风险、数据可靠性和用户目标裁决。

裁决要求：
- 不要输出一大段流水账。必须把裁决拆成摘要、分维度证据、要点化理由、风控条件。
- 必须逐项审视 account_risk、technical、capital_flow、news_event、fundamental、data_quality。
- 资金面和消息面不能被技术面覆盖：有证据就引用证据；工具失败、结果不相关或未调用时，必须在 missing 里写清楚“资金面/消息面证据不足”，并降低裁决确定性。
- 如果资金面或消息面证据不足，不允许写“无实质利好/无重大利空”这种确定表述，只能写“现有证据未确认”。
- 每条 evidence 必须能在同一份证据包、主观点或反方中找到依据。

必须输出 JSON：
{{
  "winner": "primary|opposing|no_trade|insufficient_data",
  "final_action": "hold|add|reduce|take_profit|stop_loss|open|wait|reject|monitor|insufficient_data",
  "decision_summary": "一句话裁决，适合前端摘要展示",
  "reason": "兼容旧字段的简短裁决理由，最多120字",
  "reason_points": ["要点化裁决理由，每条不超过60字"],
  "dimension_assessments": [
    {{
      "dimension": "account_risk|technical|capital_flow|news_event|fundamental|data_quality",
      "verdict": "supports_primary|supports_opposing|mixed|insufficient_data",
      "weight": "high|medium|low",
      "summary": "该维度如何影响裁决，最多80字",
      "evidence": ["该维度引用的关键证据"],
      "missing": ["该维度缺失或不可靠的数据"]
    }}
  ],
  "accepted_arguments": ["采纳哪些论点"],
  "rejected_arguments": ["驳回哪些论点"],
  "risk_controls": ["最终动作必须满足的风控条件"],
  "unresolved_conflicts": ["仍无法裁决的数据冲突或缺失项"]
}}

主观点：
{json.dumps(primary, ensure_ascii=False, indent=2, default=str)}

反方：
{json.dumps(opposing, ensure_ascii=False, indent=2, default=str)}

同一份证据包：
{json.dumps(bundle, ensure_ascii=False, indent=2, default=str)}
"""


def _resolve_intent(context: AgentUserContext) -> str:
    intent = context.report.intent
    if intent != "auto":
        return intent
    primary_symbol = context.report.primary_symbol
    if primary_symbol and context.has_position_for(primary_symbol):
        return "position_review"
    return "entry_analysis"


def _tool_calls_to_evidence(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for index, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            continue
        status = "success" if call.get("success") is not False else "failed"
        tool = call.get("tool")
        entries.append({
            "index": index,
            "step": call.get("step"),
            "tool": tool,
            "dimension": _infer_evidence_dimension(str(tool or "")),
            "arguments": call.get("arguments") or {},
            "status": status,
            "duration": call.get("duration"),
            "evidence": call.get("result_preview") or "",
            "limitation": "工具失败，不能作为强证据。" if status != "success" else "需结合字段完整度和行情时效判断。",
        })
    return entries


def _parse_or_fallback(content: Optional[str], fallback: Dict[str, Any]) -> Dict[str, Any]:
    parsed = try_parse_json(content or "")
    return parsed if isinstance(parsed, dict) else fallback


def _normalize_thesis(value: Dict[str, Any], *, role: str) -> Dict[str, Any]:
    return {
        "role": role,
        "direction": str(value.get("direction") or "-"),
        "action": str(value.get("action") or "-"),
        "summary": str(value.get("summary") or value.get("reason") or "-"),
        "evidence": _listify(value.get("evidence")),
        "evidence_by_dimension": _normalize_evidence_by_dimension(value.get("evidence_by_dimension")),
        "failure_conditions": _listify(value.get("failure_conditions")),
        "primary_challenges": _listify(value.get("primary_challenges")),
        "account_impact": str(value.get("account_impact") or "-"),
    }


def _normalize_judge(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "winner": str(value.get("winner") or "insufficient_data"),
        "final_action": str(value.get("final_action") or "insufficient_data"),
        "decision_summary": str(value.get("decision_summary") or value.get("reason") or "-"),
        "reason": str(value.get("reason") or "-"),
        "reason_points": _listify(value.get("reason_points")),
        "dimension_assessments": _normalize_dimension_assessments(value.get("dimension_assessments")),
        "accepted_arguments": _listify(value.get("accepted_arguments")),
        "rejected_arguments": _listify(value.get("rejected_arguments")),
        "risk_controls": _listify(value.get("risk_controls")),
        "unresolved_conflicts": _listify(value.get("unresolved_conflicts")),
    }


def _fallback_primary(bundle: Dict[str, Any]) -> Dict[str, Any]:
    action = "hold" if bundle.get("intent") == "position_review" else "monitor"
    return {
        "direction": "neutral_bullish",
        "action": action,
        "summary": "主报告结论可作为初始观点，但需要反方和 Judge 复核。",
        "evidence": ["主报告与已执行工具证据"],
        "failure_conditions": ["关键工具证据缺失或出现强反证"],
        "account_impact": "按账户上下文和仓位约束保守执行。",
    }


def _fallback_opposing(bundle: Dict[str, Any]) -> Dict[str, Any]:
    action = "wait" if bundle.get("intent") in {"entry_analysis", "watchlist_scan"} else "monitor"
    return {
        "direction": "neutral_bearish",
        "action": action,
        "summary": "反方认为在证据未充分覆盖前应降低动作强度。",
        "evidence": ["工具证据可能存在时效或字段局限"],
        "failure_conditions": ["价格、趋势、事件和账户风险均确认支持主观点"],
        "primary_challenges": ["主观点需要证明风险收益比和账户约束均可接受"],
        "account_impact": "仓位、回撤和止损约束会放大错误动作的成本。",
    }


def _fallback_judge(primary: Dict[str, Any], opposing: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "winner": "insufficient_data",
        "final_action": "monitor",
        "decision_summary": "Debate JSON 解析失败，保守采用监控/等待。",
        "reason": "Debate JSON 解析失败，保守采用监控/等待。",
        "reason_points": ["Debate 输出不完整，无法形成高确定性裁决。"],
        "dimension_assessments": [
            {
                "dimension": "data_quality",
                "verdict": "insufficient_data",
                "weight": "high",
                "summary": "Debate 输出无法稳定解析，只能降级处理。",
                "evidence": [],
                "missing": ["结构化 Judge 输出"],
            }
        ],
        "accepted_arguments": [str(opposing.get("summary") or "反方风险提示")],
        "rejected_arguments": [],
        "risk_controls": _listify(primary.get("failure_conditions")) + _listify(opposing.get("failure_conditions")),
        "unresolved_conflicts": ["Debate 输出不完整"],
    }


def _accumulate_usage(result: DebateResult, response: LLMResponse) -> None:
    result.total_tokens += int((response.usage or {}).get("total_tokens", 0) or 0)
    model = response.model or response.provider
    if model and model != "error":
        result.models_used.append(model)


def _emit_debate_event(progress_callback: Optional[Any], event_type: str, **payload: Any) -> None:
    if not progress_callback:
        return
    try:
        progress_callback({"type": event_type, **payload})
    except Exception:
        return


def _listify(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [str(item) for item in value if item not in (None, "")]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


def _infer_evidence_dimension(tool_name: str) -> str:
    if tool_name in {"search_stock_news", "search_comprehensive_intel"}:
        return "news_event"
    if tool_name == "get_capital_flow":
        return "capital_flow"
    if tool_name in {"analyze_trend", "calculate_ma", "get_volume_analysis", "analyze_pattern", "get_daily_history"}:
        return "technical"
    if tool_name in {"get_chip_distribution"}:
        return "chip_distribution"
    if tool_name in {"get_stock_info"}:
        return "fundamental"
    if tool_name in {"get_realtime_quote", "get_market_indices", "get_sector_rankings"}:
        return "market_state"
    return "other"


def _normalize_evidence_by_dimension(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _listify(items) for key, items in value.items()}


def _normalize_dimension_assessments(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized.append({
            "dimension": str(item.get("dimension") or "-"),
            "verdict": str(item.get("verdict") or "-"),
            "weight": str(item.get("weight") or "-"),
            "summary": str(item.get("summary") or "-"),
            "evidence": _listify(item.get("evidence")),
            "missing": _listify(item.get("missing")),
        })
    return normalized


def _truncate(value: str, max_chars: int) -> str:
    text = value or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...[truncated {len(text) - max_chars} chars]"


def _markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


__all__ = [
    "DebateResult",
    "build_shared_evidence_bundle",
    "format_debate_appendix",
    "run_adversarial_debate",
    "should_run_debate",
]
