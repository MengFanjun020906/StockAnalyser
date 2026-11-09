# -*- coding: utf-8 -*-
"""Selection and confirmation agents for P3 historical research."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.backtest.schemas import ExperimentConfig

from .llm import ResearchBudgetExceeded, ResearchLLM, ResearchLLMError
from .market import ResearchPlanDraft, TechnicalSnapshot

logger = logging.getLogger(__name__)


SELECTION_SYSTEM_PROMPT = """你是历史时点的 A 股选股研究 Agent（prompt_version=research-prompt.v1）。
你只能使用用户消息中提供的 as-of 技术快照、组合快照和风险档位，不得使用任何未来信息、当前实时行情、外部搜索或你记忆中的后续事件。
你的任务是从给定 universe 中选出最多 N 个候选，按 rank 排序。你只能输出严格 JSON，不要输出 Markdown。
JSON schema:
{"selected":[{"symbol":"6位A股代码","rank":1,"confidence":0.0,"thesis":"不超过120字的 as-of 理由"}],"no_trade_reason":null}
规则：
- symbol 必须来自 universe，不能编造。
- 最多 N 个；没有合适标的时 selected=[] 并给出 no_trade_reason。
- 不要输出入场价、止损价或目标价；价格由确定性研究计划生成。
- 资金有限，应优先选择流动性和风险收益更匹配的候选。"""

CONFIRMATION_SYSTEM_PROMPT = """你是历史时点的 A 股单股确认 Agent（prompt_version=research-prompt.v1）。
你只能使用用户消息中提供的 as-of 技术快照、确定性研究计划和组合快照；不得修改价格，不得使用未来信息或外部记忆。
你的任务是确认该计划是否值得在给定资金/风险约束下开仓。只能输出严格 JSON：
{"decision":"accept"|"reject","confidence":0.0,"reason":"不超过120字"}
规则：
- 如果入场区、止损、目标与 as-of 波动/趋势明显不匹配，reject。
- 如果现金或总仓位不足以容纳最小一手，reject。
- 不要输出新的价格；价格由确定性代码生成。
- 只接受你能用给定 as-of 证据解释的计划。"""


@dataclass
class SelectionResult:
    selected: list[dict[str, Any]] = field(default_factory=list)
    no_trade_reason: str | None = None
    fallback: bool = False
    gap: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "no_trade_reason": self.no_trade_reason,
            "fallback": self.fallback,
            "gap": self.gap,
        }


@dataclass
class ConfirmationResult:
    accepted: bool
    confidence: float = 0.0
    reason: str = ""
    gap: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "confidence": self.confidence,
            "reason": self.reason,
            "gap": self.gap,
        }


def run_selection(
    *,
    llm: ResearchLLM,
    snapshots: dict[str, TechnicalSnapshot],
    as_of: date,
    portfolio: dict[str, Any],
    config: ExperimentConfig,
    tool_gaps: dict[str, str],
) -> SelectionResult:
    research = config.research
    if research is None:
        raise ValueError("research config is required")
    max_select = min(research.selection_top_n, research.max_confirmations_per_decision)
    rows = [snapshot.to_prompt_row() for snapshot in sorted(snapshots.values(), key=lambda item: item.score, reverse=True)]
    payload = {
        "as_of": as_of.isoformat(),
        "max_select": max_select,
        "universe": rows,
        "portfolio": portfolio,
        "risk_policy": config.risk_policy.model_dump(mode="json") if config.risk_policy else {},
        "tool_gaps": tool_gaps,
        "notes": "No future data. Prices are generated deterministically after your selection.",
    }
    cache_key = f"selection:{research.prompt_version}:{as_of.isoformat()}:{llm.model_ref}:{max_select}"
    try:
        response, _record = llm.call_json(
            stage="selection",
            system_prompt=SELECTION_SYSTEM_PROMPT,
            payload=payload,
            cache_key=cache_key,
        )
    except ResearchBudgetExceeded:
        raise
    except ResearchLLMError as exc:
        fallback = _fallback_selection(snapshots, max_select)
        return SelectionResult(
            selected=fallback,
            fallback=True,
            gap=f"selection_llm_failure:{exc}",
            raw={"error": str(exc)},
        )
    selected, invalid = _normalize_selected(response.get("selected"), snapshots, max_select)
    no_trade_reason = response.get("no_trade_reason")
    if selected:
        return SelectionResult(
            selected=selected,
            no_trade_reason=str(no_trade_reason) if no_trade_reason else None,
            gap="selection_invalid_symbols" if invalid else None,
            raw=response,
        )
    if no_trade_reason:
        return SelectionResult(no_trade_reason=str(no_trade_reason), raw=response)
    fallback = _fallback_selection(snapshots, max_select)
    return SelectionResult(
        selected=fallback,
        fallback=True,
        gap="selection_empty_fallback",
        raw=response,
    )


def run_confirmation(
    *,
    llm: ResearchLLM,
    draft: ResearchPlanDraft,
    portfolio: dict[str, Any],
    config: ExperimentConfig,
) -> ConfirmationResult:
    research = config.research
    if research is None:
        raise ValueError("research config is required")
    plan = draft.plan
    payload = {
        "as_of": draft.snapshot.as_of.isoformat(),
        "symbol": plan.symbol,
        "technical": draft.snapshot.to_prompt_row(),
        "plan": {
            "entry_low": float(plan.entry_low),
            "entry_high": float(plan.entry_high),
            "stop_loss": float(plan.stop_loss) if plan.stop_loss is not None else None,
            "target_1": float(plan.target_1) if plan.target_1 is not None else None,
            "target_2": float(plan.target_2) if plan.target_2 is not None else None,
            "order_valid_trading_days": plan.order_valid_trading_days,
            "max_hold_trading_days": plan.max_hold_trading_days,
            "target_position_pct": float(plan.target_position_pct),
        },
        "portfolio": portfolio,
        "risk_policy": config.risk_policy.model_dump(mode="json") if config.risk_policy else {},
        "tool_gaps": {"fundamental": "gap", "news": "gap"},
    }
    cache_key = (
        f"confirmation:{research.prompt_version}:{draft.snapshot.as_of.isoformat()}:"
        f"{plan.symbol}:{plan.source_hash}:{llm.model_ref}"
    )
    try:
        response, _record = llm.call_json(
            stage=f"confirmation:{plan.symbol}",
            system_prompt=CONFIRMATION_SYSTEM_PROMPT,
            payload=payload,
            cache_key=cache_key,
        )
    except ResearchBudgetExceeded:
        raise
    except ResearchLLMError as exc:
        return ConfirmationResult(
            accepted=False,
            reason="confirmation_llm_failure",
            gap=f"confirmation_llm_failure:{exc}",
            raw={"error": str(exc)},
        )
    decision = str(response.get("decision") or "").strip().lower()
    try:
        confidence = float(response.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(response.get("reason") or "")
    if decision == "accept" and confidence >= 0.5:
        return ConfirmationResult(accepted=True, confidence=confidence, reason=reason, raw=response)
    return ConfirmationResult(
        accepted=False,
        confidence=confidence,
        reason=reason or "rejected_by_agent",
        raw=response,
    )


def _normalize_selected(
    value: Any,
    snapshots: dict[str, TechnicalSnapshot],
    max_select: int,
) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], True
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    invalid = False
    for item in value:
        if not isinstance(item, dict):
            invalid = True
            continue
        symbol = str(item.get("symbol") or "").strip()
        symbol = "".join(ch for ch in symbol if ch.isdigit())[-6:]
        if symbol not in snapshots or symbol in seen:
            invalid = True
            continue
        try:
            rank = int(item.get("rank") or len(selected) + 1)
        except (TypeError, ValueError):
            rank = len(selected) + 1
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        selected.append(
            {
                "symbol": symbol,
                "rank": max(1, rank),
                "confidence": max(0.0, min(1.0, confidence)),
                "thesis": str(item.get("thesis") or "")[:240],
            }
        )
        seen.add(symbol)
        if len(selected) >= max_select:
            break
    selected.sort(key=lambda item: (item["rank"], item["symbol"]))
    return selected, invalid


def _fallback_selection(snapshots: dict[str, TechnicalSnapshot], max_select: int) -> list[dict[str, Any]]:
    ordered = sorted(snapshots.values(), key=lambda item: item.score, reverse=True)[:max_select]
    return [
        {
            "symbol": snapshot.symbol,
            "rank": index + 1,
            "confidence": 0.0,
            "thesis": "deterministic technical score fallback",
        }
        for index, snapshot in enumerate(ordered)
    ]


__all__ = [
    "CONFIRMATION_SYSTEM_PROMPT",
    "SELECTION_SYSTEM_PROMPT",
    "ConfirmationResult",
    "SelectionResult",
    "run_confirmation",
    "run_selection",
]
