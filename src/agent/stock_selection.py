# -*- coding: utf-8 -*-
"""Staged stock-selection pipeline for planning_execute watchlist scans."""

from __future__ import annotations

import json
import logging
import os
import re
import time
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
from src.config import Config
from src.schemas.agent_context import AgentUserContext

logger = logging.getLogger(__name__)


SELECTION_INTENT = "watchlist_scan"
DEFAULT_CANDIDATE_LIMIT = 8
DEFAULT_DEEP_DIVE_LIMIT = 6
MIN_RICH_REPORT_DEEP_DIVE_TARGETS = 3
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
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
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
            "tool_call_count": len(self.tool_calls),
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
        progress_callback=progress_callback,
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
                "tool_seed_result": _compact_candidate_seed(discovery_seed),
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
                "evidence_ledger_summary": _compact_evidence_ledger_for_prompt(ctx),
                "base_evidence": _compact_base_evidence(base_evidence),
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
        deep_dive_limit = _selection_deep_dive_limit()
        deep_targets = _expand_deep_dive_targets_for_rich_report(
            deep_targets=deep_targets,
            candidates=candidates,
            screening_full=ctx.stage_full("candidate_screening"),
        )
        deep_targets = deep_targets[:deep_dive_limit]
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
            prompt_evidence = _deep_dive_prompt_evidence(
                ctx=ctx,
                code=code,
                stock_name=stock_name,
                detailed_evidence=detailed_evidence,
                evidence_cards=evidence_cards,
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
                    "evidence_ledger_summary": _compact_evidence_ledger_for_prompt(ctx),
                    "stock_evidence": prompt_evidence,
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
                "evidence_ledger_summary": _compact_evidence_ledger_for_prompt(ctx),
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
                "evidence_ledger_summary": _compact_evidence_ledger_for_prompt(ctx),
            }),
            fallback=_fallback_judge_decision(ctx),
            timeout_seconds=timeout_seconds,
        )
        _stabilize_judge_decision(ctx, judge_payload)
        _apply_judge_position_overrides(ctx, judge_payload)
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
    """Render final stock-selection JSON into a user-readable report."""
    judge = report.get("judge_decision", {}).get("summary", {})
    allocation = report.get("portfolio_allocation", {}).get("summary", {})
    positions = report.get("portfolio_allocation", {}).get("full", {}).get("positions_plan", [])
    adversarial = report.get("adversarial_review", {}).get("summary", {})
    discovery = report.get("candidate_discovery", {})
    discovery_summary = discovery.get("summary") if isinstance(discovery.get("summary"), dict) else {}
    discovery_full = discovery.get("full") if isinstance(discovery.get("full"), dict) else {}
    candidates = discovery_full.get("candidates") if isinstance(discovery_full.get("candidates"), list) else []
    llm_candidate_summary = (
        discovery_full.get("llm_candidate_summary")
        if isinstance(discovery_full.get("llm_candidate_summary"), list)
        else []
    )
    llm_candidate_by_code = {
        str(item.get("code") or item.get("stock_code")): item
        for item in llm_candidate_summary
        if isinstance(item, dict) and (item.get("code") or item.get("stock_code"))
    }
    candidate_quality = discovery_full.get("quality") if isinstance(discovery_full.get("quality"), dict) else {}
    screening_summary = report.get("candidate_screening", {}).get("summary", {})
    deep_dive_full = report.get("single_stock_deep_dive", {}).get("full", {})
    deep_results = deep_dive_full.get("results") if isinstance(deep_dive_full, dict) and isinstance(deep_dive_full.get("results"), list) else []
    all_missing = _report_missing_evidence(report)
    core_reason = _primary_report_core_reason(
        allocation=allocation,
        deep_results=deep_results,
        judge=judge,
    )
    review_note = _auxiliary_review_note(judge)
    recommendations = _recommendation_items(
        positions=positions,
        deep_results=deep_results,
        candidates=candidates,
        llm_candidate_by_code=llm_candidate_by_code,
    )
    actionable_recommendations = [item for item in recommendations if _is_actionable_recommendation(item)]
    observation_items = [item for item in recommendations if not _is_actionable_recommendation(item) and _is_observable_item(item)]

    lines = [
        "# 选股分析报告：下周可关注候选",
        "",
        "## 一、核心推荐结论",
        "",
        "| 项目 | 结论 |",
        "| --- | --- |",
        f"| 最终动作 | {judge.get('final_action') or allocation.get('portfolio_action') or '-'} |",
        f"| 裁决 | {judge.get('primary_plan_verdict') or '-'} |",
        f"| 首选标的 | {_markdown_cell(_preferred_candidate_label(actionable_recommendations))} |",
        f"| 可观察标的 | {_markdown_cell(_watch_candidate_labels(observation_items))} |",
        f"| 核心原因 | {_markdown_cell(core_reason)} |",
        f"| 最大约束 | {_markdown_cell(allocation.get('main_constraint') or '-')} |",
        f"| 候选池规模 | {_markdown_cell(discovery_summary.get('source_count') or discovery.get('candidate_count') or candidate_quality.get('candidate_count') or len(candidates))} 只 |",
        "",
        "## 二、推荐排序与入场决策",
        "",
    ]

    if actionable_recommendations:
        for idx, item in enumerate(actionable_recommendations[:4], start=1):
            lines.extend(_render_recommendation_block(idx, item))
    else:
        lines.extend([
            "本轮没有形成可直接入手的股票。原因是组合配置和 Judge 均指向等待，且深度分析标的缺少明确入场价、止损线或存在反向证据。",
            "",
        ])

    if observation_items:
        lines.extend([
            "### 观察池",
            "",
            "以下股票只代表后续跟踪对象，不代表可以买入；入池分只衡量召回强度，不等于推荐分。",
            "",
            "| 股票 | 当前状态 | 观察原因 | 主要风险/缺口 |",
            "| --- | --- | --- | --- |",
        ])
        for item in observation_items[:6]:
            lines.append(
                "| {stock} | {status} | {reason} | {risk} |".format(
                    stock=_markdown_cell(_stock_label(item)),
                    status=_markdown_cell(_observation_status(item)),
                    reason=_markdown_cell(_observation_reason(item)),
                    risk=_markdown_cell(_observation_risk(item)),
                )
            )
        lines.append("")

    lines.extend([
        "",
        "## 三、Execute 证据摘要",
        "",
        "| 证据项 | 状态 | 关键结果 | 对选股结论的影响 |",
        "| --- | --- | --- | --- |",
    ])
    for row in _evidence_summary_rows(report, candidates, deep_results):
        lines.append(
            "| {name} | {status} | {result} | {impact} |".format(
                name=_markdown_cell(row.get("name") or "-"),
                status=_markdown_cell(row.get("status") or "-"),
                result=_markdown_cell(row.get("result") or "-"),
                impact=_markdown_cell(row.get("impact") or "-"),
            )
        )

    lines.extend([
        "",
        "## 四、关键风险与等待确认",
        "",
    ])
    risk_lines = _final_risk_lines(recommendations, adversarial, all_missing)
    if risk_lines:
        lines.extend([f"- {_markdown_cell(item)}" for item in risk_lines])
    else:
        lines.append("- 当前没有结构化风险条目，但仍需按价格、仓位和数据时效执行风控。")

    lines.extend([
        "",
        "## 五、组合配置表",
        "",
        "| 排名 | 股票 | 动作 | 首仓比例 | 入场条件 | 止损条件 | 复查触发 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
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
        "## 六、辅助审查摘要",
        "",
        "反方审查和 Judge 裁决只作为主方案的校验层，完整原始输出保留在 Trace artifact 中。",
        "",
        "| 审查项 | 摘要 |",
        "| --- | --- |",
        f"| 辅助审查 | {_markdown_cell(review_note)} |",
        f"| 反方提醒 | {_markdown_cell(_brief_markdown_text(adversarial.get('opposing_summary') or '-', 180))} |",
        f"| Judge 调整 | {_markdown_cell(_brief_markdown_text(judge.get('decision_summary') or '-', 180))} |",
    ])
    top_risks = adversarial.get("top_risk_points") if isinstance(adversarial.get("top_risk_points"), list) else []
    if top_risks:
        lines.extend(["", "审查关注风险："])
        lines.extend([f"- {_markdown_cell(_brief_markdown_text(item, 160))}" for item in top_risks[:3]])
    if all_missing:
        lines.extend(["", "待补关键证据："])
        lines.extend([f"- {_markdown_cell(_brief_markdown_text(item, 160))}" for item in all_missing[:2]])

    lines.extend([
        "",
        "## 附录一、候选池来源与入池理由",
        "",
        "这部分用于追溯候选为什么进池，不代表买入结论。",
        "",
        "按入池分降序展示；入池分只衡量 L1 召回强度，不代表推荐买入。",
        "",
        "| 排名 | 股票 | 入池通道 | 入池分 | 深度分析 | 入池理由 | 关注点 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    if candidates:
        deep_analyzed_codes = _deep_dive_code_set(deep_results)
        for idx, display_item in enumerate(_candidate_appendix_items(candidates, llm_candidate_by_code)[:12], start=1):
            labels = _candidate_labels(display_item)
            lines.append(
                "| {rank} | {stock} | {source} | {score} | {deep_status} | {reason} | {labels} |".format(
                    rank=idx,
                    stock=_markdown_cell(_stock_label(display_item)),
                    source=_markdown_cell(_candidate_source_label(display_item)),
                    score=_markdown_cell(_format_score(_candidate_display_score(display_item))),
                    deep_status=_markdown_cell(_candidate_deep_dive_status(display_item, deep_analyzed_codes)),
                    reason=_markdown_cell(_candidate_reason(display_item)),
                    labels=_markdown_cell("、".join(labels[:6]) if labels else "-"),
                )
            )
    else:
        lines.append("| - | - | - | - | - | 本轮没有形成可用候选池。 | - |")

    if candidate_quality:
        lines.extend([
            "",
            "候选池质量摘要："
            f"候选 {candidate_quality.get('candidate_count', len(candidates))} 只；"
            f"多源共振 {candidate_quality.get('multi_source_count', 0)} 只；"
            f"兜底观察 {candidate_quality.get('fallback_count', 0)} 只；"
            f"硬排除 {candidate_quality.get('hard_exclusion_count', 0)} 只。",
        ])

    if deep_results:
        lines.extend([
            "",
            "## 附录二、逐股维度证据展开",
        ])
        for result in deep_results:
            if not isinstance(result, dict):
                continue
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            full = result.get("full") if isinstance(result.get("full"), dict) else {}
            stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
            lines.extend([
                "",
                f"### {_markdown_cell(_stock_label({**stock, **summary}))}",
                "",
                "| 维度 | 结论 | 证据摘要 |",
                "| --- | --- | --- |",
            ])
            dimension_summary = full.get("dimension_summary") if isinstance(full.get("dimension_summary"), dict) else {}
            if dimension_summary:
                for key, item in dimension_summary.items():
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        "| {dimension} | {verdict} | {summary} |".format(
                            dimension=_markdown_cell(_dimension_label(key)),
                            verdict=_markdown_cell(_verdict_label(item.get("verdict"))),
                            summary=_markdown_cell(_truncate(str(item.get("summary") or "-"), 240)),
                        )
                    )
            else:
                lines.append("| - | - | 本轮没有返回维度证据摘要。 |")

            evidence_items = _as_text_list(full.get("key_evidence") or summary.get("main_supporting_evidence"))
            risk_items = _as_text_list(full.get("risk_flags") or summary.get("main_risks"))
            failure_items = _as_text_list(full.get("failure_conditions"))
            missing_items = _as_text_list(full.get("missing_evidence") or summary.get("main_missing_evidence"))
            if evidence_items:
                lines.extend(["", "关键支持证据："])
                lines.extend([f"- {_markdown_cell(item)}" for item in evidence_items[:5]])
            if risk_items:
                lines.extend(["", "主要反证/风险："])
                lines.extend([f"- {_markdown_cell(item)}" for item in risk_items[:5]])
            if failure_items:
                lines.extend(["", "失效条件："])
                lines.extend([f"- {_markdown_cell(item)}" for item in failure_items[:5]])
            if missing_items:
                lines.extend(["", "仍缺证据："])
                lines.extend([f"- {_markdown_cell(item)}" for item in missing_items[:5]])

    risk_controls = report.get("judge_decision", {}).get("full", {}).get("risk_controls", [])
    if risk_controls:
        lines.extend(["", "## 附录三、风控条件"])
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
    step = len(ctx.tool_calls) + 1
    call: Dict[str, Any] = {
        "step": step,
        "tool": tool_name,
        "arguments": arguments,
        "success": False,
        "result_preview": "",
        "selection_stage": True,
    }
    started_at = _monotonic_time()
    _emit(
        ctx.progress_callback,
        "tool_start",
        step=step,
        tool=tool_name,
        arguments=arguments,
        selection_stage=True,
    )
    try:
        if tool_registry.get(tool_name) is None:
            raise KeyError(f"Tool '{tool_name}' not registered")
        result = tool_registry.execute(tool_name, **arguments)
        call["success"] = not _is_failed_tool_result(result)
        call["result_json"] = _compact_tool_result_for_trace(result)
        call["result_preview"] = _truncate(json.dumps(result, ensure_ascii=False, default=str), 1200)
        call["result_length"] = len(json.dumps(result, ensure_ascii=False, default=str))
        return result
    except Exception as exc:
        error_payload = {"status": "tool_failed", "tool": tool_name, "error": str(exc)}
        call["success"] = False
        call["result_json"] = error_payload
        call["result_preview"] = json.dumps(error_payload, ensure_ascii=False)
        call["result_length"] = len(call["result_preview"])
        return error_payload
    finally:
        call["duration"] = round(_monotonic_time() - started_at, 3)
        ctx.tool_calls.append(call)
        _refresh_evidence_summary(ctx)
        _emit(ctx.progress_callback, "tool_done", **call)


def _monotonic_time() -> float:
    return time.monotonic()


def _compact_tool_result_for_trace(result: Any) -> Any:
    """Keep structured trace payloads useful without duplicating large raw blobs."""
    if not isinstance(result, dict):
        return result
    candidates = result.get("candidates")
    if isinstance(candidates, list) and result.get("status") is not None:
        return _compact_candidate_seed(result, limit=DEFAULT_CANDIDATE_LIMIT)
    if result.get("expert_packets") or result.get("discovery_steps") or result.get("quality"):
        return _compact_candidate_seed(result, limit=DEFAULT_CANDIDATE_LIMIT)
    return result


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


def _compact_candidate_seed(seed_result: Dict[str, Any], *, limit: int = DEFAULT_CANDIDATE_LIMIT) -> Dict[str, Any]:
    if not isinstance(seed_result, dict):
        return {"status": "invalid", "candidate_count": 0, "candidates": []}
    candidates = seed_result.get("candidates") if isinstance(seed_result.get("candidates"), list) else []
    compact_candidates: List[Dict[str, Any]] = []
    for item in candidates[:limit]:
        if not isinstance(item, dict):
            continue
        compact_candidates.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "market": item.get("market"),
            "source": item.get("source"),
            "signal_score": item.get("signal_score"),
            "final_score": item.get("final_score"),
            "score": item.get("score"),
            "reason": _truncate(str(item.get("reason") or ""), 360),
            "reason_dimensions": _compact_reason_dimensions(item.get("reason_dimensions")),
            "recall_sources": _as_text_list(item.get("recall_sources"))[:6],
            "matched_strategies": _as_text_list(item.get("matched_strategies"))[:6],
            "strategy_tags": _as_text_list(item.get("strategy_tags"))[:8],
        })
    return {
        "status": seed_result.get("status"),
        "market": seed_result.get("market"),
        "candidate_source": seed_result.get("candidate_source"),
        "candidate_count": seed_result.get("candidate_count") or len(candidates),
        "candidates": compact_candidates,
        "quality_summary": seed_result.get("quality_summary"),
        "lifecycle_summary": seed_result.get("lifecycle_summary"),
        "source_summary": _compact_source_summary(seed_result.get("source_summary")),
        "discovery_steps": _compact_discovery_steps(seed_result.get("discovery_steps")),
        "theme_observations": _compact_theme_observations(seed_result),
        "errors": _as_text_list(seed_result.get("errors"))[:6],
    }


