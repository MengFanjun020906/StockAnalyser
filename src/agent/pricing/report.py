from __future__ import annotations

import json
import re
from decimal import Decimal

from .schemas import (
    GenerationStatus,
    PriceRelation,
    ScenarioRole,
    TradePlanResult,
    ZoneScenario,
)


_BLOCK_START = "<!-- deterministic-price-plan:start -->"
_BLOCK_END = "<!-- deterministic-price-plan:end -->"
_BLOCK_PATTERN = re.compile(
    rf"\n?{re.escape(_BLOCK_START)}.*?{re.escape(_BLOCK_END)}\n?",
    flags=re.DOTALL,
)
_JSON_PRICE_FIELD_PATTERN = re.compile(
    r'("(?:ideal_buy|secondary_buy|entry_price|entry_zone|entry_zone_low|entry_zone_high|'
    r'stop_loss|stop_loss_price|take_profit|take_profit_price|target_price|target_1|target_2)"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)',
    flags=re.IGNORECASE,
)
_NARRATIVE_PRICE_LINE_PATTERN = re.compile(
    r"(?:理想入场|次优入场|入场(?:价|区间|点位)|买入(?:价|区间|点位)|止损(?:价|位)?|"
    r"(?:第一|第二)?目标(?:价|位)?|ideal\s+buy|secondary\s+buy|entry\s+(?:price|zone)|"
    r"stop[ _-]?loss|take[ _-]?profit|target[ _-]?[12]?)"
    r"[^\n\r]{0,48}?\d+(?:\.\d+)?(?![\d.]|\s*%)",
    flags=re.IGNORECASE,
)

