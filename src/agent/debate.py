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
from src.agent.llm_telemetry import llm_telemetry_scope
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
    deterministic_price_plan: Optional[Dict[str, Any]] = None,
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
        deterministic_price_plan=deterministic_price_plan,
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
            stage="debate_primary",
        )
        _accumulate_usage(result, primary_response)
        result.debug_outputs["primary_thesis_raw"] = primary_response.content or ""
        primary_parse = _parse_or_fallback(
            primary_response.content,
            _fallback_primary(bundle),
            stage="primary_thesis",
        )
        if primary_parse.parse_error:
            result.debug_outputs["primary_thesis_parse_error"] = primary_parse.parse_error
        result.primary_thesis = _normalize_thesis(primary_parse.payload, role="primary")
        if primary_parse.parse_error:
            return _mark_debate_parse_failure(
                result,
                primary_parse.parse_error,
                progress_callback=progress_callback,
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
            stage="debate_opposing",
        )
        _accumulate_usage(result, opposing_response)
        result.debug_outputs["opposing_thesis_raw"] = opposing_response.content or ""
        opposing_parse = _parse_or_fallback(
            opposing_response.content,
            _fallback_opposing(bundle),
            stage="opposing_thesis",
        )
        if opposing_parse.parse_error:
            result.debug_outputs["opposing_thesis_parse_error"] = opposing_parse.parse_error
        result.opposing_thesis = _normalize_thesis(opposing_parse.payload, role="opposing")
        if opposing_parse.parse_error:
            return _mark_debate_parse_failure(
                result,
                opposing_parse.parse_error,
                progress_callback=progress_callback,
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
            stage="debate_judge",
            max_tokens=1800,
            disable_thinking=True,
        )
        _accumulate_usage(result, judge_response)
        result.debug_outputs["judge_raw"] = judge_response.content or ""
        result.debug_outputs["judge_response_diagnostics"] = _response_diagnostics(
            judge_response
        )
        require_plan_dimensions = bool(
            (bundle.get("deterministic_price_plan") or {}).get("scenarios")
        )
        judge_parse = _parse_or_fallback(
            judge_response.content,
            _fallback_judge(result.primary_thesis, result.opposing_thesis),
            stage="judge_decision",
            require_plan_dimensions=require_plan_dimensions,
        )
        if judge_parse.parse_error and (
            _looks_truncated(judge_response) or require_plan_dimensions
        ):
            result.debug_outputs["judge_initial_parse_error"] = judge_parse.parse_error
            retry_response = _call_role_json(
                llm_adapter,
                _build_judge_retry_prompt(
                    bundle,
                    result.primary_thesis,
                    result.opposing_thesis,
                    judge_response.content or "",
                ),
                timeout_seconds=timeout_seconds,
                stage="debate_judge_retry",
                max_tokens=1400,
                disable_thinking=True,
            )
            _accumulate_usage(result, retry_response)
            result.debug_outputs["judge_retry_raw"] = retry_response.content or ""
            result.debug_outputs["judge_retry_response_diagnostics"] = (
                _response_diagnostics(retry_response)
            )
            judge_parse = _parse_or_fallback(
                retry_response.content,
                _fallback_judge(result.primary_thesis, result.opposing_thesis),
                stage="judge_decision",
                require_plan_dimensions=require_plan_dimensions,
            )
            if not judge_parse.parse_error:
                result.debug_outputs["judge_recovered_after_retry"] = True
                if _looks_truncated(judge_response):
                    result.debug_outputs["judge_recovered_from_truncation"] = True
        if judge_parse.parse_error:
            result.debug_outputs["judge_parse_error"] = judge_parse.parse_error
        judge = judge_parse.payload
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
        if judge_parse.parse_error:
            return _mark_debate_parse_failure(
                result,
                judge_parse.parse_error,
                progress_callback=progress_callback,
            )
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
    deterministic_price_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the evidence bundle shared by all debate roles."""
    from src.agent.dimension_evidence import (
        build_dimension_evidence_summary,
        compact_dimension_evidence_summary,
        select_balanced_tool_calls,
    )

    balanced_tool_calls = select_balanced_tool_calls(tool_calls, limit=20)
    return {
        "task": task,
        "intent": intent,
        "stance_contract": STANCE_BY_INTENT.get(intent, STANCE_BY_INTENT["entry_analysis"]),
        "primary_report_excerpt": _truncate(primary_report, 4000),
        "agent_user_context": agent_user_context.model_dump(mode="json"),
        "planner": planner or {},
        "evidence_ledger": _tool_calls_to_evidence(
            balanced_tool_calls,
            original_tool_calls=tool_calls,
        ),
        "dimension_evidence": compact_dimension_evidence_summary(
            build_dimension_evidence_summary(tool_calls)
        ),
        "deterministic_price_plan": _compact_price_plan_for_review(
            deterministic_price_plan
        ),
        "rules": [
            "只能使用本 bundle 内的工具证据、用户上下文和原始问题。",
            "不得编造价格、新闻、财务数据、持仓成本或账户信息。",
            "双方必须给出失效条件。",
            "Judge 不能简单折中，必须按证据强弱、账户风险、数据可靠性和用户目标裁决。",
            "必须分别检查账户风险、技术面、资金面、消息面和数据可靠性；缺失的维度要明确写缺失，不能默认忽略。",
        ],
    }


def _compact_price_plan_for_review(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "status": "unavailable",
            "rule": "价格计划不可用时，Judge 不得自行生成价格或场景。",
            "scenarios": [],
        }
    scenarios = []
    for item in value.get("scenarios") or []:
        if not isinstance(item, dict):
            continue
        scenarios.append({
            "scenario_id": item.get("scenario_id"),
            "role": item.get("role"),
            "base_rank": item.get("base_rank"),
            "entry_low": item.get("entry_low"),
            "entry_high": item.get("entry_high"),
            "source_families": item.get("source_families") or [],
            "price_relation": item.get("price_relation"),
            "scenario_reason_codes": item.get("scenario_reason_codes") or [],
        })
    return {
        "status": value.get("generation_status") or "unavailable",
        "plan_id": value.get("plan_id"),
        "actual_as_of_session": value.get("actual_as_of_session"),
        "reason_codes": value.get("reason_codes") or [],
        "scenarios": scenarios,
        "authority_rule": "数学层是唯一价格真源；Judge 只能引用 scenario_id 并降低或保持执行权限。",
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
    winner_labels = {
        "primary": "主观点",
        "opposing": "反方",
        "no_trade": "暂不交易",
        "insufficient_data": "证据不足",
    }
    action_labels = {
        "hold": "持有",
        "add": "加仓",
        "reduce": "减仓",
        "take_profit": "止盈",
        "stop_loss": "止损",
        "open": "开仓",
        "wait": "等待",
        "reject": "拒绝入场",
        "monitor": "观察",
        "insufficient_data": "证据不足",
    }
    dimension_labels = {
        "account_risk": "账户风险",
        "technical": "技术面",
        "capital_flow": "资金面",
        "news_event": "消息面",
        "fundamental": "基本面",
        "data_quality": "数据质量",
    }
    verdict_labels = {
        "supports_primary": "支持主观点",
        "supports_opposing": "支持反方",
        "mixed": "证据混合",
        "insufficient_data": "证据不足",
    }
    plan_verdict_labels = {
        "support": "支持执行",
        "mixed": "证据混合",
        "insufficient": "证据不足",
        "opposing": "反对执行",
        "hard_veto": "硬否决",
    }

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
        f"- 裁决结果：**{winner_labels.get(str(judge.get('winner') or ''), judge.get('winner') or '-')}**",
        f"- 最终动作：**{action_labels.get(str(judge.get('final_action') or ''), judge.get('final_action') or '-')}**",
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
            "| 维度 | 对价格计划结论 | 证据摘要 | 缺口 |",
            "| --- | --- | --- | --- |",
        ])
        for item in dimensions:
            plan_verdict = str(item.get("plan_verdict") or "")
            verdict = plan_verdict_labels.get(plan_verdict)
            if not verdict:
                verdict = verdict_labels.get(
                    str(item.get("verdict") or ""),
                    item.get("verdict") or "-",
                )
            lines.append(
                "| {dimension} | {verdict} | {summary} | {missing} |".format(
                    dimension=_markdown_cell(dimension_labels.get(str(item.get("dimension") or ""), item.get("dimension") or "-")),
                    verdict=_markdown_cell(verdict),
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
    stage: str,
    max_tokens: int = 6000,
    disable_thinking: bool = False,
) -> LLMResponse:
    with llm_telemetry_scope(stage=stage, agent_role=stage):
        return llm_adapter.call_text(
            [
                {
                    "role": "system",
                    "content": "你是账户感知股票分析系统中的 Debate Agent。只输出 JSON，不输出 Markdown。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
            response_format={"type": "json_object"},
            disable_thinking=disable_thinking,
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
    context = _compact_judge_context(bundle, primary, opposing)
    return f"""你是 Judge Agent。基于压缩证据审查数学价格计划的执行权限。只输出一个完整 JSON object，不得输出 Markdown 或额外字段。