def _compact_reason_dimensions(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        result.append({
            "dimension": item.get("dimension"),
            "label": item.get("label"),
            "detail": _truncate(str(item.get("detail") or ""), 240),
        })
    return result


def _compact_source_summary(value: Any) -> Any:
    if isinstance(value, list):
        return value[:10]
    if isinstance(value, dict):
        return {
            key: value.get(key)
            for key in ("strategy", "technical", "capital", "fundamental", "sector", "news", "sentiment")
            if key in value
        }
    return value


def _compact_discovery_steps(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    steps: List[Dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        steps.append({
            "source": item.get("source") or item.get("expert") or item.get("candidate_source"),
            "status": item.get("status"),
            "count": item.get("count") or item.get("candidate_count"),
            "errors": _as_text_list(item.get("errors"))[:3],
            "message": _truncate(str(item.get("message") or item.get("summary") or ""), 240),
        })
    return steps


def _compact_theme_observations(seed_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    for key in ("theme_observations", "themes", "watch_only_events"):
        value = seed_result.get(key)
        if isinstance(value, list):
            for item in value[:6]:
                if isinstance(item, dict):
                    observations.append({
                        "theme": item.get("theme") or item.get("title") or item.get("name"),
                        "summary": _truncate(str(item.get("summary") or item.get("reason") or ""), 260),
                        "status": item.get("status"),
                    })
                elif item:
                    observations.append({"theme": str(item), "summary": ""})
    return observations[:6]


def _compact_evidence_ledger_for_prompt(ctx: SelectionRunContext) -> Dict[str, Any]:
    summary = ctx.evidence_ledger.get("summary") if isinstance(ctx.evidence_ledger, dict) else {}
    entries = summary.get("entries") if isinstance(summary, dict) and isinstance(summary.get("entries"), list) else []
    recent: List[Dict[str, Any]] = []
    for entry in entries[-12:]:
        if not isinstance(entry, dict):
            continue
        recent.append({
            "tool": entry.get("tool"),
            "arguments": entry.get("arguments"),
            "status": entry.get("status"),
        })
    return {
        "entry_count": summary.get("entry_count") if isinstance(summary, dict) else len(ctx.tool_calls),
        "success_count": summary.get("success_count") if isinstance(summary, dict) else None,
        "failed_tools": list(dict.fromkeys(summary.get("failed_tools") or []))[:12] if isinstance(summary, dict) else [],
        "recent_tools": recent,
        "full_ref": ctx.evidence_ledger.get("full_ref") if isinstance(ctx.evidence_ledger, dict) else "selection_evidence_ledger.json",
    }


def _compact_base_evidence(base_evidence: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(base_evidence, dict):
        return {}
    quotes = base_evidence.get("quotes") if isinstance(base_evidence.get("quotes"), dict) else {}
    compact_quotes: Dict[str, Any] = {}
    for code, quote in list(quotes.items())[:DEFAULT_CANDIDATE_LIMIT]:
        compact_quotes[str(code)] = _compact_quote(quote)
    return {
        "detect_market_regime": _summarize_market_regime(base_evidence.get("detect_market_regime") or {}),
        "market_indices": _compact_market_indices(base_evidence.get("get_market_indices")),
        "sector_rankings": _compact_sector_rankings(base_evidence.get("get_sector_rankings")),
        "quotes": compact_quotes,
    }


def _compact_quote(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "invalid"}
    return {
        "status": value.get("status", "ok"),
        "code": value.get("code") or value.get("stock_code"),
        "name": value.get("name"),
        "price": value.get("price") or value.get("current_price"),
        "change_pct": value.get("change_pct") or value.get("pct_chg"),
        "turnover_rate": value.get("turnover_rate"),
        "volume_ratio": value.get("volume_ratio"),
        "quote_trade_date": value.get("quote_trade_date") or value.get("latest_date") or value.get("date"),
        "market_session": value.get("market_session"),
        "errors": _as_text_list(value.get("errors"))[:3],
    }


def _compact_market_indices(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "indices": []}
    indices = value.get("indices") if isinstance(value.get("indices"), list) else []
    return {
        "status": value.get("status", "ok"),
        "indices": [
            {
                "name": item.get("name"),
                "code": item.get("code"),
                "price": item.get("price") or item.get("close"),
                "change_pct": item.get("change_pct") or item.get("pct_chg"),
            }
            for item in indices[:8]
            if isinstance(item, dict)
        ],
        "errors": _as_text_list(value.get("errors"))[:3],
    }


def _compact_sector_rankings(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "missing", "top": [], "bottom": []}
    top = value.get("top_sectors") or value.get("sectors") or []
    bottom = value.get("bottom_sectors") or []
    return {
        "status": value.get("status", "ok"),
        "top": _compact_sector_items(top),
        "bottom": _compact_sector_items(bottom),
        "errors": _as_text_list(value.get("errors"))[:3],
    }


def _compact_sector_items(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in items[:6]:
        if not isinstance(item, dict):
            continue
        result.append({
            "name": item.get("name") or item.get("sector_name"),
            "change_pct": item.get("change_pct") or item.get("涨跌幅"),
            "main_net_inflow": item.get("main_net_inflow") or item.get("main_amount"),
            "rank": item.get("rank"),
        })
    return result


def _deep_dive_prompt_evidence(
    *,
    ctx: SelectionRunContext,
    code: str,
    stock_name: str,
    detailed_evidence: Dict[str, Any],
    evidence_cards: List[Any],
) -> Dict[str, Any]:
    card_view = _evidence_cards_prompt_view(evidence_cards)
    return {
        "stock": {"code": code, "name": stock_name, "market": ctx.market},
        "evidence_cards": card_view,
        "dimension_summary": _dimension_summary_from_cards(card_view),
        "tool_failures": _tool_failures_from_evidence(detailed_evidence),
        "raw_refs": [card.get("raw_ref") for card in card_view if card.get("raw_ref")],
        "protocol": {
            "name": "EvidenceCard",
            "version": "v1",
            "raw_policy": "raw tool JSON is retained only by raw_ref/full artifacts; prompts receive compact cards.",
        },
    }


def _evidence_cards_prompt_view(cards: List[Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for card in cards:
        if hasattr(card, "model_dump"):
            item = card.model_dump(mode="json")
        elif isinstance(card, dict):
            item = card
        else:
            continue
        data_quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
        impact = item.get("impact") if isinstance(item.get("impact"), dict) else {}
        result.append({
            "card_id": item.get("card_id"),
            "dimension": item.get("dimension"),
            "data_quality": {
                "status": data_quality.get("status"),
                "as_of": data_quality.get("as_of"),
                "freshness": data_quality.get("freshness"),
                "source": data_quality.get("source"),
                "warnings": _as_text_list(data_quality.get("warnings"))[:4],
                "missing_fields": _as_text_list(data_quality.get("missing_fields"))[:6],
            },
            "impact": {
                "stance": impact.get("stance"),
                "action_bias": impact.get("action_bias"),
                "confidence": impact.get("confidence"),
                "score_delta": impact.get("score_delta"),
                "reason": _truncate(str(impact.get("reason") or ""), 320),
            },
            "signals": [
                {
                    "name": signal.get("name"),
                    "value": signal.get("value"),
                    "unit": signal.get("unit"),
                    "direction": signal.get("direction"),
                    "strength": signal.get("strength"),
                    "score_delta": signal.get("score_delta"),
                    "interpretation": _truncate(str(signal.get("interpretation") or ""), 260),
                }
                for signal in (item.get("signals") or [])[:4]
                if isinstance(signal, dict)
            ],
            "counter_evidence": [
                {
                    "refuted_claim": counter.get("refuted_claim"),
                    "refutation": _truncate(str(counter.get("refutation") or ""), 260),
                    "severity": counter.get("severity"),
                }
                for counter in (item.get("counter_evidence") or [])[:3]
                if isinstance(counter, dict)
            ],
            "expiry": item.get("expiry"),
            "raw_ref": item.get("raw_ref"),
        })
    return result


def _dimension_summary_from_cards(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for card in cards:
        dimension = str(card.get("dimension") or "unknown")
        impact = card.get("impact") if isinstance(card.get("impact"), dict) else {}
        quality = card.get("data_quality") if isinstance(card.get("data_quality"), dict) else {}
        summary[dimension] = {
            "stance": impact.get("stance"),
            "action_bias": impact.get("action_bias"),
            "confidence": impact.get("confidence"),
            "score_delta": impact.get("score_delta"),
            "data_status": quality.get("status"),
            "freshness": quality.get("freshness"),
            "missing_fields": quality.get("missing_fields") or [],
        }
    return summary


def _tool_failures_from_evidence(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    if not isinstance(evidence, dict):
        return failures
    for tool_name, raw in evidence.items():
        if not isinstance(raw, dict) or not _is_failed_tool_result(raw):
            continue
        failures.append({
            "tool": tool_name,
            "status": raw.get("status") or ("timeout" if raw.get("timeout") else "failed"),
            "errors": _as_text_list(raw.get("errors") or raw.get("error") or raw.get("error_summary"))[:4],
        })
    return failures


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
    seed_candidates = seed_result.get("candidates") if isinstance(seed_result, dict) else []
    if isinstance(seed_candidates, list):
        llm_candidates = full.get("candidates")
        if isinstance(llm_candidates, list) and llm_candidates:
            full["llm_candidate_summary"] = llm_candidates
        full["candidates"] = seed_candidates
        payload["candidate_count"] = len(seed_candidates)
        summary = payload.setdefault("summary", {})
        summary["candidate_codes"] = [
            str(item.get("code")) for item in seed_candidates if isinstance(item, dict) and item.get("code")
        ]
        summary["candidate_source"] = seed_result.get("candidate_source") or summary.get("candidate_source")
        summary["source_count"] = len(seed_candidates)
        for key in ("expert_packets", "quality", "hard_exclusion", "capacity", "themes", "discovery_steps", "candidate_pool_run_id"):
            if key in seed_result and key not in full:
                full[key] = seed_result.get(key)


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


def _stabilize_judge_decision(ctx: SelectionRunContext, payload: Dict[str, Any]) -> None:
    """Normalize over-aggressive judge outputs for non-empty candidate pools."""
    if not isinstance(payload, dict):
        return
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        payload["summary"] = summary
    full = payload.get("full")
    if not isinstance(full, dict):
        full = {}
        payload["full"] = full

    candidates = ctx.stage_full("candidate_discovery").get("candidates") or []
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    discovery_status = str(ctx.stages.get("candidate_discovery", SelectionStage()).status or "")
    action = str(summary.get("final_action") or "").strip().lower()
    next_step = str(summary.get("next_step") or "").strip()
    fallback_path = full.get("fallback_path") if isinstance(full.get("fallback_path"), dict) else {}

    if action != "reject" or candidate_count <= 0 or discovery_status == "insufficient_candidates":
        return

    # A non-empty candidate pool with weak evidence should remain an actionable
    # watchlist, not be rendered as a failed stock-picking run.
    summary["final_action"] = "wait"
    if summary.get("primary_plan_verdict") == "reject":
        summary["primary_plan_verdict"] = "wait_for_more_data"
    original_reason = str(summary.get("decision_summary") or "").strip()
    guard_note = "候选池已形成但证据质量不足，系统将“拒绝建仓”降级为“等待确认”，保留候选观察和后续复查条件。"
    summary["decision_summary"] = f"{original_reason}；{guard_note}" if original_reason else guard_note
    summary["next_step"] = next_step if next_step and next_step != "stop_no_trade" else "render_final_report"
    required_changes = full.get("required_plan_changes")
    if not isinstance(required_changes, list):
        required_changes = []
    if guard_note not in required_changes:
        required_changes.append(guard_note)
    full["required_plan_changes"] = required_changes
    if not isinstance(fallback_path, dict):
        fallback_path = {}
    fallback_path.update({
        "when": "wait_for_more_data",
        "next_step": "render_final_report",
        "reason": "保留候选池，等待技术回踩、资金流和基本面/消息证据补齐后复查。",
    })
    full["fallback_path"] = fallback_path


def _apply_judge_position_overrides(ctx: SelectionRunContext, payload: Dict[str, Any]) -> None:
    """Apply explicit Judge action changes back to the portfolio table."""
    if not isinstance(payload, dict):
        return
    allocation_stage = ctx.stages.get("portfolio_allocation")
    if allocation_stage is None or not isinstance(allocation_stage.full, dict):
        return

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    full = payload.get("full") if isinstance(payload.get("full"), dict) else {}
    positions = allocation_stage.full.get("positions_plan")
    if not isinstance(positions, list) or not positions:
        return

    override_text = _judge_override_text(summary, full)
    monitor_codes = _monitor_override_codes(override_text)
    final_action = str(summary.get("final_action") or "").strip().lower()
    apply_all = final_action == "monitor" and not monitor_codes
    if not monitor_codes and not apply_all:
        return

    changed = False
    for item in positions:
        if not isinstance(item, dict):
            continue
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if apply_all or code in monitor_codes:
            _downgrade_position_to_monitor(item, reason="Judge 裁决要求降为仅监控，不保留具体买入触发。")
            changed = True

    if not changed:
        return

    allocation_stage.summary["portfolio_action"] = "monitor"
    allocation_stage.summary["recommended_position_count"] = 0
    allocation_stage.summary["initial_total_position_pct"] = 0
    allocation_stage.summary["reserved_cash_pct"] = 100
    core_reason = str(allocation_stage.summary.get("core_reason") or "").strip()
    override_reason = "Judge 已把部分或全部候选降为仅监控，组合层取消开仓计划。"
    allocation_stage.summary["core_reason"] = f"{core_reason}；{override_reason}" if core_reason else override_reason

    brief = allocation_stage.summary.get("positions_plan_brief")
    if isinstance(brief, list):
        for item in brief:
            if isinstance(item, dict):
                code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
                if apply_all or code in monitor_codes:
                    item["action"] = "monitor"
                    item["initial_position_pct"] = 0

    risk_controls = allocation_stage.full.get("risk_controls")
    if not isinstance(risk_controls, list):
        risk_controls = []
    if override_reason not in risk_controls:
        risk_controls.append(override_reason)
    allocation_stage.full["risk_controls"] = risk_controls


def _judge_override_text(summary: Dict[str, Any], full: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in ("decision_summary", "final_action", "primary_plan_verdict"):
        if summary.get(key):
            values.append(str(summary[key]))
    for key in (
        "required_plan_changes",
        "accepted_arguments",
        "rejected_arguments",
        "risk_controls",
        "fallback_path",
    ):
        value = full.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item)
        elif isinstance(value, dict):
            values.extend(str(item) for item in value.values() if item)
        elif value:
            values.append(str(value))
    return "\n".join(values)


def _monitor_override_codes(text: str) -> List[str]:
    codes: List[str] = []
    patterns = (
        r"将\s*(\d{6})[^。；;\n]*?(?:降为|降级为|改为|调整为)[^。；;\n]*?(?:仅监控|monitor)",
        r"(\d{6})[^。；;\n]*?(?:降为|降级为|改为|调整为)[^。；;\n]*?(?:仅监控|monitor)",
        r"(\d{6})[^。；;\n]*?(?:仅监控|monitor)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            code = _normalize_stock_identity_code(match.group(1))
            if code and code not in codes:
                codes.append(code)
    return codes


def _downgrade_position_to_monitor(item: Dict[str, Any], *, reason: str) -> None:
    item["action"] = "monitor"
    item["action_strength"] = "none"
    item["initial_position_pct"] = 0
    item["initial_amount"] = 0
    item["entry_condition"] = "仅监控，不设置买入触发；等待基本面、资金或消息证据补齐后重新评估。"
    item["add_condition"] = "-"
    item["stop_loss_condition"] = "未建仓，不设置交易止损；若继续走弱则移出观察。"
    item["review_trigger"] = item.get("review_trigger") or "下一轮候选池刷新或关键公告/资金变化后复查"
    original_reason = str(item.get("reason") or "").strip()
    item["reason"] = f"{original_reason}；{reason}" if original_reason else reason
    risk_flags = item.get("risk_flags")
    if not isinstance(risk_flags, list):
        risk_flags = []
    if reason not in risk_flags:
        risk_flags.append(reason)
    item["risk_flags"] = risk_flags


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


def _selection_deep_dive_limit() -> int:
    try:
        config = Config.get_instance()
        raw = getattr(config, "agent_selection_deep_dive_limit", DEFAULT_DEEP_DIVE_LIMIT)
    except Exception:
        raw = os.getenv("AGENT_SELECTION_DEEP_DIVE_LIMIT", str(DEFAULT_DEEP_DIVE_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_DEEP_DIVE_LIMIT
    return max(1, min(20, value))


def _expand_deep_dive_targets_for_rich_report(
    *,
    deep_targets: List[str],
    candidates: List[str],
    screening_full: Dict[str, Any],
) -> List[str]:
    """Keep reports useful by deep-diving enough non-rejected candidates."""
    targets = [_normalize_stock_identity_code(item) for item in deep_targets if item]
    targets = [item for item in targets if item]
    if len(candidates) < MIN_RICH_REPORT_DEEP_DIVE_TARGETS or len(targets) >= MIN_RICH_REPORT_DEEP_DIVE_TARGETS:
        return list(dict.fromkeys(targets))

    rejected_codes = _screening_rejected_codes(screening_full)
    for code in candidates:
        normalized = _normalize_stock_identity_code(code)
        if not normalized or normalized in rejected_codes or normalized in targets:
            continue
        targets.append(normalized)
        if len(targets) >= MIN_RICH_REPORT_DEEP_DIVE_TARGETS:
            break
    return list(dict.fromkeys(targets))


def _screening_rejected_codes(screening_full: Dict[str, Any]) -> set[str]:
    rejected: set[str] = set()
    shortlist = screening_full.get("shortlist") if isinstance(screening_full, dict) else []
    if not isinstance(shortlist, list):
        return rejected
    for item in shortlist:
        if not isinstance(item, dict):
            continue
        result = str(item.get("screening_result") or "").strip().lower()
        if result == "reject":
            code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
            if code:
                rejected.add(code)
    return rejected


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


def _report_missing_evidence(report: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for stage_name in ("candidate_discovery", "candidate_screening", "single_stock_deep_dive", "portfolio_allocation", "adversarial_review"):
        stage = report.get(stage_name)
        full = stage.get("full") if isinstance(stage, dict) and isinstance(stage.get("full"), dict) else {}
        stage_missing = full.get("missing_evidence")
        if isinstance(stage_missing, list):
            missing.extend(str(item) for item in stage_missing if item)
        results = full.get("results")
        if isinstance(results, list):
            for item in results:
                item_full = item.get("full") if isinstance(item, dict) and isinstance(item.get("full"), dict) else {}
                item_missing = item_full.get("missing_evidence")
                if isinstance(item_missing, list):
                    missing.extend(str(value) for value in item_missing if value)
        summary = stage.get("summary") if isinstance(stage, dict) and isinstance(stage.get("summary"), dict) else {}
        summary_missing = summary.get("top_evidence_gaps")
        if isinstance(summary_missing, list):
            missing.extend(str(item) for item in summary_missing if item)
    return list(dict.fromkeys(missing))


def _primary_report_core_reason(
    *,
    allocation: Dict[str, Any],
    deep_results: List[Any],
    judge: Dict[str, Any],
) -> str:
    """Keep the final report centered on evidence and allocation, not debate prose."""
    allocation_reason = str(allocation.get("core_reason") or "").strip()
    if allocation_reason:
        return _brief_markdown_text(allocation_reason, 260)

    stock_reasons: List[str] = []
    for result in deep_results[:3]:
        if not isinstance(result, dict):
            continue
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        full = result.get("full") if isinstance(result.get("full"), dict) else {}
        stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
        label = _stock_label({**stock, **summary})
        reason = (
            summary.get("key_reason")
            or summary.get("reason")
            or full.get("reason")
            or _first_text(summary.get("main_supporting_evidence"))
            or _first_text(full.get("key_evidence"))
            or _dimension_reason(full.get("dimension_summary"))
        )
        if reason:
            stock_reasons.append(f"{label}: {reason}")
    if stock_reasons:
        return _brief_markdown_text("；".join(stock_reasons), 260)

    judge_summary = str(judge.get("decision_summary") or "").strip()
    if judge_summary:
        return _brief_markdown_text(judge_summary, 180)
    return "-"


def _auxiliary_review_note(judge: Dict[str, Any]) -> str:
    verdict = str(judge.get("primary_plan_verdict") or "").strip()
    action = str(judge.get("final_action") or "").strip()
    if verdict and action:
        return f"{verdict}，最终动作 {action}；详见第六节辅助审查摘要。"
    if verdict:
        return f"{verdict}；详见第六节辅助审查摘要。"
    if action:
        return f"最终动作 {action}；详见第六节辅助审查摘要。"
    return "-"


def _recommendation_items(
    *,
    positions: List[Any],
    deep_results: List[Any],
    candidates: List[Any],
    llm_candidate_by_code: Dict[str, Any],
) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for result in deep_results:
        if not isinstance(result, dict):
            continue
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        full = result.get("full") if isinstance(result.get("full"), dict) else {}
        stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
        entry_quality = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        code = _normalize_stock_identity_code(summary.get("code") or stock.get("code"))
        if not code:
            continue
        by_code[code] = {
            "code": code,
            "name": summary.get("name") or stock.get("name"),
            "action": summary.get("action_bias") or full.get("action_bias") or "wait",
            "action_strength": summary.get("action_strength") or full.get("action_strength") or "weak",
            "has_deep_dive": True,
            "quote_basis": summary.get("quote_basis") or full.get("quote_basis"),
            "ideal_entry_zone": summary.get("ideal_entry_zone") or entry_quality.get("ideal_entry_zone"),
            "secondary_entry_zone": summary.get("secondary_entry_zone") or entry_quality.get("secondary_entry_zone"),
            "no_chase_line": summary.get("no_chase_line") or entry_quality.get("no_chase_line"),
            "stop_loss": summary.get("stop_loss") or entry_quality.get("stop_loss"),
            "target_1": summary.get("target_1") or entry_quality.get("target_1"),
            "target_2": summary.get("target_2") or entry_quality.get("target_2"),
            "supporting_evidence": _as_text_list(full.get("key_evidence") or summary.get("main_supporting_evidence")),
            "risk_flags": _as_text_list(full.get("risk_flags") or summary.get("main_risks")),
            "failure_conditions": _as_text_list(full.get("failure_conditions")),
            "missing_evidence": _as_text_list(full.get("missing_evidence") or summary.get("main_missing_evidence")),
            "reason": (
                summary.get("key_reason")
                or summary.get("reason")
                or full.get("reason")
                or _first_text(summary.get("main_supporting_evidence"))
                or _dimension_reason(full.get("dimension_summary"))
            ),
        }

    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if not code:
            continue
        display_item = dict(llm_candidate_by_code.get(code) or {})
        display_item.update(item)
        target = by_code.setdefault(code, {"code": code, "name": display_item.get("name"), "action": "watch", "action_strength": "weak"})
        target.setdefault("name", display_item.get("name"))
        target["has_candidate"] = True
        target["candidate_score"] = display_item.get("final_score") or display_item.get("signal_score") or display_item.get("score")
        target["candidate_reason"] = _candidate_reason(display_item)
        target["candidate_source"] = _candidate_source_label(display_item)
        target["candidate_labels"] = _candidate_labels(display_item)

    for idx, plan in enumerate(positions):
        if not isinstance(plan, dict):
            continue
        code = _normalize_stock_identity_code(plan.get("code") or plan.get("stock_code"))
        if not code:
            continue
        target = by_code.setdefault(code, {"code": code, "name": plan.get("name")})
        target.update({
            "rank": plan.get("rank") or idx + 1,
            "name": plan.get("name") or target.get("name"),
            "has_plan": True,
            "action": plan.get("action") or target.get("action") or "wait",
            "action_strength": plan.get("action_strength") or target.get("action_strength") or "weak",
            "initial_position_pct": plan.get("initial_position_pct"),
            "initial_amount": plan.get("initial_amount"),
            "entry_condition": plan.get("entry_condition"),
            "add_condition": plan.get("add_condition"),
            "stop_loss_condition": plan.get("stop_loss_condition"),
            "take_profit_condition": plan.get("take_profit_condition"),
            "review_trigger": plan.get("review_trigger"),
            "plan_reason": plan.get("reason"),
        })

    items = list(by_code.values())
    items.sort(key=lambda item: (
        _recommendation_sort_rank(item),
        int(item.get("rank") or 999),
        -_safe_float(item.get("candidate_score")),
        str(item.get("code") or ""),
    ))
    return items


def _action_rank(value: Any) -> int:
    action = str(value or "").strip().lower()
    if action in {"open", "buy"}:
        return 0
    if action in {"wait", "monitor", "watch"}:
        return 1
    if action == "hold":
        return 2
    if action in {"reject", "avoid"}:
        return 3
    return 4


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _recommendation_sort_rank(item: Dict[str, Any]) -> int:
    if _is_actionable_recommendation(item):
        return _action_rank(item.get("action"))
    if _is_observable_item(item):
        return 10 + _action_rank(item.get("action"))
    return 99


def _is_actionable_recommendation(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid", "monitor", "watch"}:
        return False
    if action in {"open", "buy"}:
        return bool(item.get("has_plan") or item.get("has_deep_dive"))
    if action != "wait":
        return False
    if not (item.get("has_plan") and item.get("has_deep_dive")):
        return False
    if str(item.get("action_strength") or "").strip().lower() in {"", "none", "weak"}:
        return False
    if not (item.get("ideal_entry_zone") or item.get("entry_condition")):
        return False
    if not (item.get("stop_loss") or item.get("stop_loss_condition")):
        return False
    risks = " ".join(_as_text_list(item.get("risk_flags"))[:4])
    if any(marker in risks for marker in ("强势空头", "资金流出", "净流出", "数据过期", "严重过期", "基本面缺失")):
        return False
    return True


def _is_observable_item(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid"}:
        return False
    return bool(item.get("has_candidate") or item.get("has_deep_dive") or item.get("has_plan"))


def _preferred_candidate_label(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "暂无可入手标的"
    return _stock_label(items[0])


def _watch_candidate_labels(items: List[Dict[str, Any]]) -> str:
    labels = [_stock_label(item) for item in items[:4]]
    return "、".join(labels) if labels else "-"


def _observation_status(item: Dict[str, Any]) -> str:
    action = str(item.get("action") or "watch").strip().lower()
    strength = str(item.get("action_strength") or "").strip().lower()
    if item.get("has_plan") or item.get("has_deep_dive"):
        if action == "wait":
            return f"等待确认{f' / {strength}' if strength else ''}"
        if action in {"monitor", "watch"}:
            return "仅跟踪"
    return "候选观察"


def _observation_reason(item: Dict[str, Any]) -> str:
    for key in ("plan_reason", "reason", "candidate_reason"):
        value = item.get(key)
        if value:
            return _brief_markdown_text(_translate_report_text(value), 120)
    labels = _as_text_list(item.get("candidate_labels"))
    if labels:
        return f"入池关注点：{'、'.join(labels[:4])}"
    return "候选池召回，等待深度证据验证。"


def _observation_risk(item: Dict[str, Any]) -> str:
    risks = _as_text_list(item.get("risk_flags")) or _as_text_list(item.get("missing_evidence")) or _as_text_list(item.get("failure_conditions"))
    if risks:
        return _brief_markdown_text("；".join(risks[:2]), 140)
    if not item.get("has_deep_dive"):
        return f"未进入本轮逐股深度分析；当前深挖上限为 {_selection_deep_dive_limit()} 只，仍需补技术、资金、消息和基本面证据。"
    return "等待价格、资金或消息进一步确认。"


def _render_recommendation_block(rank: int, item: Dict[str, Any]) -> List[str]:
    medal = {1: "🥇 首选", 2: "🥈 次选", 3: "🥉 观察"}.get(rank, f"第 {rank} 顺位")
    score = item.get("candidate_score")
    title_suffix = f"（{_format_score(score)}分）" if score is not None else ""
    lines = [
        f"### {medal}：{_markdown_cell(_stock_label(item))}{title_suffix}",
        "",
        "| 项目 | 决策 |",
        "| --- | --- |",
        f"| 入场结论 | {_markdown_cell(item.get('action') or 'wait')} |",
        f"| 动作强度 | {_markdown_cell(item.get('action_strength') or '-')} |",
        f"| 行情口径 | {_markdown_cell(_quote_basis_label(item.get('quote_basis')))} |",
        f"| 理想入场区间 | {_markdown_cell(item.get('ideal_entry_zone') or item.get('entry_condition') or '等待回踩或突破确认')} |",
        f"| 次优入场区间 | {_markdown_cell(item.get('secondary_entry_zone') or '-')} |",
        f"| 禁止追高线 | {_markdown_cell(item.get('no_chase_line') or '-')} |",
        f"| 首仓比例 | {_markdown_cell(_format_pct(item.get('initial_position_pct')))} |",
        f"| 加仓条件 | {_markdown_cell(item.get('add_condition') or '-')} |",
        f"| 止损位 | {_markdown_cell(item.get('stop_loss') or item.get('stop_loss_condition') or '-')} |",
        f"| 第一目标位 | {_markdown_cell(item.get('target_1') or item.get('take_profit_condition') or '-')} |",
        f"| 第二目标位 | {_markdown_cell(item.get('target_2') or '-')} |",
        f"| 复查触发 | {_markdown_cell(item.get('review_trigger') or '-')} |",
        "",
    ]
    reasons = _recommendation_reasons(item)
    if reasons:
        lines.append("入选理由：")
        lines.extend([f"- {_markdown_cell(reason)}" for reason in reasons[:5]])
        lines.append("")
    risks = _as_text_list(item.get("risk_flags")) or _as_text_list(item.get("failure_conditions")) or _as_text_list(item.get("missing_evidence"))
    if risks:
        lines.append(f"风险：{_markdown_cell('；'.join(risks[:3]))}")
        lines.append("")
    return lines


def _recommendation_reasons(item: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    for value in _as_text_list(item.get("supporting_evidence")):
        reasons.append(_translate_report_text(value))
    for key in ("reason", "candidate_reason", "plan_reason"):
        value = item.get(key)
        if value:
            reasons.append(_translate_report_text(value))
    labels = _as_text_list(item.get("candidate_labels"))
    if labels:
        reasons.append(f"入池关注点：{'、'.join(labels[:5])}")
    return list(dict.fromkeys(reason for reason in reasons if reason))[:6]


def _format_score(value: Any) -> str:
    try:
        return f"{float(value):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _quote_basis_label(value: Any) -> str:
    raw = str(value or "").strip()
    labels = {
        "intraday": "盘中实时数据",
        "latest_trading_day": "最近有效交易日",
        "eod": "收盘后数据",
    }
    return labels.get(raw, raw or "-")


def _evidence_summary_rows(
    report: Dict[str, Any],
    candidates: List[Any],
    deep_results: List[Any],
) -> List[Dict[str, str]]:
    regime = report.get("market_regime") if isinstance(report.get("market_regime"), dict) else {}
    discovery = report.get("candidate_discovery", {})
    rows = [
        {
            "name": "市场状态",
            "status": str(regime.get("status") or ("partial" if regime else "missing")),
            "result": _market_regime_summary_text(regime),
            "impact": "决定是否降低开仓和追高权重",
        },
        {
            "name": "候选发现",
            "status": str(discovery.get("status") or "partial"),
            "result": f"形成 {len(candidates)} 只候选，主要来源：{_candidate_source_mix(candidates)}",
            "impact": "提供可筛选对象，不等于买入结论",
        },
    ]
    dimension_hits = _summarize_deep_dimensions(deep_results)
    for name, key, impact in (
        ("技术面", "technical", "决定是否具备趋势和入场时机"),
        ("资金筹码", "capital_flow", "验证资金是否同步"),
        ("消息事件", "news_event", "验证是否有真实催化或利空"),
        ("基本面", "fundamental", "约束长期持有质量和估值风险"),
    ):
        item = dimension_hits.get(key) or {}
        rows.append({
            "name": name,
            "status": item.get("status") or "partial",
            "result": item.get("summary") or "本轮证据不足或未形成明确方向",
            "impact": impact,
        })
    return rows


def _market_regime_summary_text(regime: Dict[str, Any]) -> str:
    if not regime:
        return "市场状态未知"
    bits = [
        f"regime={regime.get('regime')}" if regime.get("regime") else "",
        f"风险={regime.get('risk_level')}" if regime.get("risk_level") else "",
        f"波动={regime.get('volatility_bucket')}" if regime.get("volatility_bucket") else "",
    ]
    hints = _as_text_list(regime.get("strategy_hints"))
    return "，".join(bit for bit in bits if bit) or (hints[0] if hints else "市场状态已识别")


def _candidate_source_mix(candidates: List[Any]) -> str:
    labels: List[str] = []
    for item in candidates:
        if isinstance(item, dict):
            labels.append(_candidate_source_label(item))
    labels = [label for label in labels if label and label != "-"]
    return "、".join(list(dict.fromkeys(labels))[:4]) if labels else "-"


def _candidate_appendix_items(
    candidates: List[Any],
    llm_candidate_by_code: Dict[str, Any],
) -> List[Dict[str, Any]]:
    display_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if not code:
            continue
        display_item = dict(llm_candidate_by_code.get(code) or {})
        display_item.update(item)
        display_item["_candidate_original_index"] = idx
        display_items.append(display_item)
    display_items.sort(
        key=lambda item: (
            -_candidate_display_score(item),
            int(item.get("_candidate_original_index") or 0),
            str(item.get("code") or item.get("stock_code") or ""),
        )
    )
    return display_items


def _candidate_display_score(item: Dict[str, Any]) -> float:
    return _safe_float(item.get("final_score") or item.get("signal_score") or item.get("score"))


def _deep_dive_code_set(deep_results: List[Any]) -> set[str]:
    codes: set[str] = set()
    for result in deep_results:
        if not isinstance(result, dict):
            continue
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        full = result.get("full") if isinstance(result.get("full"), dict) else {}
        stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
        code = _normalize_stock_identity_code(summary.get("code") or stock.get("code"))
        if code:
            codes.add(code)
    return codes


def _candidate_deep_dive_status(item: Dict[str, Any], deep_analyzed_codes: set[str]) -> str:
    code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
    if code and code in deep_analyzed_codes:
        return "已完成"
    return f"未覆盖（深挖上限 {_selection_deep_dive_limit()} 只）"


def _summarize_deep_dimensions(deep_results: List[Any]) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for item in deep_results:
        if not isinstance(item, dict):
            continue
        full = item.get("full") if isinstance(item.get("full"), dict) else {}
        dimension_summary = full.get("dimension_summary") if isinstance(full.get("dimension_summary"), dict) else {}
        for key, value in dimension_summary.items():
            if not isinstance(value, dict) or key in result:
                continue
            result[str(key)] = {
                "status": _verdict_label(value.get("verdict")),
                "summary": _brief_markdown_text(value.get("summary") or "-", 180),
            }
    return result


def _final_risk_lines(recommendations: List[Dict[str, Any]], adversarial: Dict[str, Any], all_missing: List[str]) -> List[str]:
    risks: List[str] = []
    for item in recommendations[:3]:
        stock = _stock_label(item)
        for risk in _as_text_list(item.get("risk_flags"))[:2]:
            risks.append(f"{stock}：{risk}")
        for failure in _as_text_list(item.get("failure_conditions"))[:1]:
            risks.append(f"{stock} 淘汰条件：{failure}")
    risks.extend(_as_text_list(adversarial.get("top_risk_points"))[:3])
    if all_missing:
        risks.append(f"待补关键证据：{'、'.join(all_missing[:2])}")
    return list(dict.fromkeys(_translate_report_text(item) for item in risks if item))[:8]


def _stock_label(item: Dict[str, Any]) -> str:
    code = str(item.get("code") or item.get("stock_code") or "-")
    name = str(item.get("name") or item.get("stock_name") or "").strip()
    return f"{code} {name}".strip()


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if item:
                return str(item)
        return ""
    return str(value or "")


REPORT_STRATEGY_LABELS = {
    "ma_volume": "均线放量突破",
    "turtle_trade": "海龟突破",
    "high_tight_flag": "高窄旗形",
    "limit_up_shakeout": "涨停洗盘",
    "uptrend_limit_down": "上升趋势跌停错杀",
    "rps_breakout": "RPS 强势突破",
    "volume_breakout": "放量突破",
    "capital_heat": "资金热度",
    "quality_value": "质量价值",
    "shrink_pullback": "缩量回踩",
    "balanced_alpha": "均衡 Alpha",
    "dual_low": "双低价值",
    "momentum_quality": "动量质量",
    "oversold_reversal": "超跌反转",
    "fundamental_quality": "基本面质量",
    "hot_sector": "强势板块",
    "breakout": "突破",
    "rps": "RPS 强势",
    "momentum": "动量",
    "relative_strength": "相对强势",
    "liquidity": "流动性",
    "ma_cross": "均线信号",
    "multi_factor": "多因子",
    "balanced": "均衡",
    "quality": "质量",
    "growth": "成长",
    "value": "估值",
    "news_momentum": "消息动量",
}

REPORT_DIMENSION_LABELS = {
    "strategy": "策略",
    "technical": "技术面",
    "capital": "资金面",
    "fundamental": "基本面",
    "message": "消息面",
    "sentiment": "情绪/热点",
    "sector": "板块主题",
    "fallback": "兜底观察",
}

REPORT_SOURCE_PREFIX_LABELS = (
    ("alphasift:", "AlphaSift 多因子"),
    ("sequoia:", "Sequoia 技术形态"),
    ("fundamental:", "基本面质量筛选"),
    ("news_momentum:", "公司消息/公告"),
    ("news_sentiment:", "新闻情绪"),
    ("event_impact:", "事件影响链"),
    ("akshare:industry:", "强势行业板块"),
    ("akshare:concept:", "强势概念板块"),
    ("fallback_seed_pool", "固定兜底观察池"),
    ("user_seed", "用户输入"),
)


def _candidate_source_label(item: Dict[str, Any]) -> str:
    sources = _candidate_source_values(item)
    labels = [_source_token_label(source) for source in sources]
    labels = [label for label in labels if label and label != "-"]
    return "、".join(list(dict.fromkeys(labels))[:3]) if labels else "-"


def _candidate_source_values(item: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for value in [item.get("source"), item.get("candidate_source")]:
        if value:
            values.append(str(value))
    for key in ("recall_sources", "evidence_refs"):
        raw = item.get(key)
        if isinstance(raw, list):
            values.extend(str(entry) for entry in raw if entry)
    return list(dict.fromkeys(values))


def _source_token_label(source: str) -> str:
    text = str(source or "").strip()
    if not text:
        return "-"
    if text == "alphasift:multi_strategy":
        return "AlphaSift 多策略共振"
    if text == "sequoia:multi_strategy":
        return "Sequoia 多策略共振"
    if text == "fundamental:quality_snapshot":
        return "基本面质量筛选"
    if text == "news_momentum:company_event":
        return "公司消息/公告"
    for prefix, label in REPORT_SOURCE_PREFIX_LABELS:
        if text == prefix or text.startswith(prefix):
            suffix = text[len(prefix):]
            if suffix == "multi_strategy":
                return f"{label}多策略共振"
            suffix_label = REPORT_STRATEGY_LABELS.get(suffix, suffix)
            return f"{label}：{suffix_label}" if suffix_label else label
    return REPORT_STRATEGY_LABELS.get(text, _humanize_token(text))


def _strategy_token_label(token: str) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    if ":" in text:
        return _source_token_label(text)
    return REPORT_STRATEGY_LABELS.get(text, _humanize_token(text))


def _humanize_token(token: str) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    if re.search(r"[A-Za-z_]", text):
        return text.replace("_", " ")
    return text


def _translate_report_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    replacements = {
        "AlphaSift YAML 多因子策略入池": "AlphaSift 多因子策略入池",
        "alphasift:multi_strategy": "AlphaSift 多策略共振",
        "sequoia:multi_strategy": "Sequoia 多策略共振",
        "news_momentum:company_event": "公司消息/公告",
        "fundamental:quality_snapshot": "基本面质量筛选",
        "fundamental_quality": "基本面质量",
        "llm_friendly": "",
    }
    for raw, label in {**REPORT_STRATEGY_LABELS, **replacements}.items():
        if label:
            text = text.replace(raw, label)
        else:
            text = text.replace(raw, "")
    text = re.sub(r"[、,，；;]\s*[、,，；;]+", "；", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ；;、,，")


def _candidate_labels(item: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for key in ("matched_strategies", "strategy_tags"):
        value = item.get(key)
        if isinstance(value, list):
            labels.extend(_strategy_token_label(str(entry)) for entry in value if entry)
    for source in _candidate_source_values(item):
        labels.append(_source_token_label(source))
    reason_dimensions = item.get("reason_dimensions")
    if isinstance(reason_dimensions, list):
        for entry in reason_dimensions:
            if isinstance(entry, dict):
                dimension = str(entry.get("dimension") or "")
                labels.append(str(entry.get("label") or REPORT_DIMENSION_LABELS.get(dimension) or dimension or ""))
    blocked = {"llm friendly"}
    return list(dict.fromkeys(label for label in labels if label and label not in blocked))


def _candidate_reason(item: Dict[str, Any]) -> str:
    reasons = []
    for key in ("reason", "entry_reason"):
        if item.get(key):
            reasons.append(str(item[key]))
    entry_reasons = item.get("entry_reasons")
    if isinstance(entry_reasons, list):
        reasons.extend(str(reason) for reason in entry_reasons if reason)
    reason_dimensions = item.get("reason_dimensions")
    if isinstance(reason_dimensions, list):
        for entry in reason_dimensions:
            if isinstance(entry, dict) and entry.get("detail"):
                reasons.append(str(entry["detail"]))
    unique = list(dict.fromkeys(_translate_report_text(reason).strip() for reason in reasons if reason and str(reason).strip()))
    concise: List[str] = []
    for reason in unique:
        if not reason:
            continue
        if reason in concise:
            continue
        if any(reason != existing and reason in existing for existing in concise):
            continue
        concise.append(reason)
    return "；".join(concise[:3]) if concise else "-"


def _dimension_reason(dimension_summary: Any) -> str:
    if not isinstance(dimension_summary, dict):
        return ""
    reasons: List[str] = []
    for key, item in dimension_summary.items():
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        if verdict in {"support", "weaken", "tool_failed", "missing"}:
            reasons.append(f"{_dimension_label(key)}：{summary}")
    if not reasons:
        for key, item in dimension_summary.items():
            if isinstance(item, dict) and item.get("summary"):
                reasons.append(f"{_dimension_label(key)}：{item['summary']}")
                break
    return "；".join(reasons[:2])


def _dimension_label(key: Any) -> str:
    labels = {
        "technical": "技术面",
        "price_structure": "价格结构",
        "fundamental": "基本面",
        "news_event": "消息面",
        "capital_flow": "资金面",
        "market_sector": "板块/市场",
        "account_fit": "账户适配",
    }
    return labels.get(str(key), str(key))


def _verdict_label(value: Any) -> str:
    labels = {
        "support": "支持",
        "weaken": "削弱",
        "neutral": "中性",
        "missing": "缺失",
        "tool_failed": "工具失败",
        "unknown": "未知",
    }
    return labels.get(str(value or ""), str(value or "-"))


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []


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


def _brief_markdown_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value if value is not None else "-").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"...[truncated {len(value) - max_chars} chars]"