_ROLE_LABELS = {
    ScenarioRole.PRIMARY: "主场景",
    ScenarioRole.SECONDARY: "次场景",
    ScenarioRole.OBSERVATION: "观察场景",
}
_RELATION_LABELS = {
    PriceRelation.ABOVE: "现价在区间上方，等待回落",
    PriceRelation.INSIDE: "现价已进入区间，等待确认条件",
    PriceRelation.BELOW: "现价在区间下方，等待重新评估",
    PriceRelation.INVALIDATED: "场景已失效",
}
_STATUS_LABELS = {
    GenerationStatus.GENERATED: "已生成",
    GenerationStatus.OBSERVATION: "仅观察",
    GenerationStatus.NO_PLAN: "暂无计划",
}
_REASON_LABELS = {
    "SYSTEM_DISABLED": "确定性价格计划功能已关闭",
    "SYSTEM_NOT_READY": "系统就绪检查未通过",
    "UNSUPPORTED_INSTRUMENT": "当前版本仅支持 A 股普通股票",
    "PRICE_SCALE_UNVERIFIED": "现金价格尺度无法验证",
    "INSUFFICIENT_DAILY_HISTORY": "日线历史不足",
    "INSUFFICIENT_WEEKLY_HISTORY": "周线历史不足",
    "NO_ELIGIBLE_ANCHORS": "没有满足条件的有效价格锚点",
    "NO_ELIGIBLE_CLUSTERS": "有效锚点未形成可用价格区间",
    "NO_FORMAL_SCENARIO": "候选区间未通过正式场景约束",
    "REVIEW_OBSERVE_ONLY": "模型证据仅允许观察",
    "REVIEW_HARD_VETO": "模型证据触发硬否决",
    "LLM_PRICE_AUTHORITY_VIOLATION": "模型越权输出价格字段，相关内容已丢弃",
    "TECHNICAL_CONFIRMATION_PENDING": "技术确认尚未完成",
    "EXTRA_CONFIRMATION_PENDING": "附加确认尚未完成",
    "POSITION_SIZING_UNAVAILABLE": "确定性仓位上限暂不可用",
    "ACCOUNT_CONTEXT_ABSENT_REFERENCE_COST": "未绑定账户，盈亏比采用明确标记的参考成本",
    "ACCOUNT_COST_POLICY_UNAVAILABLE": "账户费率或资金字段不完整",
    "ACCOUNT_ORDER_SIZE_UNAVAILABLE": "账户资金不足一手或无法计算委托数量",
    "ACCOUNT_NET_RR_UNAVAILABLE": "账户净盈亏比暂不可计算",
    "ACCOUNT_NET_RR_BELOW_MINIMUM": "账户实际成本下的净盈亏比未达门槛",
    "RISK_GATE_PENDING": "等待风险门检查",
    "RISK_GATE_BLOCKED": "风险门不允许执行",
    "NO_SELECTABLE_SCENARIO": "没有可选择的正式场景",
    "AS_OF_ROLLED_BACK": "行情提供方尚未返回目标日完整 K 线，已回退到实际最新交易日",
    "INSUFFICIENT_SOURCE_FAMILIES": "独立价格来源不足",
    "NO_VALID_STOP": "没有满足风险约束的有效止损",
    "TARGET_TOO_CLOSE": "目标空间过近",
    "WEEKLY_TREND_OPPOSING": "周线趋势与入场方向相反",
    "PLAN_VERDICT_UNAVAILABLE": "模型未给出该维度对价格计划的明确结论",
}
_EXECUTION_STATUS_LABELS = {
    "review_pending": "等待模型证据评审",
    "waiting_confirmation": "等待技术确认",
    "observe_only": "仅观察，不执行",
    "risk_blocked": "风险门阻断",
    "actionable": "条件满足，可按计划执行",
    "vetoed": "硬否决",
    "closed": "计划已结束",
    "no_plan": "无有效计划",
    "system_unready": "系统未就绪",
    "system_disabled": "系统已停用",
}
_REVIEW_LABELS = {
    "support": "支持",
    "mixed": "证据混合",
    "insufficient": "证据不足",
    "opposing": "反对",
    "hard_veto": "硬否决",
}
_REVIEW_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "评审失败",
    "pending": "等待评审",
    "not_run": "未执行",
}
_REVIEW_ERROR_LABELS = {
    "legacy_report_fallback_skipped_debate": "该历史 Trace 使用旧链路，主报告降级后跳过了 Debate；请重跑以生成四维评审。",
}
_RISK_PROFILE_LABELS = {
    "aggressive": "进取型",
    "balanced": "均衡型",
    "conservative": "稳健型",
}
_DIMENSION_LABELS = {
    "technical": "技术面",
    "capital": "资金面",
    "fundamental": "基本面",
    "intel": "消息面",
}
_QUALITY_LABELS = {
    "ok": "完整",
    "partial": "部分可用",
    "unavailable": "不可用",
}
_EFFECT_LABELS = {
    "keep": "保持数学计划",
    "reduce_position": "降低仓位并提高确认要求",
    "observe_only": "仅观察",
    "veto": "禁止执行",
}
_SOURCE_FAMILY_LABELS = {
    "trend": "趋势与均线",
    "cost_volume": "筹码与成交成本",
    "price_structure": "价格结构",
    "volatility_boundary": "波动边界",
}


def render_price_plan_markdown(result: TradePlanResult) -> str:
    """Render the authoritative deterministic price block for a report."""
    artifact = result.price_plan
    lines = [
        _BLOCK_START,
        "## 确定性价格计划",
        "",
        "> 以下价格由确定性数学模型生成，是本报告中入场、止损和目标价的唯一有效来源。模型文字意见只能降低执行强度，不能修改这些价格。",
        "",
        f"- 计划状态：{_STATUS_LABELS[artifact.generation_status]}",
        f"- 标的与基准日：`{artifact.symbol}` · `{artifact.actual_as_of_session.isoformat()}`",
        f"- 规则版本：`{artifact.algorithm_version}` / `{artifact.params_version}`",
        f"- 计划编号：`{artifact.plan_id}`（修订 {artifact.revision}）",
    ]
    lines.extend(_render_execution_decision(result))
    if artifact.generation_status is not GenerationStatus.NO_PLAN:
        lines.extend(_render_dimension_bridge(result))
    if artifact.generation_status is GenerationStatus.NO_PLAN:
        lines.extend(
            [
                "",
                "### 暂不可生成价格计划",
                "",
                *[
                    f"- {_REASON_LABELS.get(code, code)}（`{code}`）"
                    for code in artifact.reason_codes
                ],
                "",
                "当前不会回退到大模型猜测价格，也不会创建可执行买入信号。",
            ]
        )
    else:
        for scenario in sorted(artifact.scenarios, key=lambda item: item.base_rank):
            lines.extend(_render_scenario(scenario, result))
    lines.extend(["", _BLOCK_END])
    return "\n".join(lines)