裁决要求：
- 必须且只能输出 technical、capital_flow、fundamental、news_event 四项评审，顺序固定。
- plan_verdict 只能是 support / mixed / insufficient / opposing / hard_veto，表示该维度对数学计划执行权限的绝对结论。
- 数学场景本身已给出可执行性和原因码；四维评审只允许保持或降低权限，不修改任何价格。
- 股票级结论 scenario_ids 留空；只有维度对某个区间有独立证据时才填写真实 scenario_id。
- 技术面只评审数学计划是否允许执行，不得重新生成价格或把已经参与数学建模的指标重复计票；资金面、基本面和消息面只能保持、降级或否决执行，不能修改价格。
- `hard_veto` 只能引用 Evidence Ledger 中可核验的公司级硬证据；证据不足时必须用 `insufficient`，不得猜测。
- 每个非 insufficient 结论至少引用一个本维度真实 ref；insufficient 必须填写 missing。
- 资金面和消息面不能被技术面覆盖，四项必须分别形成结论。
- 如果资金面或消息面证据不足，不允许写“无实质利好/无重大利空”这种确定表述，只能写“现有证据未确认”。
- 每个字符串不超过 60 个汉字，整份 JSON 控制在 1600 字以内。

必须输出 JSON：
{{
  "winner": "primary|opposing|no_trade|insufficient_data",
  "final_action": "hold|add|reduce|take_profit|stop_loss|open|wait|reject|monitor|insufficient_data",
  "decision_summary": "一句话裁决，适合前端摘要展示",
  "reason": "简短裁决理由",
  "dimension_assessments": [
    {{
      "dimension": "technical|capital_flow|fundamental|news_event",
      "plan_verdict": "support|mixed|insufficient|opposing|hard_veto",
      "summary": "为什么保持或降低执行权限",
      "evidence_refs": ["真实 ref"],
      "scenario_ids": [],
      "missing": [],
      "reason_codes": ["稳定英文原因码"]
    }}
  ],
  "risk_controls": ["最终动作必须满足的风控条件"],
  "unresolved_conflicts": ["仍无法裁决的数据冲突或缺失项"]
}}

