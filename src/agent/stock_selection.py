# -*- coding: utf-8 -*-
"""Staged stock-selection pipeline for planning_execute watchlist scans."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.agent.evidence import build_evidence_cards_for_stock
from src.agent.evidence.adapter import cards_to_json
from src.agent.llm_adapter import LLMResponse, LLMToolAdapter
from src.agent.multi_expert import AgentState, ExpertOrchestrator
from src.agent.runner import try_parse_json
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name
from src.agent.stock_selection_prompts import (
    STRATEGY_THRESHOLDS,
    build_adversarial_review_prompt,
    build_candidate_discovery_prompt,
    build_candidate_screening_prompt,
    build_deep_dive_prompt,
    build_judge_decision_prompt,
    build_portfolio_allocation_prompt,
)
from src.agent.tools.registry import ToolRegistry
from src.schemas.agent_context import AgentUserContext

logger = logging.getLogger(__name__)


SELECTION_INTENT = "watchlist_scan"
DEFAULT_CANDIDATE_LIMIT = 8
DEFAULT_DEEP_DIVE_LIMIT = 4
FAILED_TOOL_STATUSES = {"failed", "error", "tool_failed", "timeout"}


@dataclass
class SelectionStage:
    """One stage result stored in ``SelectionRunContext``."""

    status: str = "pending"
    summary: Dict[str, Any] = field(default_factory=dict)
    full_ref: Optional[str] = None
    full: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_full: bool = True) -> Dict[str, Any]:
        payload = {
            "status": self.status,
            "summary": self.summary,
            "full_ref": self.full_ref,
        }
        if include_full:
            payload["full"] = self.full
        return payload


@dataclass
class SelectionRunContext:
    """Runtime context for one staged stock-selection run."""

    run_id: str
    user_message: str
    market: str = "cn"
    account_summary: Dict[str, Any] = field(default_factory=dict)
    investor_profile: Dict[str, Any] = field(default_factory=dict)
    candidate_strategy: str = "hot_sector"
    strategy_thresholds: Dict[str, Any] = field(default_factory=lambda: dict(STRATEGY_THRESHOLDS))
    stages: Dict[str, SelectionStage] = field(default_factory=dict)
    evidence_ledger: Dict[str, Any] = field(default_factory=lambda: {"summary": {}, "full_ref": "selection_evidence_ledger.json"})
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    market_regime: Dict[str, Any] = field(default_factory=dict)
    orchestration_mode: str = "legacy"
    expert_state: Optional[AgentState] = None
    stock_identity_violations: List[Dict[str, Any]] = field(default_factory=list)
    next_step: str = "candidate_discovery"
    total_tokens: int = 0
    models_used: List[str] = field(default_factory=list)

    def set_stage(self, stage_name: str, payload: Dict[str, Any]) -> None:
        full_ref = payload.get("full_ref") or f"{stage_name}.json"
        self.stages[stage_name] = SelectionStage(
            status=str(payload.get("status") or "partial"),
            summary=payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
            full_ref=full_ref,
            full=payload.get("full") if isinstance(payload.get("full"), dict) else payload,
        )

    def stage_summary(self, stage_name: str) -> Dict[str, Any]:
        stage = self.stages.get(stage_name)
        return stage.summary if stage else {}

    def stage_full(self, stage_name: str) -> Dict[str, Any]:
        stage = self.stages.get(stage_name)
        return stage.full if stage else {}

    def to_dict(self, *, include_full: bool = True) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "user_message": self.user_message,
            "market": self.market,
            "account_summary": self.account_summary,
            "investor_profile": self.investor_profile,
            "candidate_strategy": self.candidate_strategy,
            "strategy_thresholds": self.strategy_thresholds,
            "stages": {
                name: stage.to_dict(include_full=include_full)
                for name, stage in self.stages.items()
            },
            "evidence_ledger": self.evidence_ledger,
            "market_regime": self.market_regime,
            "orchestration_mode": self.orchestration_mode,
            "expert_state": self.expert_state.to_trace_dict() if self.expert_state else None,
            "stock_identity_audit": _stock_identity_audit(self),
            "next_step": self.next_step,
            "total_tokens": self.total_tokens,
            "models_used": _unique_text_items(self.models_used),
        }


@dataclass
class StockSelectionResult:
    """Result from the staged stock-selection pipeline."""

    enabled: bool = False
    success: bool = False
    skipped_reason: Optional[str] = None
    context: Optional[SelectionRunContext] = None
    final_report_json: Dict[str, Any] = field(default_factory=dict)
    final_markdown: str = ""
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0
    models_used: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "success": self.success,
            "skipped_reason": self.skipped_reason,
            "selection_context": self.context.to_dict(include_full=True) if self.context else None,
            "final_report_json": self.final_report_json,
            "final_markdown": self.final_markdown,
            "tool_calls_log": self.tool_calls_log,
            "total_tokens": self.total_tokens,
            "models_used": _unique_text_items(self.models_used),
            "error": self.error,
        }


def should_run_stock_selection(agent_user_context: Optional[AgentUserContext]) -> bool:
    """Return whether the staged selection pipeline should run."""
    if not agent_user_context:
        return False
    report = agent_user_context.report
    return report.analysis_mode == "planning_execute" and report.intent == SELECTION_INTENT


def run_stock_selection_pipeline(
    *,
    task: str,
    agent_user_context: AgentUserContext,
    tool_registry: ToolRegistry,
    llm_adapter: LLMToolAdapter,
    timeout_seconds: Optional[float] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    run_id: str = "selection-run",
    orchestration_mode: Optional[str] = None,
) -> StockSelectionResult:
    """Run the staged stock-selection pipeline.

    The pipeline uses deterministic tools for evidence collection and LLM JSON
    prompts for each analytical stage. It is intentionally scoped to
    ``watchlist_scan`` so existing single-stock and position-review flows remain
    unchanged.
    """
    if not should_run_stock_selection(agent_user_context):
        return StockSelectionResult(enabled=False, skipped_reason="not_watchlist_scan")

    ctx = SelectionRunContext(
        run_id=run_id,
        user_message=task,
        market=_resolve_market(agent_user_context),
        account_summary=_account_summary(agent_user_context),
        investor_profile=_investor_profile(agent_user_context),
        candidate_strategy="hot_sector",
        orchestration_mode=_resolve_orchestration_mode(orchestration_mode),
    )
    result = StockSelectionResult(enabled=True, context=ctx)
    base_evidence: Dict[str, Any] = {}
    try:
        _emit(progress_callback, "selection_start", message="开始五阶段选股流水线。")

        discovery_seed = _run_candidate_discovery_tool(
            ctx=ctx,
            tool_registry=tool_registry,
            target_symbols=list(agent_user_context.report.target_symbols or []),
        )
        discovery_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="candidate_discovery",
            prompt=build_candidate_discovery_prompt({
                "user_message": task,
                "market": ctx.market,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "target_symbols": list(agent_user_context.report.target_symbols or []),
                "candidate_strategy": ctx.candidate_strategy,
                "strategy_thresholds": ctx.strategy_thresholds,
                "tool_seed_result": _summarize_payload(discovery_seed),
                "available_tools": tool_registry.list_names(),
            }),
            fallback=_fallback_candidate_discovery(ctx, discovery_seed),
            timeout_seconds=timeout_seconds,
        )
        _merge_discovery_candidates(discovery_payload, discovery_seed)
        _enforce_stage_stock_identity(ctx, discovery_payload, "candidate_discovery")
        ctx.set_stage("candidate_discovery", discovery_payload)
        _emit(progress_callback, "selection_candidate_discovery_done", payload=ctx.stages["candidate_discovery"].to_dict(include_full=False))

        ctx.market_regime = _run_market_regime_tool(ctx=ctx, tool_registry=tool_registry)
        _emit(progress_callback, "selection_market_regime_done", payload=_summarize_market_regime(ctx.market_regime))

        candidates = _candidate_codes(ctx.stage_full("candidate_discovery"), limit=DEFAULT_CANDIDATE_LIMIT)
        if not candidates:
            ctx.next_step = "stop_no_trade"
            _maybe_run_expert_graph(
                ctx=ctx,
                task=task,
                base_evidence={},
                progress_callback=progress_callback,
            )
            result.success = True
            result.final_report_json = _build_final_report_json(ctx)
            _enforce_report_stock_identity(ctx, result.final_report_json)
            result.final_markdown = render_stock_selection_markdown(result.final_report_json)
            _finalize_result(result)
            return result

        base_evidence = _collect_base_evidence(ctx, tool_registry, candidates)
        screening_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="candidate_screening",
            prompt=build_candidate_screening_prompt({
                "user_message": task,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "candidate_pool_summary": ctx.stage_summary("candidate_discovery"),
                "candidate_pool_full_ref": ctx.stages["candidate_discovery"].full_ref,
                "market_regime": _summarize_market_regime(ctx.market_regime),
                "evidence_ledger_summary": ctx.evidence_ledger.get("summary"),
                "base_evidence": _summarize_payload(base_evidence),
            }),
            fallback=_fallback_candidate_screening(ctx, base_evidence),
            timeout_seconds=timeout_seconds,
        )
        _enforce_stage_stock_identity(ctx, screening_payload, "candidate_screening")
        ctx.set_stage("candidate_screening", screening_payload)
        _emit(progress_callback, "selection_candidate_screening_done", payload=ctx.stages["candidate_screening"].to_dict(include_full=False))

        deep_targets = _deep_dive_targets(ctx.stage_summary("candidate_screening"), ctx.stage_full("candidate_screening"))
        if not deep_targets:
            deep_targets = candidates[: min(2, len(candidates))]
        deep_targets = deep_targets[:DEFAULT_DEEP_DIVE_LIMIT]
        deep_dive_outputs: List[Dict[str, Any]] = []
        for code in deep_targets:
            detailed_evidence = _collect_deep_dive_evidence(ctx, tool_registry, code)
            stock_name = _stock_name_from_evidence(code, detailed_evidence)
            evidence_cards = build_evidence_cards_for_stock(
                run_id=ctx.run_id,
                stock_code=code,
                stock_name=stock_name,
                market=ctx.market,
                evidence=detailed_evidence,
            )
            deep_payload = _call_stage_json(
                ctx=ctx,
                llm_adapter=llm_adapter,
                stage_name=f"single_stock_deep_dive:{code}",
                prompt=build_deep_dive_prompt({
                    "user_message": task,
                    "stock_code": code,
                    "stock_name": stock_name,
                    "account_summary": ctx.account_summary,
                    "investor_profile": ctx.investor_profile,
                    "screening_summary": ctx.stage_summary("candidate_screening"),
                    "market_regime": _summarize_market_regime(ctx.market_regime),
                    "evidence_ledger_summary": ctx.evidence_ledger.get("summary"),
                    "stock_evidence": _summarize_payload(detailed_evidence, max_chars=5000),
                }),
                fallback=_fallback_deep_dive(code, stock_name, detailed_evidence),
                timeout_seconds=timeout_seconds,
            )
            _attach_evidence_cards(deep_payload, evidence_cards)
            _enforce_stage_stock_identity(ctx, deep_payload, f"single_stock_deep_dive:{code}")
            deep_dive_outputs.append(deep_payload)

        deep_dive_stage = _combine_deep_dive_outputs(deep_dive_outputs)
        _enforce_stage_stock_identity(ctx, deep_dive_stage, "single_stock_deep_dive")
        ctx.set_stage("single_stock_deep_dive", deep_dive_stage)
        _emit(progress_callback, "selection_deep_dive_done", payload=ctx.stages["single_stock_deep_dive"].to_dict(include_full=False))

        allocation_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="portfolio_allocation",
            prompt=build_portfolio_allocation_prompt({
                "user_message": task,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "deep_dive_results_summary": ctx.stage_summary("single_stock_deep_dive"),
                "market_regime": _summarize_market_regime(ctx.market_regime),
                "positions": _positions_summary(agent_user_context),
                "available_cash": ctx.account_summary.get("available_cash"),
            }),
            fallback=_fallback_portfolio_allocation(ctx),
            timeout_seconds=timeout_seconds,
        )
        allocation_payload = _apply_market_regime_constraints(ctx, allocation_payload)
        _enforce_stage_stock_identity(ctx, allocation_payload, "portfolio_allocation")
        ctx.set_stage("portfolio_allocation", allocation_payload)
        _emit(progress_callback, "selection_allocation_done", payload=ctx.stages["portfolio_allocation"].to_dict(include_full=False))

        adversarial_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="adversarial_review",
            prompt=build_adversarial_review_prompt({
                "user_message": task,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "candidate_discovery_summary": ctx.stage_summary("candidate_discovery"),
                "screening_summary": ctx.stage_summary("candidate_screening"),
                "deep_dive_results_summary": ctx.stage_summary("single_stock_deep_dive"),
                "allocation_plan_summary": ctx.stage_summary("portfolio_allocation"),
                "market_regime": _summarize_market_regime(ctx.market_regime),
                "evidence_ledger_summary": ctx.evidence_ledger.get("summary"),
            }),
            fallback=_fallback_adversarial_review(ctx),
            timeout_seconds=timeout_seconds,
        )
        _enforce_stage_stock_identity(ctx, adversarial_payload, "adversarial_review")
        ctx.set_stage("adversarial_review", adversarial_payload)
        _emit(progress_callback, "selection_adversarial_done", payload=ctx.stages["adversarial_review"].to_dict(include_full=False))

        judge_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="judge_decision",
            prompt=build_judge_decision_prompt({
                "user_message": task,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "allocation_plan_summary": ctx.stage_summary("portfolio_allocation"),
                "opposing_review_summary": ctx.stage_summary("adversarial_review"),
                "market_regime": _summarize_market_regime(ctx.market_regime),
                "evidence_ledger_summary": ctx.evidence_ledger.get("summary"),
            }),
            fallback=_fallback_judge_decision(ctx),
            timeout_seconds=timeout_seconds,
        )
        _enforce_stage_stock_identity(ctx, judge_payload, "judge_decision")
        ctx.set_stage("judge_decision", judge_payload)
        ctx.next_step = (judge_payload.get("summary") or {}).get("next_step") or "render_final_report"
        _emit(progress_callback, "selection_judge_done", payload=ctx.stages["judge_decision"].to_dict(include_full=False))
        _maybe_run_expert_graph(
            ctx=ctx,
            task=task,
            base_evidence=base_evidence,
            progress_callback=progress_callback,
        )

        result.success = True
        result.final_report_json = _build_final_report_json(ctx)
        _enforce_report_stock_identity(ctx, result.final_report_json)
        result.final_markdown = render_stock_selection_markdown(result.final_report_json)
        _finalize_result(result)
        _emit(progress_callback, "selection_done", final_action=(ctx.stage_summary("judge_decision") or {}).get("final_action"))
        return result
    except Exception as exc:
        logger.exception("Staged stock-selection pipeline failed: %s", exc)
        _complete_failed_selection_result(
            ctx=ctx,
            result=result,
            error=str(exc),
            task=task,
            base_evidence=base_evidence,
            progress_callback=progress_callback,
        )
        _finalize_result(result)
        _emit(progress_callback, "selection_error", message=str(exc), payload=result.final_report_json)
        return result


def render_stock_selection_markdown(report: Dict[str, Any]) -> str:
    """Render final stock-selection JSON into a compact Markdown report."""
    judge = report.get("judge_decision", {}).get("summary", {})
    allocation = report.get("portfolio_allocation", {}).get("summary", {})
    positions = report.get("portfolio_allocation", {}).get("full", {}).get("positions_plan", [])
    adversarial = report.get("adversarial_review", {}).get("summary", {})

    lines = [
        "# 选股与持仓配置报告",
        "",
        "## 一、最终结论",
        "",
        "| 项目 | 结论 |",
        "| --- | --- |",
        f"| 最终动作 | {judge.get('final_action') or allocation.get('portfolio_action') or '-'} |",
        f"| 裁决 | {judge.get('primary_plan_verdict') or '-'} |",
        f"| 核心原因 | {_markdown_cell(judge.get('decision_summary') or allocation.get('core_reason') or '-')} |",
        f"| 最大约束 | {_markdown_cell(allocation.get('main_constraint') or '-')} |",
        "",
        "## 二、组合配置表",
        "",
        "| 排名 | 股票 | 动作 | 首仓比例 | 入场条件 | 止损条件 | 复查触发 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if positions:
        for item in positions:
            lines.append(
                "| {rank} | {stock} | {action} | {pct} | {entry} | {stop} | {review} |".format(
                    rank=item.get("rank") or "-",
                    stock=_markdown_cell(f"{item.get('code') or '-'} {item.get('name') or ''}".strip()),
                    action=_markdown_cell(item.get("action") or "-"),
                    pct=_markdown_cell(_format_pct(item.get("initial_position_pct"))),
                    entry=_markdown_cell(item.get("entry_condition") or "-"),
                    stop=_markdown_cell(item.get("stop_loss_condition") or "-"),
                    review=_markdown_cell(item.get("review_trigger") or "-"),
                )
            )
    else:
        lines.append("| - | - | wait | - | 候选不足或证据不足 | - | 补充候选或等待数据 |")

    lines.extend([
        "",
        "## 三、反方审查与 Judge 裁决",
        "",
        f"- 反方观点：{adversarial.get('opposing_summary') or '-'}",
        f"- Judge 结论：{judge.get('decision_summary') or '-'}",
    ])

    risk_controls = report.get("judge_decision", {}).get("full", {}).get("risk_controls", [])
    if risk_controls:
        lines.extend(["", "## 四、风控条件"])
        lines.extend([f"- {item}" for item in risk_controls])
    return "\n".join(lines).strip()


def _resolve_orchestration_mode(value: Optional[str]) -> str:
    raw = (value or os.getenv("AGENT_ORCHESTRATION_MODE") or "legacy").strip().lower()
    return raw if raw in {"legacy", "expert_graph"} else "legacy"


def _maybe_run_expert_graph(
    *,
    ctx: SelectionRunContext,
    task: str,
    base_evidence: Dict[str, Any],
    progress_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> None:
    orchestrator = ExpertOrchestrator(ctx.orchestration_mode)
    if not orchestrator.enabled:
        return
    candidate_pool = ctx.stage_full("candidate_discovery").get("candidates") or []
    state = orchestrator.run_watchlist_scan(
        task=task,
        run_id=ctx.run_id,
        market=ctx.market,
        account_summary=ctx.account_summary,
        investor_profile=ctx.investor_profile,
        market_regime=ctx.market_regime,
        candidate_pool=candidate_pool,
        base_evidence=base_evidence,
        deep_dive_stage=ctx.stages.get("single_stock_deep_dive", SelectionStage()).to_dict(include_full=True),
        allocation_stage=ctx.stages.get("portfolio_allocation", SelectionStage()).to_dict(include_full=True),
        adversarial_stage=ctx.stages.get("adversarial_review", SelectionStage()).to_dict(include_full=True),
        judge_stage=ctx.stages.get("judge_decision", SelectionStage()).to_dict(include_full=True),
        evidence_ledger=ctx.evidence_ledger,
    )
    ctx.expert_state = state
    _emit(
        progress_callback,
        "selection_expert_graph_done",
        payload={
            "orchestration_mode": ctx.orchestration_mode,
            "expert_count": len(state.expert_opinions),
            "experts": list(state.expert_opinions.keys()),
            "expert_state": state.to_trace_dict(),
        },
    )


def _complete_failed_selection_result(
    *,
    ctx: SelectionRunContext,
    result: StockSelectionResult,
    error: str,
    task: str,
    base_evidence: Dict[str, Any],
    progress_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> None:
    """Finalize a partial selection report instead of falling back to legacy ReAct."""
    ctx.next_step = "stop_partial_report"
    _ensure_failure_judge_stage(ctx, error)
    try:
        _maybe_run_expert_graph(
            ctx=ctx,
            task=task,
            base_evidence=base_evidence,
            progress_callback=progress_callback,
        )
    except Exception as expert_exc:
        logger.exception("Expert graph failed during partial stock-selection finalization: %s", expert_exc)
    result.success = True
    result.error = error
    result.final_report_json = _build_final_report_json(ctx)
    result.final_report_json["partial_failure"] = {
        "status": "failed_stage_degraded",
        "error": error,
        "next_step": ctx.next_step,
    }
    _enforce_report_stock_identity(ctx, result.final_report_json)
    result.final_markdown = render_stock_selection_markdown(result.final_report_json)


def _ensure_failure_judge_stage(ctx: SelectionRunContext, error: str) -> None:
    if "judge_decision" not in ctx.stages:
        ctx.set_stage("judge_decision", {
            "stage": "judge_decision",
            "status": "partial_failure",
            "summary": {
                "primary_plan_verdict": "reject",
                "final_action": "wait",
                "decision_summary": f"选股流水线中途失败，已保留现有候选和证据；本轮禁止直接开仓。错误：{_truncate(error, 180)}",
                "next_step": "stop_partial_report",
            },
            "full": {
                "winner": "risk_control",
                "accepted_arguments": ["已有候选和工具证据可作为观察池"],
                "rejected_arguments": ["在流水线失败时直接给买入结论"],
                "required_plan_changes": ["等待缺失阶段恢复后重新运行"],
                "risk_controls": ["本轮仅观察，不开新仓"],
                "failure": {"error": error},
            },
            "full_ref": "judge_decision.json",
        })
    if "portfolio_allocation" not in ctx.stages:
        ctx.set_stage("portfolio_allocation", _fallback_portfolio_allocation(ctx))
    if "adversarial_review" not in ctx.stages:
        ctx.set_stage("adversarial_review", _fallback_adversarial_review(ctx))


def _call_stage_json(
    *,
    ctx: SelectionRunContext,
    llm_adapter: LLMToolAdapter,
    stage_name: str,
    prompt: str,
    fallback: Dict[str, Any],
    timeout_seconds: Optional[float],
) -> Dict[str, Any]:
    response = llm_adapter.call_text(
        [
            {
                "role": "system",
                "content": "你是账户感知股票选股流水线中的阶段 Agent。只输出 JSON，不输出 Markdown。",
            },
            {"role": "user", "content": prompt},
        ],
        timeout=timeout_seconds,
    )
    _accumulate_usage(ctx, response)
    parsed = try_parse_json(response.content or "")
    if not parsed:
        parsed = dict(fallback)
        parsed.setdefault("full", {})
        parsed["full"].setdefault("tool_failures", [])
        parsed["full"]["tool_failures"].append({
            "stage": stage_name,
            "error": "llm_json_parse_failed",
            "raw": _truncate(response.content or "", 1000),
        })
    return _normalize_stage_payload(parsed, fallback=fallback)


def _normalize_stage_payload(payload: Dict[str, Any], *, fallback: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = dict(fallback)
    normalized.update(payload)
    if not isinstance(normalized.get("summary"), dict):
        normalized["summary"] = fallback.get("summary", {})
    if not isinstance(normalized.get("full"), dict):
        normalized["full"] = fallback.get("full", {})
    normalized.setdefault("status", fallback.get("status", "partial"))
    normalized.setdefault("stage", fallback.get("stage"))
    normalized.setdefault("full_ref", fallback.get("full_ref"))
    return normalized


def _normalize_stock_identity_code(raw_code: Any) -> str:
    text = str(raw_code or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        text = text.split(".", 1)[0]
    text = re.sub(r"^(SH|SZ|BJ)", "", text)
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def _canonical_stock_name(code: Any, fallback_name: Any = None) -> str:
    code_text = _normalize_stock_identity_code(code)
    fallback_text = str(fallback_name or "").strip()
    for name in (STOCK_NAME_MAP.get(code_text), get_index_stock_name(code_text)):
        if is_meaningful_stock_name(name, code_text):
            return str(name).strip()
    if is_meaningful_stock_name(fallback_text, code_text):
        return fallback_text
    return code_text


def _record_stock_identity_violation(
    ctx: SelectionRunContext,
    *,
    stage_name: str,
    path: str,
    code: str,
    provided_name: str,
    canonical_name: str,
) -> None:
    if not provided_name or provided_name == canonical_name:
        return
    record = {
        "stage": stage_name,
        "path": path,
        "code": code,
        "provided_name": provided_name,
        "canonical_name": canonical_name,
        "resolution": "name_overwritten_by_code",
    }
    if record not in ctx.stock_identity_violations:
        ctx.stock_identity_violations.append(record)


def _enforce_stage_stock_identity(ctx: SelectionRunContext, payload: Dict[str, Any], stage_name: str) -> None:
    _enforce_stock_identity_node(ctx, payload, stage_name=stage_name, path=stage_name)


def _enforce_report_stock_identity(ctx: SelectionRunContext, report: Dict[str, Any]) -> None:
    _enforce_stock_identity_node(ctx, report, stage_name="final_report", path="final_report")
    report["stock_identity_audit"] = _stock_identity_audit(ctx)
    selection_context = report.get("selection_context")
    if isinstance(selection_context, dict):
        selection_context["stock_identity_audit"] = report["stock_identity_audit"]


def _enforce_stock_identity_node(
    ctx: SelectionRunContext,
    node: Any,
    *,
    stage_name: str,
    path: str,
) -> None:
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _enforce_stock_identity_node(ctx, item, stage_name=stage_name, path=f"{path}[{idx}]")
        return
    if not isinstance(node, dict):
        return

    code_key = "code" if "code" in node else ("stock_code" if "stock_code" in node else None)
    name_key = "name" if "name" in node else ("stock_name" if "stock_name" in node else None)
    if code_key:
        code = _normalize_stock_identity_code(node.get(code_key))
        if code:
            canonical_name = _canonical_stock_name(code, node.get(name_key) if name_key else None)
            provided_name = str(node.get(name_key) or "").strip() if name_key else ""
            node[code_key] = code
            if name_key:
                if provided_name != canonical_name:
                    _record_stock_identity_violation(
                        ctx,
                        stage_name=stage_name,
                        path=path,
                        code=code,
                        provided_name=provided_name,
                        canonical_name=canonical_name,
                    )
                node[name_key] = canonical_name
            elif canonical_name and canonical_name != code:
                node["name"] = canonical_name

    for key, value in list(node.items()):
        _enforce_stock_identity_node(ctx, value, stage_name=stage_name, path=f"{path}.{key}")


def _stock_identity_audit(ctx: SelectionRunContext) -> Dict[str, Any]:
    return {
        "status": "corrected" if ctx.stock_identity_violations else "passed",
        "violation_count": len(ctx.stock_identity_violations),
        "violations": list(ctx.stock_identity_violations),
    }


def _run_candidate_discovery_tool(
    *,
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    target_symbols: List[str],
) -> Dict[str, Any]:
    args = {
        "market": ctx.market,
        "seed_symbols": target_symbols,
        "limit": DEFAULT_CANDIDATE_LIMIT,
    }
    return _execute_tool(ctx, tool_registry, "discover_watchlist_candidates", args)


def _run_market_regime_tool(
    *,
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
) -> Dict[str, Any]:
    if tool_registry.get("detect_market_regime") is None:
        return {
            "status": "missing",
            "tool": "detect_market_regime",
            "data_quality": "insufficient",
            "regime": "unknown",
            "risk_level": "unknown",
            "strategy_hints": ["detect_market_regime 未注册，选股按保守市场环境处理。"],
        }
    result = _execute_tool(
        ctx,
        tool_registry,
        "detect_market_regime",
        {"market": ctx.market, "persist": True},
    )
    return result if isinstance(result, dict) else {"status": "unknown", "raw": result}


def _collect_base_evidence(
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    candidates: List[str],
) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {"detect_market_regime": ctx.market_regime}
    for name, args in (
        ("get_market_indices", {"region": ctx.market}),
        ("get_sector_rankings", {"top_n": 10}),
    ):
        evidence[name] = _execute_tool(ctx, tool_registry, name, args)
    evidence["quotes"] = {
        code: _execute_tool(ctx, tool_registry, "get_realtime_quote", {"stock_code": code})
        for code in candidates[:DEFAULT_CANDIDATE_LIMIT]
    }
    return evidence


def _collect_deep_dive_evidence(
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    code: str,
) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {}
    base_tools = (
        ("get_realtime_quote", {"stock_code": code}),
        ("analyze_trend", {"stock_code": code}),
        ("analyze_price_structure", {"stock_code": code}),
        ("get_capital_flow", {"stock_code": code}),
        ("get_stock_info", {"stock_code": code}),
        ("get_chip_distribution", {"stock_code": code}),
    )
    for tool_name, args in base_tools:
        if tool_registry.get(tool_name):
            evidence[tool_name] = _execute_tool(ctx, tool_registry, tool_name, args)
    if tool_registry.get("search_comprehensive_intel"):
        stock_name = _stock_name_from_evidence(code, evidence)
        evidence["search_comprehensive_intel"] = _execute_tool(
            ctx,
            tool_registry,
            "search_comprehensive_intel",
            {"stock_code": code, "stock_name": stock_name},
        )
    return evidence


def _execute_tool(
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Any:
    call: Dict[str, Any] = {
        "tool": tool_name,
        "arguments": arguments,
        "success": False,
        "result_preview": "",
        "selection_stage": True,
    }
    try:
        if tool_registry.get(tool_name) is None:
            raise KeyError(f"Tool '{tool_name}' not registered")
        result = tool_registry.execute(tool_name, **arguments)
        call["success"] = not _is_failed_tool_result(result)
        call["result_preview"] = _truncate(json.dumps(result, ensure_ascii=False, default=str), 1200)
        return result
    except Exception as exc:
        error_payload = {"status": "tool_failed", "tool": tool_name, "error": str(exc)}
        call["success"] = False
        call["result_preview"] = json.dumps(error_payload, ensure_ascii=False)
        return error_payload
    finally:
        ctx.tool_calls.append(call)
        _refresh_evidence_summary(ctx)


def _is_failed_tool_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in FAILED_TOOL_STATUSES:
        return True
    if status != "not_supported" and _result_has_errors(result):
        return True
    if result.get("timeout") is True:
        return True
    if result.get("success") is False:
        return True
    return bool(result.get("error")) and status != "not_supported"


def _result_has_errors(result: Dict[str, Any]) -> bool:
    if result.get("error"):
        return True
    errors = result.get("errors")
    if isinstance(errors, list):
        return any(str(item).strip() for item in errors)
    return bool(errors)


def _has_effective_tool_data(result: Dict[str, Any]) -> bool:
    ignored_keys = {"status", "errors", "error", "note", "message", "stock_code", "code"}
    for key, value in result.items():
        if key in ignored_keys:
            continue
        if _contains_effective_value(value):
            return True
    return False


def _contains_effective_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    if isinstance(value, dict):
        return any(_contains_effective_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_effective_value(item) for item in value)
    return True


def _refresh_evidence_summary(ctx: SelectionRunContext) -> None:
    entries = []
    for call in ctx.tool_calls:
        entries.append({
            "tool": call.get("tool"),
            "arguments": call.get("arguments"),
            "status": "success" if call.get("success") else "tool_failed",
            "preview": call.get("result_preview"),
        })
    ctx.evidence_ledger = {
        "summary": {
            "entry_count": len(entries),
            "success_count": sum(1 for item in entries if item["status"] == "success"),
            "failed_tools": [item["tool"] for item in entries if item["status"] != "success"],
            "entries": entries[-20:],
        },
        "full_ref": "selection_evidence_ledger.json",
    }


def _fallback_candidate_discovery(ctx: SelectionRunContext, seed_result: Dict[str, Any]) -> Dict[str, Any]:
    candidates = seed_result.get("candidates") if isinstance(seed_result, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    return {
        "stage": "candidate_discovery",
        "status": "ok" if candidates else "insufficient_candidates",
        "strategy": ctx.candidate_strategy,
        "market": ctx.market,
        "candidate_count": len(candidates),
        "summary": {
            "strategy": ctx.candidate_strategy,
            "candidate_codes": [str(item.get("code")) for item in candidates if isinstance(item, dict) and item.get("code")],
            "key_sources": list(dict.fromkeys(str(item.get("source") or "") for item in candidates if isinstance(item, dict)))[:5],
            "main_limitations": ["候选发现由工具结果生成，尚未完成逐只深度取证。"],
            "next_required_tools": [
                "get_realtime_quote",
                "analyze_trend",
                "analyze_price_structure",
                "get_capital_flow",
                "search_comprehensive_intel",
            ],
        },
        "full": {
            "candidates": candidates,
            "excluded": [],
            "tool_failures": [],
            "missing_evidence": [],
        },
        "full_ref": "candidate_discovery.json",
    }


def _merge_discovery_candidates(payload: Dict[str, Any], seed_result: Dict[str, Any]) -> None:
    full = payload.setdefault("full", {})
    candidates = full.get("candidates")
    if candidates:
        return
    seed_candidates = seed_result.get("candidates") if isinstance(seed_result, dict) else []
    if isinstance(seed_candidates, list):
        full["candidates"] = seed_candidates
        payload["candidate_count"] = len(seed_candidates)
        payload.setdefault("summary", {})["candidate_codes"] = [
            str(item.get("code")) for item in seed_candidates if isinstance(item, dict) and item.get("code")
        ]


def _fallback_candidate_screening(ctx: SelectionRunContext, base_evidence: Dict[str, Any]) -> Dict[str, Any]:
    quote_map = base_evidence.get("quotes") if isinstance(base_evidence, dict) else {}
    shortlist = []
    for code, quote in (quote_map or {}).items():
        if not isinstance(quote, dict) or quote.get("status") == "tool_failed":
            result = "monitor"
            score = 40
            reason = "行情数据缺失或工具失败，只能观察。"
        else:
            result = "deep_dive"
            score = 60
            reason = "候选具备基础行情数据，进入深度分析。"
        name = str(quote.get("name") or code) if isinstance(quote, dict) else str(code)
        shortlist.append({
            "code": code,
            "name": name,
            "market": ctx.market,
            "data_status": "ok" if result == "deep_dive" else "partial",
            "screening_result": result,
            "score": score,
            "score_breakdown": {
                "technical": 10,
                "fundamental": 5,
                "market_sector": 10,
                "news_event": 5,
                "account_fit": 10,
                "data_quality": 20 if result == "deep_dive" else 0,
            },
            "primary_reason": reason,
            "supporting_evidence": ["已获取基础行情"] if result == "deep_dive" else [],
            "risk_flags": [],
            "missing_evidence": ["尚未深度取证"],
        })
    return {
        "stage": "candidate_screening",
        "status": "ok" if shortlist else "insufficient_data",
        "summary": {
            "deep_dive_targets": [item["code"] for item in shortlist if item["screening_result"] == "deep_dive"][:DEFAULT_DEEP_DIVE_LIMIT],
            "monitor_targets": [item["code"] for item in shortlist if item["screening_result"] == "monitor"],
            "rejected_targets": [],
            "main_limitations": ["初筛 fallback 只使用基础行情，仍需深度分析。"],
            "audit_note": "深度分析阶段必须补足技术、资金、消息和基本面证据。",
        },
        "full": {"shortlist": shortlist, "tool_failures": []},
        "full_ref": "candidate_screening.json",
    }


def _fallback_deep_dive(code: str, name: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    quote = evidence.get("get_realtime_quote") if isinstance(evidence, dict) else {}
    trend = evidence.get("analyze_trend") if isinstance(evidence, dict) else {}
    price = quote.get("price") if isinstance(quote, dict) else None
    quote_basis = "latest_trading_day"
    if isinstance(quote, dict):
        session = str(quote.get("market_session") or "")
        if "open" in session:
            quote_basis = "intraday"
        elif quote.get("freshness_note"):
            quote_basis = "latest_trading_day"
    bias = trend.get("bias_ma5") if isinstance(trend, dict) else None
    trend_status = trend.get("trend_status") if isinstance(trend, dict) else None
    action = "wait"
    strength = "weak"
    risks: List[str] = []
    if isinstance(bias, (int, float)) and bias > 5:
        risks.append("乖离率过高，存在追高风险。")
    if trend_status and "空头" in str(trend_status):
        action = "reject"
        strength = "none"
        risks.append("趋势为空头结构。")
    return {
        "stage": "single_stock_deep_dive",
        "status": "partial",
        "summary": {
            "code": code,
            "name": name,
            "action_bias": action,
            "action_strength": strength,
            "quote_basis": quote_basis,
            "ideal_entry_zone": "等待回踩或突破确认",
            "no_chase_line": "高于关键压力位且乖离扩大时不追",
            "stop_loss": "跌破关键支撑或账户止损线",
            "main_supporting_evidence": [f"当前价={price}" if price is not None else "行情价格缺失"],
            "main_risks": risks,
            "main_missing_evidence": _missing_from_evidence(evidence),
        },
        "full": {
            "stock": {"code": code, "name": name, "market": "cn", "data_status": "partial"},
            "action_bias": action,
            "action_strength": strength,
            "quote_basis": quote_basis,
            "entry_quality": {
                "ideal_entry_zone": "等待回踩或突破确认",
                "secondary_entry_zone": "突破后回踩不破",
                "no_chase_line": "高于关键压力位且乖离扩大时不追",
                "stop_loss": "跌破关键支撑或账户止损线",
                "target_1": "前高或筹码压力位",
                "target_2": "趋势延伸位",
                "risk_reward_comment": "证据不足时只给条件型计划。",
            },
            "dimension_summary": _dimension_summary_from_evidence(evidence),
            "key_evidence": [],
            "risk_flags": risks,
            "failure_conditions": ["跌破关键支撑", "出现重大利空", "价格高于追高线且无回踩确认"],
            "missing_evidence": _missing_from_evidence(evidence),
            "tool_failures": _tool_failures_from_evidence(evidence),
        },
        "full_ref": f"single_stock_deep_dive_{code}.json",
    }


def _fallback_portfolio_allocation(ctx: SelectionRunContext) -> Dict[str, Any]:
    deep = ctx.stage_full("single_stock_deep_dive")
    results = deep.get("results") if isinstance(deep, dict) else []
    plans = []
    max_single = ctx.investor_profile.get("max_single_position_pct") or 20
    available_cash = ctx.account_summary.get("available_cash")
    for idx, item in enumerate(results or [], start=1):
        summary = item.get("summary") if isinstance(item, dict) else {}
        action = summary.get("action_bias") or "wait"
        if action == "open":
            initial_pct = min(10, float(max_single))
        elif action == "wait":
            initial_pct = 0
        else:
            initial_pct = 0
        plans.append({
            "rank": idx,
            "code": summary.get("code"),
            "name": summary.get("name"),
            "action": action,
            "action_strength": summary.get("action_strength") or "weak",
            "initial_position_pct": initial_pct,
            "initial_amount": (available_cash * initial_pct / 100) if isinstance(available_cash, (int, float)) else None,
            "entry_condition": summary.get("ideal_entry_zone") or "等待确认",
            "add_condition": "突破后回踩不破再评估",
            "stop_loss_condition": summary.get("stop_loss") or "跌破关键支撑",
            "take_profit_condition": "到达第一压力位分批止盈",
            "review_trigger": "下一交易日开盘或关键价格触发",
            "auto_downgrade_rules": ["如果价格高于 no_chase_line，降级为 wait"],
            "reason": "来自单股深度分析的条件型计划。",
            "risk_flags": summary.get("main_risks") or [],
        })
    total_pct = sum((item.get("initial_position_pct") or 0) for item in plans)
    action = "open" if total_pct > 0 else "wait"
    return {
        "stage": "portfolio_allocation",
        "status": "partial",
        "summary": {
            "portfolio_action": action,
            "recommended_position_count": sum(1 for item in plans if (item.get("initial_position_pct") or 0) > 0),
            "initial_total_position_pct": total_pct,
            "reserved_cash_pct": max(0, 100 - total_pct),
            "core_reason": "按账户约束生成条件型组合计划。",
            "main_constraint": "证据缺口或行情时效约束",
            "positions_plan_brief": [
                {"code": item.get("code"), "action": item.get("action"), "initial_position_pct": item.get("initial_position_pct")}
                for item in plans
            ],
        },
        "full": {
            "account_constraints": {
                "currency": ctx.account_summary.get("base_currency") or "CNY",
                "available_cash": available_cash,
                "max_single_position_pct": max_single,
                "max_total_equity_exposure_pct": ctx.investor_profile.get("max_total_equity_exposure_pct"),
                "default_stop_loss_pct": ctx.investor_profile.get("default_stop_loss_pct"),
            },
            "positions_plan": plans,
            "execution_matrix": [],
            "cash_plan": {"reserved_cash_pct": max(0, 100 - total_pct), "reason": "保留现金等待确认和回撤机会。"},
            "risk_controls": ["价格高于追高线不买", "工具证据缺失时降低动作强度"],
            "missing_evidence": [],
        },
        "full_ref": "portfolio_allocation.json",
    }


def _summarize_market_regime(regime: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(regime, dict) or not regime:
        return {"status": "missing", "regime": "unknown", "risk_level": "unknown"}
    return {
        "status": regime.get("status"),
        "regime": regime.get("regime"),
        "risk_level": regime.get("risk_level"),
        "volatility_bucket": regime.get("volatility_bucket"),
        "sentiment_state": regime.get("sentiment_state"),
        "wyckoff_phase": regime.get("wyckoff_phase"),
        "risk_multiplier": regime.get("risk_multiplier"),
        "strategy_hints": list(regime.get("strategy_hints") or [])[:5],
        "data_quality": regime.get("data_quality"),
        "conflicts": list(regime.get("conflicts") or [])[:5],
    }


def _apply_market_regime_constraints(
    ctx: SelectionRunContext,
    allocation_payload: Dict[str, Any],
) -> Dict[str, Any]:
    regime = ctx.market_regime if isinstance(ctx.market_regime, dict) else {}
    regime_name = str(regime.get("regime") or "").strip().lower()
    risk_level = str(regime.get("risk_level") or "").strip().lower()
    volatility = str(regime.get("volatility_bucket") or "").strip().lower()
    restrictive = (
        regime_name in {"risk_off", "panic"}
        or risk_level in {"high", "extreme"}
        or volatility == "extreme"
    )
    high_vol = restrictive or volatility in {"high_vol", "extreme"} or risk_level == "medium_high"
    if not high_vol:
        return allocation_payload

    payload = dict(allocation_payload or {})
    summary = dict(payload.get("summary") or {})
    full = dict(payload.get("full") or {})
    positions = list(full.get("positions_plan") or [])
    rule = _market_regime_rule_text(regime)

    adjusted_positions: List[Dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        plan = dict(item)
        action = str(plan.get("action") or "").strip().lower()
        pct = plan.get("initial_position_pct")
        try:
            pct_num = float(pct or 0)
        except Exception:
            pct_num = 0.0
        if restrictive and action == "open":
            plan["action"] = "wait"
            plan["action_strength"] = "none"
            plan["initial_position_pct"] = 0
            plan["initial_amount"] = 0
            plan["reason"] = f"{plan.get('reason') or ''}；{rule}".strip("；")
        elif high_vol and action == "open":
            capped = min(pct_num, 5.0)
            plan["initial_position_pct"] = capped
            if isinstance(plan.get("initial_amount"), (int, float)) and pct_num > 0:
                plan["initial_amount"] = plan["initial_amount"] * capped / pct_num
            plan["reason"] = f"{plan.get('reason') or ''}；{rule}".strip("；")
        rules = list(plan.get("auto_downgrade_rules") or [])
        if rule not in rules:
            rules.append(rule)
        plan["auto_downgrade_rules"] = rules
        adjusted_positions.append(plan)

    total_pct = sum(float(item.get("initial_position_pct") or 0) for item in adjusted_positions)
    if restrictive:
        summary["portfolio_action"] = "wait"
        summary["recommended_position_count"] = 0
        summary["initial_total_position_pct"] = 0
        summary["reserved_cash_pct"] = 100
        summary["main_constraint"] = rule
    elif high_vol:
        summary["initial_total_position_pct"] = total_pct
        summary["reserved_cash_pct"] = max(0, 100 - total_pct)
        summary["main_constraint"] = summary.get("main_constraint") or rule
    full["positions_plan"] = adjusted_positions
    risk_controls = list(full.get("risk_controls") or [])
    if rule not in risk_controls:
        risk_controls.append(rule)
    full["risk_controls"] = risk_controls
    full["market_regime_constraint"] = _summarize_market_regime(regime)
    payload["summary"] = summary
    payload["full"] = full
    return payload


def _market_regime_rule_text(regime: Dict[str, Any]) -> str:
    regime_name = regime.get("regime") or "unknown"
    volatility = regime.get("volatility_bucket") or "unknown"
    return f"市场状态约束：regime={regime_name}, volatility={volatility}，降低主动开仓和追高权重"


def _fallback_adversarial_review(ctx: SelectionRunContext) -> Dict[str, Any]:
    return {
        "stage": "adversarial_review",
        "status": "ok",
        "summary": {
            "opposing_summary": "反方认为当前选股和仓位配置仍需防范追高、证据缺口和账户风险。",
            "top_risk_points": ["候选可能依赖热点板块", "资金面或消息面可能缺失", "价格高于追高线时不宜开仓"],
            "top_evidence_gaps": _all_missing_evidence(ctx),
            "recommended_verdict": "accept_with_changes",
        },
        "full": {
            "opposing_thesis": {
                "summary": "需要等待关键证据确认后再执行。",
                "risk_points": ["追高风险", "数据缺口", "仓位执行风险"],
                "evidence_gaps": _all_missing_evidence(ctx),
                "failure_scenarios": ["热点退潮", "开盘跳高后回落", "重大利空出现"],
                "plan_changes_required": ["高于 no_chase_line 时降级为 wait"],
            },
            "missing_evidence": _all_missing_evidence(ctx),
        },
        "full_ref": "adversarial_review.json",
    }


def _fallback_judge_decision(ctx: SelectionRunContext) -> Dict[str, Any]:
    allocation = ctx.stage_summary("portfolio_allocation")
    action = allocation.get("portfolio_action") or "wait"
    missing = _all_missing_evidence(ctx)
    if missing and action == "open":
        action = "wait"
    return {
        "stage": "judge_decision",
        "status": "ok",
        "summary": {
            "primary_plan_verdict": "accept_with_changes" if missing else "accept",
            "final_action": action,
            "decision_summary": "采纳组合配置的条件型框架，但证据缺口或追高风险存在时必须等待确认。",
            "next_step": "render_final_report",
        },
        "full": {
            "winner": "mixed",
            "accepted_arguments": ["保留条件型执行计划"],
            "rejected_arguments": ["证据不足时立即开仓"],
            "required_plan_changes": ["高于 no_chase_line 降级为 wait"],
            "risk_controls": ["不追高", "先确认行情口径", "资金/消息缺失时降低仓位"],
            "fallback_path": {
                "when": "wait_for_more_data",
                "next_step": "request_user_input",
                "reason": "需要补充偏好或等待交易日数据。",
            },
        },
        "full_ref": "judge_decision.json",
    }


def _build_final_report_json(ctx: SelectionRunContext) -> Dict[str, Any]:
    return {
        "selection_context": ctx.to_dict(include_full=False),
        "market_regime": ctx.market_regime,
        "orchestration_mode": ctx.orchestration_mode,
        "expert_state": ctx.expert_state.to_trace_dict() if ctx.expert_state else None,
        "candidate_discovery": ctx.stages.get("candidate_discovery", SelectionStage()).to_dict(include_full=True),
        "candidate_screening": ctx.stages.get("candidate_screening", SelectionStage()).to_dict(include_full=True),
        "single_stock_deep_dive": ctx.stages.get("single_stock_deep_dive", SelectionStage()).to_dict(include_full=True),
        "portfolio_allocation": ctx.stages.get("portfolio_allocation", SelectionStage()).to_dict(include_full=True),
        "adversarial_review": ctx.stages.get("adversarial_review", SelectionStage()).to_dict(include_full=True),
        "judge_decision": ctx.stages.get("judge_decision", SelectionStage()).to_dict(include_full=True),
        "evidence_ledger": ctx.evidence_ledger,
    }


def _combine_deep_dive_outputs(outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries = [item.get("summary") for item in outputs if isinstance(item.get("summary"), dict)]
    return {
        "stage": "single_stock_deep_dive",
        "status": "ok" if outputs else "insufficient_data",
        "summary": {
            "target_count": len(outputs),
            "open_targets": [item.get("code") for item in summaries if item.get("action_bias") == "open"],
            "wait_targets": [item.get("code") for item in summaries if item.get("action_bias") == "wait"],
            "reject_targets": [item.get("code") for item in summaries if item.get("action_bias") == "reject"],
            "results_brief": summaries,
        },
        "full": {"results": outputs},
        "full_ref": "deep_dive_results.json",
    }


def _attach_evidence_cards(payload: Dict[str, Any], cards: List[Any]) -> None:
    """Attach evidence cards to a deep-dive payload without changing LLM summary."""
    if not isinstance(payload, dict) or not cards:
        return
    full = payload.get("full")
    if not isinstance(full, dict):
        full = {}
        payload["full"] = full
    full["evidence_cards"] = cards_to_json(cards)
    full["evidence_card_count"] = len(cards)


def _candidate_codes(full: Dict[str, Any], *, limit: int) -> List[str]:
    candidates = full.get("candidates") if isinstance(full, dict) else []
    codes: List[str] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
        if len(codes) >= limit:
            break
    return codes


def _deep_dive_targets(summary: Dict[str, Any], full: Dict[str, Any]) -> List[str]:
    targets = summary.get("deep_dive_targets") if isinstance(summary, dict) else []
    if isinstance(targets, list) and targets:
        return [str(item) for item in targets if item]
    shortlist = full.get("shortlist") if isinstance(full, dict) else []
    codes = []
    for item in shortlist or []:
        if isinstance(item, dict) and item.get("screening_result") == "deep_dive" and item.get("code"):
            codes.append(str(item["code"]))
    return codes


def _stock_name_from_evidence(code: str, evidence: Dict[str, Any]) -> str:
    for item in evidence.values():
        if isinstance(item, dict) and item.get("name"):
            return str(item["name"])
    return code


def _resolve_market(context: AgentUserContext) -> str:
    if context.investor.preferred_markets:
        market = context.investor.preferred_markets[0]
        return "cn" if market == "mixed" else str(market)
    for account in context.accounts:
        if account.market:
            return "cn" if account.market == "mixed" else str(account.market)
    return "cn"


def _account_summary(context: AgentUserContext) -> Dict[str, Any]:
    account = context.accounts[0] if context.accounts else None
    if not account:
        return {}
    return {
        "account_id": account.account_id,
        "account_name": account.account_name,
        "broker": account.broker,
        "market": account.market,
        "base_currency": account.base_currency,
        "total_equity": account.total_equity,
        "available_cash": account.available_cash,
        "total_market_value": account.total_market_value,
        "cost_method": account.cost_method,
    }


def _investor_profile(context: AgentUserContext) -> Dict[str, Any]:
    investor = context.investor
    return {
        "risk_preference": investor.risk_preference,
        "trading_horizon": investor.trading_horizon,
        "preferred_markets": investor.preferred_markets,
        "max_single_position_pct": investor.max_single_position_pct,
        "max_total_equity_exposure_pct": investor.max_total_equity_exposure_pct,
        "max_acceptable_drawdown_pct": investor.max_acceptable_drawdown_pct,
        "default_stop_loss_pct": investor.default_stop_loss_pct,
        "allow_margin": investor.allow_margin,
        "allow_short_selling": investor.allow_short_selling,
        "notes": investor.notes,
    }


def _positions_summary(context: AgentUserContext) -> List[Dict[str, Any]]:
    return [
        {
            "symbol": position.symbol,
            "name": _canonical_stock_name(position.symbol, getattr(position, "name", None)),
            "quantity": position.quantity,
            "avg_cost": position.avg_cost,
            "market_value": position.market_value,
            "weight_pct": position.position_pct,
        }
        for position in context.positions
    ]


def _missing_from_evidence(evidence: Dict[str, Any]) -> List[str]:
    missing = []
    for key, value in evidence.items():
        if isinstance(value, dict) and value.get("status") in {"failed", "tool_failed", "missing"}:
            missing.append(key)
    return missing


def _tool_failures_from_evidence(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    failures = []
    for key, value in evidence.items():
        if isinstance(value, dict) and value.get("status") in {"failed", "tool_failed"}:
            failures.append({"tool": key, "error": value.get("error") or value.get("errors")})
    return failures


def _dimension_summary_from_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    failures = set(_missing_from_evidence(evidence))
    regime = evidence.get("detect_market_regime") if isinstance(evidence, dict) else {}
    regime_summary = _summarize_market_regime(regime) if isinstance(regime, dict) else {}
    return {
        "technical": {
            "verdict": "tool_failed" if "analyze_trend" in failures else ("support" if evidence.get("analyze_trend") else "missing"),
            "summary": _truncate(json.dumps(evidence.get("analyze_trend") or {}, ensure_ascii=False, default=str), 500),
        },
        "price_structure": {
            "verdict": "tool_failed" if "analyze_price_structure" in failures else (
                "support" if evidence.get("analyze_price_structure") else "missing"
            ),
            "summary": _truncate(json.dumps(evidence.get("analyze_price_structure") or {}, ensure_ascii=False, default=str), 500),
        },
        "fundamental": {
            "verdict": "tool_failed" if "get_stock_info" in failures else ("neutral" if evidence.get("get_stock_info") else "missing"),
            "summary": _truncate(json.dumps(evidence.get("get_stock_info") or {}, ensure_ascii=False, default=str), 500),
        },
        "news_event": {
            "verdict": "tool_failed" if "search_comprehensive_intel" in failures else ("neutral" if evidence.get("search_comprehensive_intel") else "missing"),
            "summary": _truncate(json.dumps(evidence.get("search_comprehensive_intel") or {}, ensure_ascii=False, default=str), 500),
        },
        "capital_flow": {
            "verdict": "tool_failed" if "get_capital_flow" in failures else ("neutral" if evidence.get("get_capital_flow") else "missing"),
            "summary": _truncate(json.dumps(evidence.get("get_capital_flow") or {}, ensure_ascii=False, default=str), 500),
        },
        "market_sector": {
            "verdict": "weaken" if regime_summary.get("regime") in {"risk_off", "panic"} else "neutral",
            "summary": _truncate(json.dumps(regime_summary or {"note": "候选发现阶段已参考市场/板块证据。"}, ensure_ascii=False, default=str), 500),
        },
        "account_fit": {"verdict": "neutral", "summary": "组合配置阶段按账户约束处理。"},
    }


def _all_missing_evidence(ctx: SelectionRunContext) -> List[str]:
    missing: List[str] = []
    for stage in ctx.stages.values():
        full = stage.full
        stage_missing = full.get("missing_evidence") if isinstance(full, dict) else None
        if isinstance(stage_missing, list):
            missing.extend(str(item) for item in stage_missing if item)
        results = full.get("results") if isinstance(full, dict) else None
        if isinstance(results, list):
            for result in results:
                result_full = result.get("full") if isinstance(result, dict) else {}
                values = result_full.get("missing_evidence") if isinstance(result_full, dict) else None
                if isinstance(values, list):
                    missing.extend(str(item) for item in values if item)
    return list(dict.fromkeys(missing))


def _summarize_payload(payload: Any, *, max_chars: int = 3000) -> Any:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return payload
    return {"truncated": True, "preview": text[:max_chars], "original_chars": len(text)}


def _accumulate_usage(ctx: SelectionRunContext, response: LLMResponse) -> None:
    usage = response.usage or {}
    try:
        ctx.total_tokens += int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        pass
    model = response.model
    if isinstance(model, str) and model:
        ctx.models_used.append(model)


def _finalize_result(result: StockSelectionResult) -> None:
    if not result.context:
        return
    result.tool_calls_log = result.context.tool_calls
    result.total_tokens = result.context.total_tokens
    result.models_used = _unique_text_items(result.context.models_used)


def _unique_text_items(items: List[Any]) -> List[str]:
    values: List[str] = []
    for item in items:
        if isinstance(item, str) and item and item not in values:
            values.append(item)
    return values


def _emit(callback: Optional[Callable[[Dict[str, Any]], None]], event_type: str, **payload: Any) -> None:
    if not callback:
        return
    event = {"type": event_type, **payload}
    try:
        callback(event)
    except Exception:
        logger.debug("Selection progress callback failed", exc_info=True)


def _format_pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):g}%"
    except (TypeError, ValueError):
        return str(value)


def _markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "-")
    return text.replace("|", "\\|").replace("\n", " ")


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"...[truncated {len(value) - max_chars} chars]"