def _render_execution_decision(result: TradePlanResult) -> list[str]:
    decision = result.account_decision
    review = result.review
    lines = [
        "",
        "### 执行判决",
        "",
        f"- 当前状态：{_EXECUTION_STATUS_LABELS.get(result.execution_status, result.execution_status)}",
    ]
    if review is not None:
        if review.review_status == "failed":
            lines.append("- 模型证据评审：评审失败")
            review_error = str(review.validated_output.get("review_error") or "").strip()
            if review_error:
                lines.append(
                    f"- 评审失败原因：{_REVIEW_ERROR_LABELS.get(review_error, review_error)}"
                )
        elif review.review_status == "pending":
            lines.append("- 模型证据评审：尚未执行")
        else:
            lines.append(
                f"- 模型证据评审：{_REVIEW_LABELS.get(review.conclusion.value, review.conclusion.value)}"
            )
        if review.validated_output.get("price_fields_present") is True:
            has_structured_dimensions = any(
                "MODEL_PLAN_VERDICT" in item.reason_codes
                for item in review.dimension_reviews
            )
            lines.append(
                "- 权限校验：模型越权价格已丢弃；合规的非价格维度意见继续按固定规则评审"
                if has_structured_dimensions
                else "- 权限校验：模型越权价格已丢弃；本轮未形成可分离的结构化维度评审"
            )
    if decision is not None:
        lines.extend(
            [
                f"- 风险偏好：{_RISK_PROFILE_LABELS.get(decision.risk_profile, decision.risk_profile)}",
                f"- 选中场景：`{decision.selected_scenario_id or '无'}`",
                f"- 仓位上限：{_position(decision.target_position_pct)}",
                f"- 计划数量：{decision.planned_quantity if decision.planned_quantity is not None else '未绑定账户'}",
                f"- 账户净盈亏比：{_number(decision.account_net_rr_1)}",
            ]
        )
    reasons = sorted(set(result.reason_codes))
    lines.append(
        "- 当前限制："
        + (
            "；".join(_REASON_LABELS.get(code, code) for code in reasons)
            if reasons
            else "无"
        )
    )
    return lines