压缩证据：
{json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)}
"""


def _compact_judge_context(
    bundle: Dict[str, Any],
    primary: Dict[str, Any],
    opposing: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep only evidence needed to bind four-dimensional plan verdicts."""
    ledger = []
    for item in bundle.get("evidence_ledger") or []:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension") or "")
        if dimension not in {"technical", "capital", "fundamental", "intel"}:
            continue
        ledger.append({
            "ref": item.get("ref"),
            "dimension": dimension,
            "status": item.get("status"),
            "evidence": _truncate(str(item.get("evidence") or ""), 220),
        })

    def compact_thesis(value: Dict[str, Any]) -> Dict[str, Any]:
        by_dimension = value.get("evidence_by_dimension") or {}
        return {
            "action": value.get("action"),
            "summary": _truncate(str(value.get("summary") or ""), 180),
            "dimensions": {
                key: [_truncate(str(text), 140) for text in _listify(items)[:2]]
                for key, items in by_dimension.items()
                if key in {"technical", "capital_flow", "fundamental", "news_event"}
            },
        }

    return {
        "intent": bundle.get("intent"),
        "primary": compact_thesis(primary),
        "opposing": compact_thesis(opposing),
        "dimension_quality": bundle.get("dimension_evidence") or {},
        "price_plan": bundle.get("deterministic_price_plan") or {},
        "evidence_ledger": ledger,
    }