def _render_dimension_bridge(result: TradePlanResult) -> list[str]:
    review = result.review
    lines = [
        "",
        "### 价格形成与四维评审",
        "",
        "> 数学模型先生成区间，四维评审只改变执行权限，不修改入场、止损或目标价格。技术面评审只检查是否允许执行，不会对已经进入数学模型的指标重复计票。",
        "",
    ]
    if review is None or not review.dimension_reviews:
        lines.append("- 四维评审尚未形成结构化结果，当前不能提高执行强度。")
        return lines
    lines.extend(
        [
            "| 维度 | 作用范围 | 证据状态 | 评审状态 | 对计划结论 | 对执行的影响 | 主要原因 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in review.dimension_reviews:
        lines.append(
            "| "
            + " | ".join(
                (
                    _DIMENSION_LABELS[item.dimension],
                    "全部场景" if item.scope == "stock" else "指定场景",
                    _QUALITY_LABELS[item.evidence_quality],
                    _REVIEW_STATUS_LABELS[item.review_status],
                    (
                        _REVIEW_LABELS[item.verdict.value]
                        if item.review_status == "completed"
                        else "未形成"
                    ),
                    (
                        _EFFECT_LABELS[item.execution_effect]
                        if item.review_status == "completed"
                        else "不提高执行强度"
                    ),
                    _markdown_cell(item.summary),
                )
            )
            + " |"
        )
    return lines


def merge_price_plan_into_report(content: str, result: TradePlanResult) -> str:
    """Replace the previous authoritative block and append the current one."""
    narrative = _BLOCK_PATTERN.sub("\n", content or "").strip()
    narrative = _strip_unauthorized_price_content(narrative)
    rendered = render_price_plan_markdown(result)
    return f"{narrative}\n\n{rendered}".strip() if narrative else rendered


def _strip_unauthorized_price_content(content: str) -> str:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, (dict, list)):
        from .decision import sanitize_llm_price_fields

        return json.dumps(
            sanitize_llm_price_fields(payload),
            ensure_ascii=False,
            indent=2,
        )
    replaced = _JSON_PRICE_FIELD_PATTERN.sub(
        lambda match: f'{match.group(1)}"N/A（由确定性价格引擎接管）"',
        content or "",
    )
    lines = []
    for line in replaced.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("{", "}")) or '"' in line:
            lines.append(line)
            continue
        if _NARRATIVE_PRICE_LINE_PATTERN.search(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _render_scenario(
    scenario: ZoneScenario,
    result: TradePlanResult,
) -> list[str]:
    label = _ROLE_LABELS[scenario.role]
    families = "、".join(
        _SOURCE_FAMILY_LABELS.get(item, item) for item in scenario.source_families
    ) or "无"
    math_reasons = "；".join(
        _REASON_LABELS.get(code, code) for code in scenario.scenario_reason_codes
    ) or "该区间已通过当前数学约束"
    lines = [
        "",
        f"### {label} · 排名 {scenario.base_rank}",
        "",
        f"- 入场区间：{_price(scenario.entry_low)} - {_price(scenario.entry_high)}",
        f"- 当前关系：{_RELATION_LABELS[scenario.price_relation]}",
        f"- 有效期：自 {scenario.valid_from.isoformat()} 起 {scenario.confirmation_valid_trading_days} 个有效交易日"
        f"（预计至 {scenario.confirmation_deadline.isoformat()}，停牌顺延）",
        f"- 价格依据：{families}",
        f"- 数学约束：{math_reasons}",
    ]
    impact = next(
        (
            item
            for item in (result.review.scenario_impacts if result.review else [])
            if item.scenario_id == scenario.scenario_id
        ),
        None,
    )
    if impact is not None:
        reviews = {}
        for item in (result.review.dimension_reviews if result.review else []):
            if scenario.scenario_id not in item.scenario_ids:
                continue
            current = reviews.get(item.dimension)
            if current is None or _effect_severity(item.execution_effect) > _effect_severity(
                current.execution_effect
            ):
                reviews[item.dimension] = item
        dimension_effects = "；".join(
            f"{_DIMENSION_LABELS[dimension]}"
            f"{_REVIEW_LABELS[reviews[dimension].verdict.value] if reviews[dimension].review_status == 'completed' else _REVIEW_STATUS_LABELS[reviews[dimension].review_status]}"
            for dimension in impact.applicable_dimensions
            if dimension in reviews
        ) or "无可用维度评审"
        lines.extend(
            [
                f"- 四维影响：{dimension_effects}",
                f"- 权限流转：数学层{_EFFECT_LABELS[impact.math_effect]} → 四维合并后{_EFFECT_LABELS[impact.final_effect]}",
            ]
        )
    if scenario.role is ScenarioRole.OBSERVATION:
        lines.append("- 执行属性：仅供观察，不创建买入信号")
        return lines
    lines.extend(
        [
            f"- 止损价：{_price(scenario.stop_loss)}",
            f"- 第一目标：{_price(scenario.target_1)}（计划减仓 {_percent(scenario.target_1_exit_ratio)}）",
            f"- 第二目标：{_price(scenario.target_2)}",
            f"- 参考净盈亏比：{_number(scenario.reference_net_rr_1)}",
            f"- 触达距离：{scenario.reachability_band.value}",
            f"- 确认规则：{_rule_names(scenario.confirmation_rules)}",
            f"- 失效规则：{_rule_names(scenario.invalidation_rules)}",
        ]
    )
    return lines


def _effect_severity(value: str) -> int:
    return {
        "keep": 0,
        "reduce_position": 1,
        "observe_only": 2,
        "veto": 3,
    }.get(value, 3)


def _markdown_cell(value: str) -> str:
    return str(value or "-").replace("|", "\\|").replace("\n", " ")


def _price(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value.quantize(Decimal('0.01'))} 元"


def _number(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value.quantize(Decimal('0.01'))}:1"


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('1'))}%"


def _position(value: Decimal | None) -> str:
    return "暂不可用" if value is None else f"{value.quantize(Decimal('0.01'))}%"


def _rule_names(rules: list[object]) -> str:
    names = [str(getattr(item, "rule_type", "")).strip() for item in rules]
    return "、".join(item for item in names if item) or "无"


__all__ = ["merge_price_plan_into_report", "render_price_plan_markdown"]