def _build_judge_retry_prompt(
    bundle: Dict[str, Any],
    primary: Dict[str, Any],
    opposing: Dict[str, Any],
    truncated_output: str,
) -> str:
    """Build one bounded retry with only refs, quality and plan state."""
    compact_ledger = [
        {
            "ref": item.get("ref"),
            "dimension": item.get("dimension"),
            "status": item.get("status"),
            "evidence": _truncate(str(item.get("evidence") or ""), 120),
        }
        for item in bundle.get("evidence_ledger") or []
        if isinstance(item, dict)
    ]
    retry_bundle = {
        "intent": bundle.get("intent"),
        "dimension_evidence": bundle.get("dimension_evidence") or {},
        "deterministic_price_plan": bundle.get("deterministic_price_plan") or {},
        "evidence_ledger": compact_ledger,
    }
    return f"""上一次 Judge 未返回完整 JSON。重新生成，不要续写。只输出一个 1200 字以内的 JSON object，不要解释。

只输出下面 schema，不要添加字段；每个字符串最多 40 字：
{{
  "winner": "primary|opposing|no_trade|insufficient_data",
  "final_action": "hold|add|reduce|take_profit|stop_loss|open|wait|reject|monitor|insufficient_data",
  "decision_summary": "一句话",
  "reason": "一句话",
  "dimension_assessments": [
    {{"dimension":"technical","plan_verdict":"support|mixed|insufficient|opposing|hard_veto","summary":"一句话","evidence_refs":["真实 ref"],"scenario_ids":[],"missing":[]}},
    {{"dimension":"capital_flow","plan_verdict":"support|mixed|insufficient|opposing|hard_veto","summary":"一句话","evidence_refs":["真实 ref"],"scenario_ids":[],"missing":[]}},
    {{"dimension":"fundamental","plan_verdict":"support|mixed|insufficient|opposing|hard_veto","summary":"一句话","evidence_refs":["真实 ref"],"scenario_ids":[],"missing":[]}},
    {{"dimension":"news_event","plan_verdict":"support|mixed|insufficient|opposing|hard_veto","summary":"一句话","evidence_refs":["真实 ref"],"scenario_ids":[],"missing":[]}}
  ],
  "risk_controls": [],
  "unresolved_conflicts": []
}}
当 plan_verdict=insufficient 时 evidence_refs 可以为空，但 missing 必须写明缺口；其它 verdict 必须至少引用一个真实 ref。只能引用下方真实 ref 和 scenario_id，不得生成价格。

精简证据包：
{json.dumps(retry_bundle, ensure_ascii=False, separators=(",", ":"), default=str)}
"""


def _resolve_intent(context: AgentUserContext) -> str:
    intent = context.report.intent
    if intent != "auto":
        return intent
    primary_symbol = context.report.primary_symbol
    if primary_symbol and context.has_position_for(primary_symbol):
        return "position_review"
    return "entry_analysis"


def _tool_calls_to_evidence(
    tool_calls: List[Dict[str, Any]],
    *,
    original_tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    from src.agent.dimension_evidence import infer_tool_dimension

    source_calls = original_tool_calls if original_tool_calls is not None else tool_calls
    source_positions = {
        id(call): index
        for index, call in enumerate(source_calls, start=1)
        if isinstance(call, dict)
    }
    for index, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            continue
        status = "success" if call.get("success") is not False else "failed"
        tool = call.get("tool")
        original_index = source_positions.get(id(call), index)
        entries.append({
            "index": original_index,
            "ref": f"tool:{original_index}:{tool or 'unknown'}",
            "step": call.get("step"),
            "tool": tool,
            "dimension": infer_tool_dimension(tool),
            "arguments": call.get("arguments") or {},
            "status": status,
            "duration": call.get("duration"),
            "evidence": _truncate(str(call.get("result_preview") or ""), 1000),
            "limitation": "工具失败，不能作为强证据。" if status != "success" else "需结合字段完整度和行情时效判断。",
        })
    return entries


@dataclass(frozen=True)
class _ParseResult:
    payload: Dict[str, Any]
    parse_error: Optional[str] = None


def _parse_or_fallback(
    content: Optional[str],
    fallback: Dict[str, Any],
    *,
    stage: str,
    require_plan_dimensions: bool = False,
) -> _ParseResult:
    parsed = _try_parse_strict_json(content or "")
    if isinstance(parsed, dict) and _has_required_debate_fields(
        parsed,
        stage=stage,
        require_plan_dimensions=require_plan_dimensions,
    ):
        return _ParseResult(payload=parsed)
    fallback_payload = dict(fallback)
    fallback_payload.setdefault("_parse_error", {
        "stage": stage,
        "error": "llm_json_parse_failed",
        "raw_excerpt": _truncate(content or "", 1000),
    })
    return _ParseResult(
        payload=fallback_payload,
        parse_error=f"llm_json_parse_failed:{stage}",
    )


def _try_parse_strict_json(content: str) -> Optional[Dict[str, Any]]:
    """Parse a complete JSON object without repairing truncated model output."""
    cleaned = content.strip()
    if not cleaned:
        return None
    candidates = [cleaned]
    if cleaned.startswith("```") and cleaned.endswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            candidates.append(cleaned[first_newline + 1 : -3].strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in dict.fromkeys(candidates):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _looks_truncated(response: LLMResponse) -> bool:
    """Identify a provider length stop or an object cut before its closing brace."""
    if str(response.finish_reason or "").lower() in {"length", "max_tokens"}:
        return True
    content = str(response.content or "").strip()
    return content.startswith("{") and not content.endswith("}")


def _response_diagnostics(response: LLMResponse) -> Dict[str, Any]:
    """Persist enough metadata to distinguish empty output from bad JSON."""
    return {
        "finish_reason": response.finish_reason,
        "content_chars": len(response.content or ""),
        "reasoning_chars": len(response.reasoning_content or ""),
        "completion_tokens": int((response.usage or {}).get("completion_tokens", 0) or 0),
        "provider": response.provider,
        "model": response.model,
    }


def _has_required_debate_fields(
    payload: Dict[str, Any],
    *,
    stage: str,
    require_plan_dimensions: bool = False,
) -> bool:
    required = {
        "primary_thesis": ("direction", "action", "summary"),
        "opposing_thesis": ("direction", "action", "summary"),
        "judge_decision": ("winner", "final_action"),
    }.get(stage, ())
    if any(not str(payload.get(field) or "").strip() for field in required):
        return False
    if stage == "judge_decision":
        if not str(payload.get("decision_summary") or payload.get("reason") or "").strip():
            return False
        if require_plan_dimensions:
            assessments = payload.get("dimension_assessments")
            if not isinstance(assessments, list):
                return False
            by_dimension = {
                str(item.get("dimension") or ""): item
                for item in assessments
                if isinstance(item, dict)
            }
            allowed_verdicts = {"support", "mixed", "insufficient", "opposing", "hard_veto"}
            for dimension in ("technical", "capital_flow", "fundamental", "news_event"):
                assessment = by_dimension.get(dimension)
                if not assessment or assessment.get("plan_verdict") not in allowed_verdicts:
                    return False
                refs = assessment.get("evidence_refs")
                missing = assessment.get("missing")
                if assessment.get("plan_verdict") == "insufficient":
                    if not isinstance(missing, list) or not missing:
                        return False
                elif not isinstance(refs, list) or not refs:
                    return False
        return True
    return True


def _mark_debate_parse_failure(
    result: DebateResult,
    error: str,
    *,
    progress_callback: Optional[Any],
) -> DebateResult:
    result.success = False
    result.error = error
    _emit_debate_event(
        progress_callback,
        "debate_error",
        intent=result.intent,
        message=error,
    )
    return result


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
    if tool_name in {
        "get_stock_business_context",
        "get_tushare_daily_basic",
        "get_tushare_financial_indicators",
        "get_tushare_financial_statements",
        "get_tushare_forecast",
        "get_tushare_express",
        "search_stock_fundamentals",
    }:
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
            "plan_verdict": str(item.get("plan_verdict") or ""),
            "weight": str(item.get("weight") or "-"),
            "summary": str(item.get("summary") or "-"),
            "evidence": _listify(item.get("evidence")),
            "evidence_refs": _listify(item.get("evidence_refs")),
            "missing": _listify(item.get("missing")),
            "reason_codes": _listify(item.get("reason_codes")),
            "scenario_ids": _listify(item.get("scenario_ids")),
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
