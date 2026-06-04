# -*- coding: utf-8 -*-
"""Staged stock-selection pipeline for planning_execute watchlist scans."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.agent.evidence import build_evidence_cards_for_stock
from src.agent.evidence.adapter import cards_to_json
from src.agent.candidate_experts_v2.seed_facts import compact_seed_fact_packets_for_model
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
    build_meta_orchestrator_prompt,
    build_portfolio_allocation_prompt,
    build_pricing_agent_prompt,
)
from src.agent.tools.registry import ToolRegistry
from src.config import Config
from src.schemas.agent_context import AgentUserContext

logger = logging.getLogger(__name__)


SELECTION_INTENT = "watchlist_scan"
DEFAULT_CANDIDATE_LIMIT = 8
DISCOVERY_RECALL_LIMIT = 20
DEFAULT_DEEP_DIVE_LIMIT = 8
PROMPT_STOCK_LIMIT = 5
PROMPT_SCENARIO_LIMIT = 3
PROMPT_CONSTRAINT_LIMIT = 4
MIN_RICH_REPORT_DEEP_DIVE_TARGETS = 3
FAILED_TOOL_STATUSES = {"failed", "error", "tool_failed", "timeout"}
CONDITIONAL_ENTRY_SCORE_MIN_DEFAULT = 88.0
STRONG_WATCH_SCORE_MIN_DEFAULT = 85.0
NO_CHASE_PCT_DEFAULT = 6.0
CONDITIONAL_ENTRY_MIN_STRENGTH_DEFAULT = "medium"
ACTION_STRENGTH_RANK = {"none": 0, "weak": 1, "medium": 2, "strong": 3}
HARD_BEARISH_RISK_MARKERS = (
    "*st",
    "st股",
    "st风险",
    "退市",
    "停牌",
    "不可成交",
    "名称代码不一致",
    "强势空头",
    "趋势空头",
    "跌破关键支撑",
    "跌破止损",
    "破位",
    "资金流出",
    "净流出",
    "卖出主导",
    "监管处罚",
    "业绩预警",
    "重大减持",
    "债务",
    "流动性风险",
    "重大诉讼",
    "数据过期",
    "严重过期",
    "行情严重过期",
    "关键价格缺失",
    "股票身份无法确认",
    "risk_off",
    "panic",
    "extreme",
)
OPPORTUNITY_HARD_EXCLUSION_MARKERS = (
    "*st",
    "st股",
    "st风险",
    "退市",
    "停牌",
    "不可成交",
    "名称代码不一致",
    "股票身份无法确认",
    "强势空头",
    "趋势空头",
    "跌破关键支撑",
    "跌破止损",
    "破位",
    "资金流出",
    "净流出",
    "卖出主导",
    "监管处罚",
    "业绩预警",
    "重大减持",
    "债务",
    "流动性风险",
    "重大诉讼",
    "数据过期",
    "严重过期",
    "行情严重过期",
)


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
    tool_call_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    tool_call_sequence: int = 0
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    market_regime: Dict[str, Any] = field(default_factory=dict)
    orchestration_mode: str = "legacy"
    candidate_discovery_mode: str = "deterministic"
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
            "candidate_discovery_mode": self.candidate_discovery_mode,
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
    appendix_markdown: str = ""
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
            "appendix_markdown": self.appendix_markdown,
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
    candidate_discovery_mode: Optional[str] = None,
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
        candidate_discovery_mode=_resolve_candidate_discovery_mode(candidate_discovery_mode),
        progress_callback=progress_callback,
    )
    result = StockSelectionResult(enabled=True, context=ctx)
    base_evidence: Dict[str, Any] = {}
    try:
        _emit(progress_callback, "selection_start", message="开始五阶段选股流水线。")

        if ctx.candidate_discovery_mode != "thesis_desk_committee":
            ctx.next_step = "stop_non_thesis_desk_mode"
            blocked_payload = _non_thesis_desk_mode_exit_payload(ctx)
            ctx.set_stage("candidate_discovery", blocked_payload)
            _emit(
                progress_callback,
                "selection_candidate_discovery_mode",
                payload={
                    "mode": ctx.candidate_discovery_mode,
                    "required_mode": "thesis_desk_committee",
                    "fallback": False,
                    "blocked": True,
                    "reason": "current development run only allows thesis_desk_committee",
                },
            )
            _emit(
                progress_callback,
                "selection_candidate_discovery_done",
                payload=ctx.stages["candidate_discovery"].to_dict(include_full=False),
            )
            result.success = True
            result.final_report_json = _build_final_report_json(ctx)
            _enforce_report_stock_identity(ctx, result.final_report_json)
            result.final_markdown = render_stock_selection_markdown(result.final_report_json)
            _apply_report_split(result)
            _finalize_result(result)
            return result

        discovery_seed = _run_candidate_discovery_tool(
            ctx=ctx,
            tool_registry=tool_registry,
            target_symbols=list(agent_user_context.report.target_symbols or []),
            llm_adapter=llm_adapter,
        )
        if ctx.candidate_discovery_mode in {"llm_expert_committee", "thesis_desk_committee"}:
            discovery_payload = discovery_seed
        else:
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
        if ctx.stages["candidate_discovery"].status == "failed":
            error = _candidate_discovery_failure_message(ctx.stage_full("candidate_discovery"))
            _complete_failed_candidate_discovery_result(ctx=ctx, result=result, error=error)
            _finalize_result(result)
            _emit(progress_callback, "selection_error", message=error, payload=result.final_report_json)
            return result

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
            _apply_report_split(result)
            _finalize_result(result)
            return result

        balanced_payload = _build_balanced_candidate_evidence_stage(
            ctx=ctx,
            tool_registry=tool_registry,
            discovery_full=ctx.stage_full("candidate_discovery"),
            canonical_codes=candidates,
        )
        _enforce_stage_stock_identity(ctx, balanced_payload, "balanced_candidate_evidence")
        ctx.set_stage("balanced_candidate_evidence", balanced_payload)
        _emit(
            progress_callback,
            "selection_balanced_candidate_evidence_done",
            payload=ctx.stages["balanced_candidate_evidence"].to_dict(include_full=False),
        )

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
                "balanced_candidate_evidence_summary": ctx.stage_summary("balanced_candidate_evidence"),
                "balanced_candidate_evidence_ref": "candidate_evidence.md",
                "candidate_evidence_table": _screening_candidate_evidence_table(ctx),
                "candidate_evidence_data": _screening_candidate_evidence_data(ctx),
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

        deep_dive_limit = _selection_deep_dive_limit()
        deep_targets, deep_dive_provenance = _select_deep_dive_targets(
            candidates=candidates,
            screening_summary=ctx.stage_summary("candidate_screening"),
            screening_full=ctx.stage_full("candidate_screening"),
            limit=deep_dive_limit,
        )
        setup_router_enabled = _deep_dive_setup_router_enabled()
        candidate_records = (
            _candidate_records_by_code(ctx.stage_full("candidate_discovery"))
            if setup_router_enabled
            else {}
        )
        deep_dive_outputs: List[Dict[str, Any]] = []
        for code in deep_targets:
            detailed_evidence = _balanced_raw_evidence_for_code(ctx, code)
            if detailed_evidence is None:
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
            deep_dive_payload_in: Dict[str, Any] = {
                "user_message": task,
                "stock_code": code,
                "stock_name": stock_name,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "screening_summary": ctx.stage_summary("candidate_screening"),
                "market_regime": _summarize_market_regime(ctx.market_regime),
                "evidence_ledger_summary": _compact_evidence_ledger_for_prompt(ctx),
                "stock_evidence": prompt_evidence,
            }
            if setup_router_enabled:
                deep_dive_payload_in.update(
                    _deep_dive_setup_fields(candidate_records.get(code), market=ctx.market)
                )
            deep_payload = _call_stage_json(
                ctx=ctx,
                llm_adapter=llm_adapter,
                stage_name=f"single_stock_deep_dive:{code}",
                prompt=build_deep_dive_prompt(deep_dive_payload_in),
                fallback=_fallback_deep_dive(code, stock_name, detailed_evidence),
                timeout_seconds=timeout_seconds,
            )
            _attach_evidence_cards(deep_payload, evidence_cards)
            if deep_dive_provenance.get(code) == "pool_fallback":
                _mark_deep_dive_pool_fallback(deep_payload)
            _enforce_stage_stock_identity(ctx, deep_payload, f"single_stock_deep_dive:{code}")
            deep_dive_outputs.append(deep_payload)

        deep_dive_stage = _combine_deep_dive_outputs(deep_dive_outputs)
        _enforce_stage_stock_identity(ctx, deep_dive_stage, "single_stock_deep_dive")
        ctx.set_stage("single_stock_deep_dive", deep_dive_stage)
        _emit(progress_callback, "selection_deep_dive_done", payload=ctx.stages["single_stock_deep_dive"].to_dict(include_full=False))

        meta_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="meta_orchestrator",
            prompt=build_meta_orchestrator_prompt(_build_meta_orchestrator_input(ctx)),
            fallback=_fallback_meta_orchestrator(ctx),
            timeout_seconds=timeout_seconds,
        )
        _enforce_stage_stock_identity(ctx, meta_payload, "meta_orchestrator")
        ctx.set_stage("meta_orchestrator", meta_payload)
        _emit(progress_callback, "selection_meta_orchestrator_done", payload=ctx.stages["meta_orchestrator"].to_dict(include_full=False))

        pricing_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="pricing_agent",
            prompt=build_pricing_agent_prompt(_build_pricing_agent_input(ctx)),
            fallback=_fallback_pricing_agent(ctx),
            timeout_seconds=timeout_seconds,
        )
        _enforce_stage_stock_identity(ctx, pricing_payload, "pricing_agent")
        ctx.set_stage("pricing_agent", pricing_payload)
        _emit(progress_callback, "selection_pricing_agent_done", payload=ctx.stages["pricing_agent"].to_dict(include_full=False))

        allocation_payload = _call_stage_json(
            ctx=ctx,
            llm_adapter=llm_adapter,
            stage_name="portfolio_allocation",
            prompt=build_portfolio_allocation_prompt({
                "user_message": task,
                "account_summary": ctx.account_summary,
                "investor_profile": ctx.investor_profile,
                "deep_dive_results_summary": ctx.stage_summary("single_stock_deep_dive"),
                "meta_orchestrator_summary": ctx.stage_summary("meta_orchestrator"),
                "meta_constraint_packages": _compact_meta_packages_for_prompt(ctx),
                "pricing_agent_summary": ctx.stage_summary("pricing_agent"),
                "if_then_order_matrix": _compact_pricing_matrix_for_prompt(ctx),
                "balanced_candidate_evidence_summary": ctx.stage_summary("balanced_candidate_evidence"),
                "balanced_candidate_evidence_ref": "candidate_evidence.md",
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
                "balanced_candidate_evidence_summary": ctx.stage_summary("balanced_candidate_evidence"),
                "balanced_candidate_evidence_ref": "candidate_evidence.md",
                "screening_summary": ctx.stage_summary("candidate_screening"),
                "deep_dive_results_summary": ctx.stage_summary("single_stock_deep_dive"),
                "meta_orchestrator_summary": ctx.stage_summary("meta_orchestrator"),
                "meta_constraint_packages": _compact_meta_packages_for_prompt(ctx),
                "pricing_agent_summary": ctx.stage_summary("pricing_agent"),
                "if_then_order_matrix": _compact_pricing_matrix_for_prompt(ctx),
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
                "meta_orchestrator_summary": ctx.stage_summary("meta_orchestrator"),
                "meta_constraint_packages": _compact_meta_packages_for_prompt(ctx),
                "pricing_agent_summary": ctx.stage_summary("pricing_agent"),
                "if_then_order_matrix": _compact_pricing_matrix_for_prompt(ctx),
                "opposing_review_summary": ctx.stage_summary("adversarial_review"),
                "balanced_candidate_evidence_summary": ctx.stage_summary("balanced_candidate_evidence"),
                "balanced_candidate_evidence_ref": "candidate_evidence.md",
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
        _apply_report_split(result)
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
    meta_summary = report.get("meta_orchestrator", {}).get("summary", {})
    pricing_summary = report.get("pricing_agent", {}).get("summary", {})
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
    deep_dive_order = [
        _normalize_stock_identity_code(
            ((item.get("summary") if isinstance(item, dict) and isinstance(item.get("summary"), dict) else {}) or {}).get("code")
            or ((item.get("full") if isinstance(item, dict) and isinstance(item.get("full"), dict) else {}).get("stock") or {}).get("code")
        )
        for item in deep_results
    ]
    deep_dive_order = [code for code in deep_dive_order if code]
    recommendation_by_code = {
        _normalize_stock_identity_code(item.get("code")): item
        for item in recommendations
        if isinstance(item, dict) and _normalize_stock_identity_code(item.get("code"))
    }
    recommended_items = [recommendation_by_code[code] for code in deep_dive_order if code in recommendation_by_code]
    displayed_recommendations = recommended_items[:5]
    headline_opportunity_items = _headline_opportunity_items(displayed_recommendations=displayed_recommendations)
    execution_recommendations = [
        item
        for item in displayed_recommendations
        if _execution_mode(item) in {"immediate_open", "conditional_open"}
    ]
    headline_watch_items = _headline_watch_items(
        displayed_recommendations=displayed_recommendations,
        primary_items=execution_recommendations or headline_opportunity_items,
    )
    observation_items = [
        item for item in recommendations
        if not item.get("has_deep_dive") and _execution_mode(item) not in {"immediate_open", "conditional_open"} and _is_observable_item(item)
    ]

    lines = [
        "# 选股分析报告：下周可关注候选",
        "",
        "## 一、核心推荐结论",
        "",
        "| 项目 | 结论 |",
        "| --- | --- |",
        f"| 最终动作 | {judge.get('final_action') or allocation.get('portfolio_action') or '-'} |",
        f"| 裁决 | {judge.get('primary_plan_verdict') or '-'} |",
        f"| 机会首选 | {_markdown_cell(_opportunity_candidate_label(headline_opportunity_items))} |",
        f"| 执行首选 | {_markdown_cell(_execution_candidate_label(execution_recommendations))} |",
        f"| 可观察标的 | {_markdown_cell(_watch_candidate_labels(headline_watch_items))} |",
        f"| 核心原因 | {_markdown_cell(core_reason)} |",
        f"| 最大约束 | {_markdown_cell(allocation.get('main_constraint') or '-')} |",
        f"| 候选池规模 | {_markdown_cell(discovery_summary.get('source_count') or discovery.get('candidate_count') or candidate_quality.get('candidate_count') or len(candidates))} 只 |",
        "",
    ]

    lines.extend(_render_meta_agent_chain_section(report, recommendations=displayed_recommendations))

    lines.extend([
        "## 三、推荐排序与入场决策" if execution_recommendations else "## 三、深挖结果与等待/排除决策",
        "",
    ])

    if displayed_recommendations:
        if not execution_recommendations:
            lines.extend([
                "本轮深挖对象已完成取证，但当前没有形成可执行开仓计划。以下内容用于展示观察、等待或排除结论，不代表立即买入排序。",
                "",
            ])
        for idx, item in enumerate(displayed_recommendations, start=1):
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
        "## 四、Execute 证据摘要",
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
        "## 五、关键风险与等待确认",
        "",
    ])
    risk_lines = _final_risk_lines(recommendations, adversarial, all_missing)
    if risk_lines:
        lines.extend([f"- {_markdown_cell(item)}" for item in risk_lines])
    else:
        lines.append("- 当前没有结构化风险条目，但仍需按价格、仓位和数据时效执行风控。")

    lines.extend([
        "",
        "## 六、组合配置表",
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
        "## 七、辅助审查摘要",
        "",
        "反方审查和 Judge 裁决只作为主方案的校验层，完整原始输出保留在 Trace artifact 中。",
        "",
        "| 审查项 | 摘要 |",
        "| --- | --- |",
        f"| 辅助审查 | {_markdown_cell(review_note)} |",
        f"| Meta 场景约束 | {_markdown_cell(_brief_markdown_text(_meta_report_note(meta_summary), 180))} |",
        f"| 点位计算条件单 | {_markdown_cell(_brief_markdown_text(_pricing_report_note(pricing_summary), 180))} |",
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
        "<!-- APPENDIX_SEPARATOR -->",
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


def _render_meta_agent_chain_section(report: Dict[str, Any], *, recommendations: List[Dict[str, Any]]) -> List[str]:
    """Render the three-desk -> Meta -> point-calculation handoff without extra tool calls."""

    meta_full = report.get("meta_orchestrator", {}).get("full", {})
    pricing_full = report.get("pricing_agent", {}).get("full", {})
    packages = meta_full.get("packages") if isinstance(meta_full, dict) and isinstance(meta_full.get("packages"), list) else []
    matrix = (
        pricing_full.get("if_then_order_matrix")
        if isinstance(pricing_full, dict) and isinstance(pricing_full.get("if_then_order_matrix"), list)
        else []
    )
    desk_by_code = _desk_opinions_by_code(report)
    package_by_code = {
        _normalize_stock_identity_code((item.get("stock") or {}).get("code")): item
        for item in packages
        if isinstance(item, dict) and isinstance(item.get("stock"), dict)
    }
    pricing_by_code = {
        _normalize_stock_identity_code(item.get("code")): item
        for item in matrix
        if isinstance(item, dict) and _normalize_stock_identity_code(item.get("code"))
    }
    recommendation_by_code = {
        _normalize_stock_identity_code(item.get("code")): item
        for item in recommendations
        if isinstance(item, dict) and _normalize_stock_identity_code(item.get("code"))
    }
    sorted_pricing_codes = [
        _normalize_stock_identity_code(item.get("code"))
        for item in sorted(matrix, key=_point_calc_item_rank, reverse=True)
        if isinstance(item, dict)
    ]
    codes: List[str] = list(recommendation_by_code)
    if not codes:
        for source in (sorted_pricing_codes, list(package_by_code), list(desk_by_code)):
            for code in source:
                if code and code not in codes:
                    codes.append(code)
                if len(codes) >= 5:
                    break
            if len(codes) >= 5:
                break

    lines = [
        "## 二、Meta-Agent 链路对齐（非推荐排序）",
        "",
        "本节只解释每只深挖标的如何从三席位传到 Meta-Agent 和点位计算层，不代表另一套推荐排序。字段缺失会直接标为缺失，不用隐含假设补齐。",
        "",
        "### 字段说明",
        "",
        "| 字段 | 在报告中的作用 |",
        "| --- | --- |",
        "| 三席位意见 | 展示低位启动、动量、质量修复三个打法对同一股票的支持、观察、反对或拒绝。 |",
        "| `meta_analysis.factual_consensus` | Meta-Agent 从三席位和深挖结果里抽出的事实共识，只陈述事实。 |",
        "| `meta_analysis.strategic_divergence` | 三席位之间的主观分歧，说明为什么不能把单一席位意见直接当买入结论。 |",
        "| `asset_regime` | Meta-Agent 给股票打的机会类型标签，用来决定后续点位计算层要算哪类剧本。 |",
        "| `hard_constraints_for_pricing_agent` | 传给点位计算层的硬约束包，包括失效位、均值回归锚点、禁追高和风险边界。 |",
        "| `required_pricing_scenarios` | Meta-Agent 强制点位计算层必算的顺势、衰竭、回归等场景。 |",
        "| `if_then_order_matrix` | 点位计算层按硬约束生成的 If-Then 条件单矩阵，Judge 必须按它降级或采纳。 |",
        "",
    ]

    if not codes:
        lines.extend([
            "### 链路状态",
            "",
            "- 缺失：本轮没有形成 Meta-Agent 包或点位计算条件单，报告不能给出入场区间、止盈止损。",
            "",
        ])
        return lines

    for idx, code in enumerate(codes, start=1):
        package = package_by_code.get(code) or {}
        stock = package.get("stock") if isinstance(package.get("stock"), dict) else {}
        pricing = pricing_by_code.get(code) or {}
        rec = recommendation_by_code.get(code) or {}
        name = stock.get("name") or pricing.get("name") or rec.get("name") or ""
        lines.extend([
            f"### 链路对齐：{_markdown_cell(f'{code} {name}'.strip())}",
            "",
            "#### 三席位意见",
            "",
            "| 席位 | 输出意见 | 处理结果 | 核心理由 |",
            "| --- | --- | --- | --- |",
        ])
        desk_rows = desk_by_code.get(code) or []
        if desk_rows:
            for row in desk_rows:
                lines.append(
                    "| {desk} | {stance} | {status} | {reason} |".format(
                        desk=_markdown_cell(row.get("desk") or "-"),
                        stance=_markdown_cell(row.get("stance") or "-"),
                        status=_markdown_cell(row.get("status") or "-"),
                        reason=_markdown_cell(_brief_markdown_text(row.get("reason") or "-", 180)),
                    )
                )
        else:
            lines.append("| - | 缺失 | 缺失 | 本轮 candidate_discovery 未落盘 thesis_desk_packets。 |")

        meta = package.get("meta_analysis") if isinstance(package.get("meta_analysis"), dict) else {}
        market = package.get("market_context") if isinstance(package.get("market_context"), dict) else {}
        lines.extend([
            "",
            "#### Meta-Agent 约束包",
            "",
            "| 项目 | 内容 |",
            "| --- | --- |",
            f"| 资产定性 | {_markdown_cell(meta.get('asset_regime') or pricing.get('asset_regime') or '缺失：asset_regime 未返回')} |",
            f"| 事实共识 | {_markdown_cell(_join_limited(meta.get('factual_consensus'), 3, '缺失：factual_consensus 未返回'))} |",
            f"| 策略分歧 | {_markdown_cell(meta.get('strategic_divergence') or '缺失：strategic_divergence 未返回')} |",
            f"| 市场环境 | {_markdown_cell(_market_context_text(market))} |",
        ])
        constraints = package.get("hard_constraints_for_pricing_agent") if isinstance(package.get("hard_constraints_for_pricing_agent"), dict) else {}
        lines.extend(_render_meta_constraints_rows(constraints))

        scenarios = package.get("required_pricing_scenarios") if isinstance(package.get("required_pricing_scenarios"), list) else []
        if scenarios:
            lines.extend([
                "",
                "Meta 必算场景：",
            ])
            for scenario in scenarios[:5]:
                if not isinstance(scenario, dict):
                    continue
                lines.append(
                    f"- {_markdown_cell(scenario.get('scenario_name') or 'Unnamed')}: "
                    f"{_markdown_cell(scenario.get('condition') or '缺失 condition')}；"
                    f"{_markdown_cell(scenario.get('required_output') or '缺失 required_output')}"
                )
        else:
            lines.extend(["", "- 缺失：Meta-Agent 未返回 required_pricing_scenarios。"])

        lines.extend([
            "",
            "#### 点位计算 If-Then 条件单",
            "",
            "| 场景 | 条件 | 动作 | 入场区间 | 止盈目标 | 止损/失效 | 备注 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        pricing_scenarios = pricing.get("scenarios") if isinstance(pricing.get("scenarios"), list) else []
        if pricing_scenarios:
            for scenario in pricing_scenarios[:5]:
                if not isinstance(scenario, dict):
                    continue
                lines.append(
                    "| {name} | {condition} | {action} | {entry} | {target} | {stop} | {comment} |".format(
                        name=_markdown_cell(scenario.get("scenario_name") or "-"),
                        condition=_markdown_cell(scenario.get("condition") or "-"),
                        action=_markdown_cell(scenario.get("action") or "-"),
                        entry=_markdown_cell(_pricing_entry_text(scenario, rec)),
                        target=_markdown_cell(_pricing_take_profit_text(scenario, rec)),
                        stop=_markdown_cell(_pricing_stop_text(scenario, rec)),
                        comment=_markdown_cell(_brief_markdown_text(scenario.get("risk_reward_comment") or "-", 160)),
                    )
                )
        else:
            lines.append("| - | 缺失 | - | 缺失：点位计算层未返回 scenarios | 缺失 | 缺失 | 不生成条件单。 |")
        warnings = _as_text_list(pricing.get("pricing_warnings"))
        if warnings:
            lines.extend(["", "点位计算警告："])
            lines.extend([f"- {_markdown_cell(_brief_markdown_text(item, 180))}" for item in warnings[:4]])
        lines.append("")
    return lines


def _desk_opinions_by_code(report: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    discovery_full = report.get("candidate_discovery", {}).get("full", {})
    packets = (
        discovery_full.get("thesis_desk_packets")
        if isinstance(discovery_full, dict) and isinstance(discovery_full.get("thesis_desk_packets"), list)
        else []
    )
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        desk = str(packet.get("expert") or "-")
        for bucket, status_label in (("candidates", "入选"), ("rejected", "反对/拒绝")):
            rows = packet.get(bucket) if isinstance(packet.get(bucket), list) else []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
                if not code:
                    continue
                by_code.setdefault(code, []).append({
                    "desk": desk,
                    "stance": item.get("stance") or "-",
                    "status": status_label,
                    "reason": item.get("reason") or _first_text(item.get("risks")) or "-",
                })
    return by_code


def _join_limited(value: Any, limit: int, missing_text: str) -> str:
    items = _as_text_list(value)
    return "；".join(items[:limit]) if items else missing_text


def _market_context_text(market: Dict[str, Any]) -> str:
    if not isinstance(market, dict) or not market:
        return "缺失：market_context 未返回"
    parts = [
        f"regime={market.get('market_regime')}" if market.get("market_regime") else "",
        f"volatility={market.get('volatility_bucket')}" if market.get("volatility_bucket") else "",
        f"risk={market.get('risk_level')}" if market.get("risk_level") else "",
        str(market.get("regime_weight_adjustment") or ""),
    ]
    text = "；".join(part for part in parts if part)
    warnings = _as_text_list(market.get("market_context_warnings"))
    if warnings:
        text = f"{text}；{'；'.join(warnings[:2])}" if text else "；".join(warnings[:2])
    return text or "缺失：market_context 未返回可读字段"


def _render_meta_constraints_rows(constraints: Dict[str, Any]) -> List[str]:
    if not isinstance(constraints, dict) or not constraints:
        return ["| 硬约束 | 缺失：hard_constraints_for_pricing_agent 未返回 |"]
    rows: List[str] = []
    invalidation = constraints.get("invalidation_level") if isinstance(constraints.get("invalidation_level"), dict) else {}
    anchor = constraints.get("mean_reversion_anchor") if isinstance(constraints.get("mean_reversion_anchor"), dict) else {}
    premium = constraints.get("max_chase_premium") if isinstance(constraints.get("max_chase_premium"), dict) else constraints.get("max_chase_premium")
    rows.append(f"| 失效位 | {_markdown_cell(_constraint_text(invalidation, '缺失：invalidation_level 未返回'))} |")
    rows.append(f"| 均值回归锚点 | {_markdown_cell(_constraint_text(anchor, '缺失：mean_reversion_anchor 未返回'))} |")
    rows.append(f"| 禁追高 | {_markdown_cell(_premium_constraint_text(premium))} |")
    risk_constraints = constraints.get("risk_constraints") if isinstance(constraints.get("risk_constraints"), list) else []
    rows.append(f"| 风险边界 | {_markdown_cell(_join_limited(risk_constraints, 4, '缺失：risk_constraints 未返回'))} |")
    return rows


def _constraint_text(value: Dict[str, Any], missing_text: str) -> str:
    if not isinstance(value, dict) or not value:
        return missing_text
    bits = []
    if value.get("price") not in (None, ""):
        bits.append(f"price={value.get('price')}")
    if value.get("value") not in (None, ""):
        bits.append(f"value={value.get('value')}")
    if value.get("reason"):
        bits.append(str(value.get("reason")))
    if value.get("source"):
        bits.append(f"source={value.get('source')}")
    return "；".join(bits) if bits else missing_text


def _premium_constraint_text(value: Any) -> str:
    if isinstance(value, dict):
        return _constraint_text(value, "缺失：max_chase_premium 未返回")
    if value not in (None, ""):
        return str(value)
    return "缺失：max_chase_premium 未返回"


def _pricing_entry_text(scenario: Dict[str, Any], rec: Dict[str, Any]) -> str:
    for key in ("entry_zone", "entry_condition"):
        if _has_text_value(scenario.get(key)):
            return str(scenario.get(key))
    action = str(scenario.get("action") or "").strip().lower()
    if action in {"monitor", "plain_wait", "wait"}:
        return "不入场，仅监控触发条件"
    for key in ("entry_condition", "ideal_entry_zone", "pullback_trigger", "breakout_trigger"):
        if _has_text_value(rec.get(key)):
            return str(rec.get(key))
    return "缺失：未给出可执行入场区间"


def _pricing_take_profit_text(scenario: Dict[str, Any], rec: Dict[str, Any]) -> str:
    for key in ("take_profit", "take_profit_condition", "target", "target_zone"):
        if _has_text_value(scenario.get(key)):
            return str(scenario.get(key))
    action = str(scenario.get("action") or "").strip().lower()
    if action in {"monitor", "plain_wait"}:
        return "不适用：该场景不生成开仓单"
    if _has_text_value(rec.get("take_profit_condition")):
        return str(rec.get("take_profit_condition"))
    targets = [str(rec.get(key)) for key in ("target_1", "target_2", "take_profit_condition") if _has_text_value(rec.get(key))]
    return " / ".join(targets) if targets else "缺失：点位计算/深挖未给出止盈目标"


def _pricing_stop_text(scenario: Dict[str, Any], rec: Dict[str, Any]) -> str:
    values = []
    for key in ("stop_loss", "failure_condition"):
        if _has_text_value(scenario.get(key)):
            values.append(str(scenario.get(key)))
    if not values:
        for key in ("stop_loss_condition", "stop_loss", "failure_condition"):
            if _has_text_value(rec.get(key)):
                values.append(str(rec.get(key)))
    text = "；".join(dict.fromkeys(values))
    return _brief_markdown_text(text, 260) if text else "缺失：未给出止损/失效条件"


APPENDIX_SEPARATOR = "<!-- APPENDIX_SEPARATOR -->"


def _split_report_appendix(full_markdown: str) -> tuple:
    """Split full markdown into (main_report, appendix) on the separator marker."""
    if APPENDIX_SEPARATOR in full_markdown:
        parts = full_markdown.split(APPENDIX_SEPARATOR, 1)
        return parts[0].rstrip(), parts[1].strip()
    return full_markdown, ""


def _apply_report_split(result: StockSelectionResult) -> None:
    main, appendix = _split_report_appendix(result.final_markdown)
    result.final_markdown = main
    result.appendix_markdown = appendix


def _resolve_orchestration_mode(value: Optional[str]) -> str:
    raw = (value or os.getenv("AGENT_ORCHESTRATION_MODE") or "legacy").strip().lower()
    return raw if raw in {"legacy", "expert_graph"} else "legacy"


def _resolve_candidate_discovery_mode(value: Optional[str]) -> str:
    """Resolve candidate-discovery mode: request value > env > default thesis desk.

    Current development runs focus on the P4 three-desk path; legacy modes are
    accepted for traceability but are stopped by a guard before candidate work.
    """
    raw = (value or os.getenv("AGENT_CANDIDATE_DISCOVERY_MODE") or "thesis_desk_committee").strip().lower()
    if raw in {"deterministic", "llm_expert_committee", "thesis_desk_committee"}:
        return raw
    logger.warning(
        "Invalid candidate_discovery_mode=%r; falling back to 'thesis_desk_committee'",
        value,
    )
    return "thesis_desk_committee"


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
    _apply_report_split(result)


def _candidate_discovery_failure_message(discovery_full: Dict[str, Any]) -> str:
    """Build a visible fail-fast error for thesis-desk candidate discovery."""

    base = str(discovery_full.get("error") or "").strip()
    errors: List[str] = []
    for packet in discovery_full.get("thesis_desk_packets") or []:
        if not isinstance(packet, dict):
            continue
        expert = str(packet.get("expert") or "").strip()
        for err in _as_text_list(packet.get("errors")):
            errors.append(f"{expert}: {err}" if expert else err)
    for step in discovery_full.get("discovery_steps") or []:
        if isinstance(step, dict) and step.get("error"):
            errors.append(str(step.get("error")))
    message_parts = [part for part in [base, "; ".join(dict.fromkeys(errors))] if part]
    detail = "；".join(message_parts) if message_parts else "三席位候选发现失败，未形成 L1 候选。"
    return f"三席位候选发现失败，已终止后续筛选/深挖：{_truncate(detail, 360)}"


def _complete_failed_candidate_discovery_result(
    *,
    ctx: SelectionRunContext,
    result: StockSelectionResult,
    error: str,
) -> None:
    """Fail fast when thesis desks do not produce candidates."""

    ctx.next_step = "stop_candidate_discovery_failed"
    ctx.set_stage("judge_decision", {
        "stage": "judge_decision",
        "status": "failed",
        "summary": {
            "primary_plan_verdict": "reject",
            "final_action": "wait",
            "decision_summary": error,
            "next_step": ctx.next_step,
        },
        "full": {
            "winner": "risk_control",
            "accepted_arguments": [],
            "rejected_arguments": ["三席位未形成 L1 候选时继续深挖或生成买入结论"],
            "required_plan_changes": ["修复三席位失败原因后重新运行"],
            "risk_controls": ["本轮不进入筛选、深挖、组合配置"],
            "failure": {"error": error, "fallback": False},
        },
        "full_ref": "judge_decision.json",
    })
    result.success = False
    result.error = error
    result.final_report_json = _build_final_report_json(ctx)
    result.final_report_json["partial_failure"] = {
        "status": "failed_candidate_discovery",
        "error": error,
        "next_step": ctx.next_step,
        "fallback": False,
    }
    _enforce_report_stock_identity(ctx, result.final_report_json)
    result.final_markdown = render_stock_selection_markdown(result.final_report_json)
    _apply_report_split(result)


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
    if "meta_orchestrator" not in ctx.stages:
        ctx.set_stage("meta_orchestrator", _fallback_meta_orchestrator(ctx))
    if "pricing_agent" not in ctx.stages:
        ctx.set_stage("pricing_agent", _fallback_pricing_agent(ctx))
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
    raw_text = ""
    parsed: Dict[str, Any] = {}
    stage_error: Optional[str] = None
    try:
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
        raw_text = response.content or ""
        parsed = try_parse_json(raw_text) or {}
    except StopIteration as exc:
        stage_error = f"llm_stage_call_failed: {exc}"
        parsed = {}
    if not parsed:
        parsed = dict(fallback)
        parsed.setdefault("full", {})
        parsed["full"].setdefault("tool_failures", [])
        parsed["full"]["tool_failures"].append({
            "stage": stage_name,
            "error": stage_error or "llm_json_parse_failed",
            "raw": _truncate(raw_text, 1000),
        })
    else:
        parsed_stage = str(parsed.get("stage") or "")
        expected_stage = stage_name.split(":", 1)[0]
        if parsed_stage and parsed_stage != stage_name and parsed_stage != expected_stage:
            original = parsed
            parsed = dict(fallback)
            parsed.setdefault("full", {})
            parsed["full"].setdefault("tool_failures", [])
            parsed["full"]["tool_failures"].append({
                "stage": stage_name,
                "error": f"llm_stage_mismatch:{parsed_stage}",
                "raw": _truncate(json.dumps(original, ensure_ascii=False, default=str), 1000),
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
    if str(normalized.get("stage") or "").startswith("single_stock_deep_dive"):
        normalized = _normalize_deep_dive_payload(normalized, fallback=fallback)
    return normalized


def _normalize_deep_dive_payload(payload: Dict[str, Any], *, fallback: Dict[str, Any]) -> Dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    full = payload.get("full") if isinstance(payload.get("full"), dict) else {}
    fallback_summary = fallback.get("summary") if isinstance(fallback.get("summary"), dict) else {}
    fallback_full = fallback.get("full") if isinstance(fallback.get("full"), dict) else {}
    entry_quality = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
    fallback_entry_quality = fallback_full.get("entry_quality") if isinstance(fallback_full.get("entry_quality"), dict) else {}

    def _pick(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return ""

    normalized_summary = dict(summary)
    normalized_full = dict(full)
    normalized_entry_quality = dict(fallback_entry_quality)
    normalized_entry_quality.update(entry_quality)

    normalized_summary["ideal_entry_zone"] = _pick(
        summary.get("ideal_entry_zone"),
        entry_quality.get("ideal_entry_zone"),
        fallback_summary.get("ideal_entry_zone"),
        fallback_entry_quality.get("ideal_entry_zone"),
    )
    normalized_summary["no_chase_line"] = _pick(
        summary.get("no_chase_line"),
        entry_quality.get("no_chase_line"),
        fallback_summary.get("no_chase_line"),
        fallback_entry_quality.get("no_chase_line"),
    )
    normalized_summary["stop_loss"] = _pick(
        summary.get("stop_loss"),
        entry_quality.get("stop_loss"),
        fallback_summary.get("stop_loss"),
        fallback_entry_quality.get("stop_loss"),
    )
    normalized_summary["target_1"] = _pick(
        summary.get("target_1"),
        entry_quality.get("target_1"),
        fallback_entry_quality.get("target_1"),
    )
    normalized_summary["target_2"] = _pick(
        summary.get("target_2"),
        entry_quality.get("target_2"),
        fallback_entry_quality.get("target_2"),
    )
    normalized_summary["secondary_entry_zone"] = _pick(
        summary.get("secondary_entry_zone"),
        entry_quality.get("secondary_entry_zone"),
        fallback_entry_quality.get("secondary_entry_zone"),
    )
    normalized_summary["auction_trigger"] = _pick(
        summary.get("auction_trigger"),
        entry_quality.get("auction_trigger"),
        fallback_entry_quality.get("auction_trigger"),
    )
    normalized_summary["breakout_trigger"] = _pick(
        summary.get("breakout_trigger"),
        entry_quality.get("breakout_trigger"),
        fallback_entry_quality.get("breakout_trigger"),
    )
    normalized_summary["pullback_trigger"] = _pick(
        summary.get("pullback_trigger"),
        entry_quality.get("pullback_trigger"),
        fallback_entry_quality.get("pullback_trigger"),
    )
    normalized_summary["failure_condition"] = _pick(
        summary.get("failure_condition"),
        entry_quality.get("failure_condition"),
        fallback_entry_quality.get("failure_condition"),
    )

    normalized_entry_quality["ideal_entry_zone"] = normalized_summary["ideal_entry_zone"]
    normalized_entry_quality["secondary_entry_zone"] = normalized_summary["secondary_entry_zone"]
    normalized_entry_quality["auction_trigger"] = normalized_summary["auction_trigger"]
    normalized_entry_quality["breakout_trigger"] = normalized_summary["breakout_trigger"]
    normalized_entry_quality["pullback_trigger"] = normalized_summary["pullback_trigger"]
    normalized_entry_quality["no_chase_line"] = normalized_summary["no_chase_line"]
    normalized_entry_quality["stop_loss"] = normalized_summary["stop_loss"]
    normalized_entry_quality["failure_condition"] = normalized_summary["failure_condition"]
    normalized_entry_quality["target_1"] = normalized_summary["target_1"]
    normalized_entry_quality["target_2"] = normalized_summary["target_2"]

    quote_snapshot = _normalize_quote_snapshot(full.get("stock"), fallback_full.get("stock"))
    _apply_quote_snapshot_to_deep_dive(
        normalized_summary,
        normalized_full,
        normalized_entry_quality,
        quote_snapshot,
    )

    normalized_full["entry_quality"] = normalized_entry_quality
    payload["summary"] = normalized_summary
    payload["full"] = normalized_full
    return payload


def _normalize_quote_snapshot(*values: Any) -> Dict[str, Any]:
    for value in values:
        if not isinstance(value, dict):
            continue
        snapshot = {
            "price": value.get("price") or value.get("current_price"),
            "change_pct": value.get("change_pct") or value.get("pct_chg"),
            "quote_trade_date": value.get("quote_trade_date") or value.get("latest_date") or value.get("date"),
            "price_label": value.get("price_label"),
            "change_pct_label": value.get("change_pct_label"),
            "freshness_note": value.get("freshness_note"),
            "market_session": value.get("market_session"),
        }
        if any(snapshot.values()):
            return snapshot
    return {}


def _apply_quote_snapshot_to_deep_dive(
    summary: Dict[str, Any],
    full: Dict[str, Any],
    entry_quality: Dict[str, Any],
    quote_snapshot: Dict[str, Any],
) -> None:
    if not quote_snapshot:
        return

    price = quote_snapshot.get("price")
    quote_trade_date = quote_snapshot.get("quote_trade_date")
    price_label = str(quote_snapshot.get("price_label") or "").strip()
    change_pct_label = str(quote_snapshot.get("change_pct_label") or "").strip()
    freshness_note = str(quote_snapshot.get("freshness_note") or "").strip()
    market_session = str(quote_snapshot.get("market_session") or "").strip()

    summary["quote_basis"] = _quote_basis_from_snapshot(quote_snapshot, current=summary.get("quote_basis"))

    stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
    stock["price"] = price
    stock["quote_trade_date"] = quote_trade_date
    stock["price_label"] = price_label or stock.get("price_label")
    stock["change_pct_label"] = change_pct_label or stock.get("change_pct_label")
    stock["freshness_note"] = freshness_note or stock.get("freshness_note")
    stock["market_session"] = market_session or stock.get("market_session")
    full["stock"] = stock

    evidence_prefix = _quote_fact_evidence(quote_snapshot)
    if evidence_prefix:
        summary["main_supporting_evidence"] = _prepend_unique_text(
            summary.get("main_supporting_evidence"),
            evidence_prefix,
        )
        full["key_evidence"] = _prepend_unique_text(full.get("key_evidence"), evidence_prefix)

    if freshness_note:
        summary["main_supporting_evidence"] = _append_unique_text(
            summary.get("main_supporting_evidence"),
            freshness_note,
        )
        full["key_evidence"] = _append_unique_text(full.get("key_evidence"), freshness_note)

    if summary["quote_basis"] != "intraday":
        mismatch_markers = ("盘中", "今日", "实时")
        if any(marker in freshness_note for marker in ("休市", "最近可用", "尚未开盘")):
            summary["main_risks"] = _filter_text_list(summary.get("main_risks"), mismatch_markers)
            full["risk_flags"] = _filter_text_list(full.get("risk_flags"), mismatch_markers)

    if summary.get("action_bias") in {"wait", "monitor", "reject"} and price is not None:
        price_text = f"{price:g}" if isinstance(price, (int, float)) else str(price)
        if summary.get("action_bias") == "reject":
            if not entry_quality.get("no_chase_line"):
                entry_quality["no_chase_line"] = f"当前价位（约{price_text}）为禁止追高区域"
        elif not entry_quality.get("no_chase_line"):
            entry_quality["no_chase_line"] = _default_no_chase_condition()


def _quote_basis_from_snapshot(quote_snapshot: Dict[str, Any], *, current: Any = None) -> str:
    session = str(quote_snapshot.get("market_session") or "").strip()
    if session == "open":
        return "intraday"
    if session == "post_close":
        return "after_close"
    if session in {"closed_non_trading_day", "pre_open"}:
        return "latest_trading_day"
    current_text = str(current or "").strip().lower()
    return current_text if current_text else "unknown"


def _quote_fact_evidence(quote_snapshot: Dict[str, Any]) -> str:
    price = quote_snapshot.get("price")
    quote_trade_date = quote_snapshot.get("quote_trade_date")
    price_label = str(quote_snapshot.get("price_label") or "当前价").strip()
    change_pct = quote_snapshot.get("change_pct")
    change_pct_label = str(quote_snapshot.get("change_pct_label") or "").strip()
    parts: List[str] = []
    if price is not None:
        price_text = f"{price:g}" if isinstance(price, (int, float)) else str(price)
        if quote_trade_date:
            parts.append(f"{price_label}={price_text}（截至{quote_trade_date}）")
        else:
            parts.append(f"{price_label}={price_text}")
    if change_pct is not None and change_pct_label:
        if isinstance(change_pct, (int, float)):
            parts.append(f"{change_pct_label}={change_pct:.2f}%")
        else:
            parts.append(f"{change_pct_label}={change_pct}")
    return "；".join(parts)


def _prepend_unique_text(values: Any, text: str) -> List[str]:
    items = _as_text_list(values)
    normalized = text.strip()
    if not normalized:
        return items
    filtered = [item for item in items if item.strip() != normalized]
    return [normalized, *filtered]


def _append_unique_text(values: Any, text: str) -> List[str]:
    items = _as_text_list(values)
    normalized = text.strip()
    if not normalized:
        return items
    filtered = [item for item in items if item.strip() != normalized]
    return [*filtered, normalized]


def _filter_text_list(values: Any, markers: tuple[str, ...]) -> List[str]:
    items = _as_text_list(values)
    return [
        item for item in items
        if not any(marker in item for marker in markers)
    ]


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
    llm_adapter: Any = None,
) -> Dict[str, Any]:
    args = {
        "market": ctx.market,
        "seed_symbols": target_symbols,
        "limit": DISCOVERY_RECALL_LIMIT,
    }
    mode = ctx.candidate_discovery_mode or "deterministic"
    _emit(
        ctx.progress_callback,
        "selection_candidate_discovery_mode",
        payload={"mode": mode, "fallback": False},
    )
    if mode != "thesis_desk_committee":
        return _non_thesis_desk_mode_exit_payload(ctx)
    if mode == "thesis_desk_committee":
        try:
            from src.agent.candidate_experts_v2.committee import run_committee_discovery
            from datetime import datetime

            if llm_adapter is None:
                llm_adapter = getattr(tool_registry, "llm_adapter", None)
            # Wrap LLMToolAdapter into the LLMCallable signature expected by committee
            committee_llm: Any = llm_adapter
            try:
                per_desk_llm_timeout = float(
                    getattr(
                        Config.get_instance(),
                        "agent_candidate_expert_timeout_seconds",
                        60.0,
                    )
                )
            except Exception:
                per_desk_llm_timeout = 60.0
            call_with_tools_fn = getattr(llm_adapter, "call_with_tools", None)
            if call_with_tools_fn is not None and not callable(llm_adapter):
                from src.agent.candidate_experts_v2.experts.base import LLMToolCall, LLMTurn

                def _committee_llm_callable(messages, tool_decls_inner):
                    response_format = (
                        {"type": "json_object"}
                        if any(str(msg.get("role") or "") == "tool" for msg in messages or [])
                        else None
                    )
                    resp = call_with_tools_fn(
                        messages,
                        tool_decls_inner,
                        timeout=per_desk_llm_timeout,
                        response_format=response_format,
                    )
                    tc_list = []
                    for tc in (getattr(resp, "tool_calls", None) or []):
                        tc_list.append(
                            LLMToolCall(
                                name=str(getattr(tc, "name", "") or ""),
                                arguments=dict(getattr(tc, "arguments", {}) or {}),
                                call_id=str(getattr(tc, "id", "") or ""),
                            )
                        )
                    content = str(getattr(resp, "content", "") or "")
                    if getattr(resp, "provider", "") == "error" and not tc_list:
                        raise RuntimeError(content or "LLM provider returned error")
                    return LLMTurn(tool_calls=tc_list, text=content)

                committee_llm = _committee_llm_callable
            tool_decls = []
            list_decls = getattr(tool_registry, "list_decls", None)
            if callable(list_decls):
                try:
                    tool_decls = list_decls() or []
                except Exception:
                    tool_decls = []
            if not tool_decls:
                to_openai_tools = getattr(tool_registry, "to_openai_tools", None)
                if callable(to_openai_tools):
                    try:
                        tool_decls = to_openai_tools() or []
                    except Exception:
                        tool_decls = []
            # Build a plain dict[str, callable] from ToolRegistry for BaseExpert
            # (BaseExpert expects tool_registry.get(name) to return a callable)
            committee_tool_registry: Any = tool_registry
            list_names_fn = getattr(tool_registry, "list_names", None)
            execute_fn = getattr(tool_registry, "execute", None)
            if callable(list_names_fn) and callable(execute_fn):
                try:
                    committee_tool_registry = {
                        name: (lambda _n: lambda **kw: execute_fn(_n, **kw))(name)
                        for name in list_names_fn()
                    }
                except Exception:
                    pass
            from src.agent.candidate_experts_v2.committee import _build_seed_pool_result
            today_str = datetime.now().strftime("%Y%m%d")
            try:
                seed_pool_total_limit = int(
                    getattr(Config.get_instance(), "agent_seed_pool_total_limit", 20) or 20
                )
            except Exception:
                seed_pool_total_limit = 20
            seed_pool_result = _build_seed_pool_result(
                market=ctx.market,
                seed_symbols=target_symbols,
                tool_registry=tool_registry,  # original ToolRegistry with .execute()
                today=today_str,
                total_limit=max(1, min(40, seed_pool_total_limit)),
            )
            _emit(
                ctx.progress_callback,
                "selection_seed_pool_built",
                payload=_compact_seed_pool_build_result(seed_pool_result),
            )
            seed_count_for_timeout = max(1, len(getattr(seed_pool_result, "seeds", []) or []))
            committee_timeout_s = max(
                300.0,
                min(
                    3600.0,
                    per_desk_llm_timeout * seed_count_for_timeout * 3,
                ),
            )
            payload = run_committee_discovery(
                market=ctx.market,
                seed_symbols=target_symbols,
                limit=DISCOVERY_RECALL_LIMIT,
                tool_registry=committee_tool_registry,
                llm_adapter=committee_llm,
                today=today_str,
                tool_decls=tool_decls,
                seed_pool_result=seed_pool_result,
                overall_timeout_s=committee_timeout_s,
                seed_fact_tools=getattr(Config.get_instance(), "agent_seed_fact_tools", None),
                seed_fact_max_workers=int(
                    getattr(Config.get_instance(), "agent_seed_fact_max_workers", 12) or 12
                ),
                seed_fact_tool_timeout_seconds=float(
                    getattr(Config.get_instance(), "agent_seed_fact_tool_timeout_seconds", 12.0) or 12.0
                ),
            )
            if not isinstance(payload, dict):
                raise RuntimeError(f"committee discovery returned {type(payload).__name__}")
            seed_fact_summary = payload.get("seed_fact_summary")
            if isinstance(seed_fact_summary, dict):
                _emit(
                    ctx.progress_callback,
                    "selection_seed_facts",
                    payload={
                        **seed_fact_summary,
                        "packets": payload.get("seed_fact_packets") if isinstance(payload.get("seed_fact_packets"), list) else [],
                    },
                )
            _emit(
                ctx.progress_callback,
                "selection_seed_gate_done",
                payload=_compact_seed_gate_result(payload),
            )
            return payload
        except Exception as exc:
            logger.warning(
                "committee discovery failed: %s; treating as failure",
                exc,
            )
            _emit(
                ctx.progress_callback,
                "selection_candidate_discovery_mode",
                payload={
                    "mode": "thesis_desk_committee",
                    "fallback": False,
                    "requested_mode": "llm_expert_committee",
                    "error": str(exc),
                },
            )
            return {
                "status": "failed",
                "market": ctx.market,
                "candidates": [],
                "candidate_count": 0,
                "candidate_source": mode,
                "discovery_steps": [
                    {
                        "source": mode,
                        "status": "failed",
                        "dimension": "committee",
                        "error": str(exc),
                    }
                ],
                "next_required_tools": [],
                "error": str(exc),
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


BALANCED_CANDIDATE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("strategy", "策略候选"),
    ("news", "消息候选"),
    ("capital", "资金候选"),
    ("fundamental", "基本面候选"),
)
BALANCED_CANDIDATES_PER_BUCKET = 2
BALANCED_CANDIDATE_EVIDENCE_WORKERS = 4


def _build_balanced_candidate_evidence_stage(
    *,
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    discovery_full: Dict[str, Any],
    canonical_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    candidates = discovery_full.get("candidates") if isinstance(discovery_full, dict) else []
    candidates = [item for item in candidates or [] if isinstance(item, dict)]

    items_by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if code and code not in items_by_code:
            items_by_code[code] = item

    normalized_canonical: List[str] = []
    for code in canonical_codes or []:
        normalized = _normalize_stock_identity_code(code)
        if normalized and normalized in items_by_code and normalized not in normalized_canonical:
            normalized_canonical.append(normalized)
    canonical_items = [items_by_code[code] for code in normalized_canonical]
    desk_converged = bool(canonical_items) and any(
        _candidate_has_desk_tag(item) for item in canonical_items
    )

    if desk_converged:
        selected = []
        for item in canonical_items:
            enriched = dict(item)
            enriched["_balanced_bucket"] = _candidate_bucket(item)
            selected.append(enriched)
        selection_mode = "canonical_desk_shortlist"
        header_note = (
            "本证据包对席位已收敛的候选入池榜逐只建立证据，"
            "与候选池、筛选、深挖共用同一份名单；JSON 为后续规划真源，Markdown 仅用于 Trace 展示。"
        )
    else:
        selected = _select_balanced_candidate_items(candidates)
        selection_mode = "balanced_buckets_fallback"
        header_note = (
            "本证据包按策略、消息、资金、基本面四类均衡抽取候选，"
            "同一股票只保留首次命中的类别并继续向后补位；"
            "JSON 为后续规划真源，Markdown 仅用于 Trace 展示。"
        )

    evidence_items: List[Dict[str, Any]] = []
    markdown_lines = [
        "# 候选证据包",
        "",
        header_note,
        "",
        "| 类别 | 股票 | 入池理由 | 技术 | 资金 | 消息 | 基本面 | 缺口 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    evidence_by_code = _collect_balanced_candidate_evidence_parallel(
        ctx=ctx,
        tool_registry=tool_registry,
        selected=selected,
    )
    for item in selected:
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if not code:
            continue
        evidence = evidence_by_code.get(code) or {}
        stock_name = _stock_name_from_evidence(code, evidence)
        packet = _candidate_evidence_packet(
            candidate=item,
            code=code,
            stock_name=stock_name,
            evidence=evidence,
        )
        evidence_items.append(packet)
        markdown_lines.append(
            "| {bucket} | {stock} | {reason} | {technical} | {capital} | {news} | {fundamental} | {missing} |".format(
                bucket=_markdown_cell(packet["bucket_label"]),
                stock=_markdown_cell(f"{code} {stock_name}".strip()),
                reason=_markdown_cell(packet["candidate_reason"]),
                technical=_markdown_cell(packet["schema"]["technical"]["summary"]),
                capital=_markdown_cell(packet["schema"]["capital_flow"]["summary"]),
                news=_markdown_cell(packet["schema"]["news_event"]["summary"]),
                fundamental=_markdown_cell(packet["schema"]["fundamental"]["summary"]),
                missing=_markdown_cell("、".join(packet["missing_evidence"]) or "-"),
            )
        )

    bucket_counts: Dict[str, int] = {}
    for packet in evidence_items:
        bucket_counts[packet["bucket"]] = bucket_counts.get(packet["bucket"], 0) + 1
    return {
        "stage": "balanced_candidate_evidence",
        "status": "ok" if evidence_items else "failed",
        "summary": {
            "target_count": len(evidence_items),
            "targets": [item["code"] for item in evidence_items],
            "bucket_counts": bucket_counts,
            "selection_mode": selection_mode,
            "schema_version": "candidate_evidence.v1",
            "json_ref": "candidate_evidence.json",
            "markdown_ref": "candidate_evidence.md",
            "main_limitations": _balanced_evidence_limitations(evidence_items),
        },
        "full": {
            "schema_version": "candidate_evidence.v1",
            "selection_policy": {
                "mode": selection_mode,
                "canonical_codes": normalized_canonical,
                "buckets": [{"key": key, "label": label, "limit": BALANCED_CANDIDATES_PER_BUCKET} for key, label in BALANCED_CANDIDATE_BUCKETS],
                "max_candidates": len(BALANCED_CANDIDATE_BUCKETS) * BALANCED_CANDIDATES_PER_BUCKET,
                "dedupe": "first bucket wins; later buckets skip selected codes",
            },
            "candidates": evidence_items,
            "candidate_evidence_json": {"schema_version": "candidate_evidence.v1", "candidates": evidence_items},
            "candidate_evidence_md": "\n".join(markdown_lines) + "\n",
        },
        "full_ref": "candidate_evidence.json",
    }


def _collect_balanced_candidate_evidence_parallel(
    *,
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    selected: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    codes: List[str] = []
    for item in selected:
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if code and code not in codes:
            codes.append(code)
    if not codes:
        return {}
    if len(codes) == 1:
        return {codes[0]: _collect_deep_dive_evidence(ctx, tool_registry, codes[0])}

    workers = max(1, min(BALANCED_CANDIDATE_EVIDENCE_WORKERS, len(codes)))
    evidence_by_code: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="balanced-evidence") as executor:
        futures = {
            executor.submit(_collect_deep_dive_evidence, ctx, tool_registry, code): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                evidence_by_code[code] = future.result()
            except Exception as exc:  # pragma: no cover - _execute_tool normally captures tool errors
                evidence_by_code[code] = {
                    "balanced_candidate_evidence": {
                        "status": "tool_failed",
                        "stock_code": code,
                        "error": str(exc),
                    }
                }
    return evidence_by_code


def _execute_tool(
    ctx: SelectionRunContext,
    tool_registry: ToolRegistry,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Any:
    with ctx.tool_call_lock:
        ctx.tool_call_sequence += 1
        step = ctx.tool_call_sequence
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
        sanitized_result = _sanitize_non_finite_numbers(result)
        call["success"] = not _is_failed_tool_result(result)
        call["result_json"] = _compact_tool_result_for_trace(sanitized_result)
        result_text = json.dumps(sanitized_result, ensure_ascii=False, default=str, allow_nan=False)
        call["result_preview"] = _truncate(result_text, 1200)
        call["result_length"] = len(result_text)
        return sanitized_result
    except Exception as exc:
        error_payload = {"status": "tool_failed", "tool": tool_name, "error": str(exc)}
        call["success"] = False
        call["result_json"] = error_payload
        call["result_preview"] = json.dumps(error_payload, ensure_ascii=False)
        call["result_length"] = len(call["result_preview"])
        return error_payload
    finally:
        call["duration"] = round(_monotonic_time() - started_at, 3)
        with ctx.tool_call_lock:
            ctx.tool_calls.append(call)
            ctx.tool_calls.sort(key=lambda item: int(item.get("step") or 0))
            _refresh_evidence_summary(ctx)
        _emit(ctx.progress_callback, "tool_done", **call)


def _select_balanced_candidate_items(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    selected_codes: set[str] = set()
    for bucket, _label in BALANCED_CANDIDATE_BUCKETS:
        bucket_items = [item for item in candidates if _candidate_bucket(item) == bucket]
        bucket_items.sort(key=lambda item: (-_candidate_display_score(item), _candidate_original_order(candidates, item)))
        for item in bucket_items:
            code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
            if not code or code in selected_codes:
                continue
            enriched = dict(item)
            enriched["_balanced_bucket"] = bucket
            selected.append(enriched)
            selected_codes.add(code)
            if sum(1 for existing in selected if existing.get("_balanced_bucket") == bucket) >= BALANCED_CANDIDATES_PER_BUCKET:
                break
    if len(selected) >= len(BALANCED_CANDIDATE_BUCKETS) * BALANCED_CANDIDATES_PER_BUCKET:
        return selected
    for item in sorted(candidates, key=lambda value: (-_candidate_display_score(value), _candidate_original_order(candidates, value))):
        code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if not code or code in selected_codes:
            continue
        enriched = dict(item)
        enriched["_balanced_bucket"] = _candidate_bucket(item)
        selected.append(enriched)
        selected_codes.add(code)
        if len(selected) >= len(BALANCED_CANDIDATE_BUCKETS) * BALANCED_CANDIDATES_PER_BUCKET:
            break
    return selected


def _candidate_original_order(candidates: List[Dict[str, Any]], target: Dict[str, Any]) -> int:
    try:
        return candidates.index(target)
    except ValueError:
        return 0


def _candidate_has_desk_tag(item: Dict[str, Any]) -> bool:
    """席位是否给候选打过标签：有 primary_desk 或确定性的 setup_type 即视为已收敛。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("primary_desk") or "").strip():
        return True
    setup = str(item.get("setup_type") or "").strip().lower()
    if setup and setup != "unknown":
        return True
    desks = item.get("desks")
    if isinstance(desks, (list, tuple)) and any(str(d or "").strip() for d in desks):
        return True
    return False


def _candidate_bucket(item: Dict[str, Any]) -> str:
    values = [value.lower() for value in _candidate_source_values(item)]
    dimensions = item.get("reason_dimensions")
    if isinstance(dimensions, list):
        for entry in dimensions:
            if isinstance(entry, dict):
                dim = str(entry.get("dimension") or entry.get("label") or "").strip().lower()
                if dim:
                    values.append(dim)
    text = " ".join(values)
    if any(token in text for token in ("news", "event", "sentiment", "消息", "事件", "情绪")):
        return "news"
    if any(token in text for token in ("capital", "money", "flow", "资金", "主力")):
        return "capital"
    if any(token in text for token in ("fundamental", "quality", "growth", "valuation", "基本面", "估值", "成长")):
        return "fundamental"
    if any(token in text for token in ("alphasift", "sequoia", "strategy", "rps", "turtle", "ma_", "策略", "突破", "形态")):
        return "strategy"
    return "strategy"


def _candidate_bucket_label(bucket: str) -> str:
    return dict(BALANCED_CANDIDATE_BUCKETS).get(bucket, bucket or "候选")


def _candidate_evidence_packet(
    *,
    candidate: Dict[str, Any],
    code: str,
    stock_name: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    bucket = str(candidate.get("_balanced_bucket") or _candidate_bucket(candidate))
    schema = {
        "base": _candidate_base_schema(candidate, code, stock_name, evidence),
        "technical": _candidate_technical_schema(evidence),
        "capital_flow": _candidate_capital_schema(evidence),
        "news_event": _candidate_news_schema(evidence),
        "fundamental": _candidate_fundamental_schema(evidence),
        "chip": _candidate_chip_schema(evidence),
        "risk": _candidate_risk_schema(evidence),
    }
    missing = _candidate_schema_missing(schema)
    return {
        "code": code,
        "name": stock_name,
        "bucket": bucket,
        "bucket_label": _candidate_bucket_label(bucket),
        "candidate_source": _candidate_source_label(candidate),
        "candidate_reason": _candidate_reason(candidate),
        "candidate_score": _candidate_display_score(candidate),
        "candidate_labels": _candidate_labels(candidate),
        "schema": schema,
        "missing_evidence": missing,
        "raw_evidence": evidence,
    }


def _candidate_base_schema(candidate: Dict[str, Any], code: str, stock_name: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    quote = evidence.get("get_realtime_quote") if isinstance(evidence, dict) else {}
    info = evidence.get("get_stock_info") if isinstance(evidence, dict) else {}
    boards = info.get("belong_boards") if isinstance(info, dict) else []
    return {
        "status": "ok",
        "code": code,
        "name": stock_name,
        "market": candidate.get("market") or "cn",
        "price": quote.get("price") if isinstance(quote, dict) else None,
        "change_pct": quote.get("change_pct") if isinstance(quote, dict) else None,
        "volume_ratio": quote.get("volume_ratio") if isinstance(quote, dict) else None,
        "turnover_rate": quote.get("turnover_rate") if isinstance(quote, dict) else None,
        "industry": boards[:5] if isinstance(boards, list) else [],
        "summary": f"{code} {stock_name}".strip(),
    }


def _candidate_technical_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    trend = evidence.get("analyze_trend") if isinstance(evidence, dict) else {}
    structure = evidence.get("analyze_price_structure") if isinstance(evidence, dict) else {}
    status = "ok" if isinstance(trend, dict) and not _is_failed_tool_result(trend) else "missing"
    summary = str(trend.get("trend_status") or structure.get("status") or "技术证据缺失") if isinstance(trend, dict) else "技术证据缺失"
    return {
        "status": status,
        "summary": summary,
        "trend_status": trend.get("trend_status") if isinstance(trend, dict) else None,
        "bias_ma5": trend.get("bias_ma5") if isinstance(trend, dict) else None,
        "support_levels": trend.get("support_levels") if isinstance(trend, dict) else None,
        "resistance_levels": trend.get("resistance_levels") if isinstance(trend, dict) else None,
        "price_structure_status": structure.get("status") if isinstance(structure, dict) else None,
    }


def _candidate_capital_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    flow = evidence.get("get_capital_flow") if isinstance(evidence, dict) else {}
    if not isinstance(flow, dict) or _is_failed_tool_result(flow):
        return {
            "status": str(flow.get("status") or "missing") if isinstance(flow, dict) else "missing",
            "summary": str(flow.get("error_summary") or "资金面数据缺失") if isinstance(flow, dict) else "资金面数据缺失",
            "errors": flow.get("errors") if isinstance(flow, dict) else [],
        }
    return {
        "status": "ok",
        "summary": f"主力净流入={flow.get('main_net_inflow')}",
        "main_net_inflow": flow.get("main_net_inflow"),
        "inflow_5d": flow.get("inflow_5d"),
        "inflow_10d": flow.get("inflow_10d"),
        "latest_date": flow.get("latest_date"),
        "source_update": flow.get("source_update"),
    }


def _candidate_news_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    news = evidence.get("search_comprehensive_intel") if isinstance(evidence, dict) else {}
    if not isinstance(news, dict) or _is_failed_tool_result(news):
        return {"status": "missing", "summary": "消息面数据缺失", "errors": news.get("errors") if isinstance(news, dict) else []}
    text = str(news.get("report") or news.get("summary") or news.get("status") or "")
    return {"status": "ok" if text else "partial", "summary": _truncate(text or "消息面未形成明确结论", 180)}


def _candidate_fundamental_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    info = evidence.get("get_stock_info") if isinstance(evidence, dict) else {}
    if not isinstance(info, dict) or _is_failed_tool_result(info):
        return {"status": "missing", "summary": "基本面数据缺失"}
    keys = ("roe", "revenue_growth", "profit_growth", "pe_ttm", "pb", "debt_ratio")
    metrics = {
        key: value
        for key in keys
        for value in [_sanitize_non_finite_numbers(info.get(key))]
        if value is not None
    }
    return {
        "status": "ok" if metrics else "partial",
        "summary": "基本面指标已获取" if metrics else "仅获取到公司基础信息",
        "metrics": metrics,
    }


def _candidate_chip_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    chip = evidence.get("get_chip_distribution") if isinstance(evidence, dict) else {}
    if not isinstance(chip, dict) or _is_failed_tool_result(chip):
        return {
            "status": str(chip.get("status") or "missing") if isinstance(chip, dict) else "missing",
            "summary": str(chip.get("error_summary") or "筹码数据缺失") if isinstance(chip, dict) else "筹码数据缺失",
        }
    return {
        "status": "ok",
        "summary": f"平均成本={chip.get('avg_cost')}",
        "avg_cost": chip.get("avg_cost"),
        "concentration_90": chip.get("concentration_90"),
        "profit_ratio": chip.get("profit_ratio"),
    }


def _candidate_risk_schema(evidence: Dict[str, Any]) -> Dict[str, Any]:
    failures = _tool_failures_from_evidence(evidence)
    missing = _missing_from_evidence(evidence)
    return {"status": "partial" if failures or missing else "ok", "tool_failures": failures, "missing_evidence": missing}


def _candidate_schema_missing(schema: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for key in ("technical", "capital_flow", "news_event", "fundamental", "chip"):
        status = str((schema.get(key) or {}).get("status") or "")
        if status in {"missing", "failed", "timeout", "tool_failed"}:
            missing.append(key)
    return missing


def _balanced_evidence_limitations(items: List[Dict[str, Any]]) -> List[str]:
    limitations: List[str] = []
    for item in items:
        missing = item.get("missing_evidence") if isinstance(item, dict) else []
        if missing:
            limitations.append(f"{item.get('code')} 缺失：{', '.join(str(x) for x in missing[:4])}")
    return limitations[:8]


def _balanced_evidence_targets(summary: Dict[str, Any]) -> List[str]:
    targets = summary.get("targets") if isinstance(summary, dict) else []
    if not isinstance(targets, list):
        return []
    return [_normalize_stock_identity_code(item) for item in targets if _normalize_stock_identity_code(item)]


def _balanced_raw_evidence_for_code(ctx: SelectionRunContext, code: str) -> Optional[Dict[str, Any]]:
    normalized = _normalize_stock_identity_code(code)
    if not normalized:
        return None
    full = ctx.stage_full("balanced_candidate_evidence")
    candidates = full.get("candidates") if isinstance(full, dict) else []
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_code = _normalize_stock_identity_code(item.get("code") or item.get("stock_code"))
        if item_code != normalized:
            continue
        raw_evidence = item.get("raw_evidence")
        if isinstance(raw_evidence, dict):
            return raw_evidence
    return None


def _merge_preferred_targets(preferred: List[str], existing: List[str]) -> List[str]:
    merged: List[str] = []
    for code in list(preferred or []) + list(existing or []):
        normalized = _normalize_stock_identity_code(code)
        if normalized and normalized not in merged:
            merged.append(normalized)
    return merged


def _monotonic_time() -> float:
    return time.monotonic()


def _compact_tool_result_for_trace(result: Any) -> Any:
    """Keep structured trace payloads useful without duplicating large raw blobs."""
    if not isinstance(result, dict):
        return _sanitize_non_finite_numbers(result)
    candidates = result.get("candidates")
    if isinstance(candidates, list) and result.get("status") is not None:
        return _sanitize_non_finite_numbers(_compact_candidate_seed(result, limit=DEFAULT_CANDIDATE_LIMIT))
    if result.get("expert_packets") or result.get("discovery_steps") or result.get("quality"):
        return _sanitize_non_finite_numbers(_compact_candidate_seed(result, limit=DEFAULT_CANDIDATE_LIMIT))
    return _sanitize_non_finite_numbers(result)


def _sanitize_non_finite_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_non_finite_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_non_finite_numbers(item) for item in value]
    return value


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
    expert_packets = seed_result.get("expert_packets") if isinstance(seed_result.get("expert_packets"), list) else []
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
    compact_expert_packets: List[Dict[str, Any]] = []
    for packet in expert_packets:
        if not isinstance(packet, dict):
            continue
        packet_candidates = packet.get("candidates") if isinstance(packet.get("candidates"), list) else []
        compact_expert_packets.append({
            "expert": packet.get("expert"),
            "dimension": packet.get("dimension"),
            "status": packet.get("status"),
            "data_quality": packet.get("data_quality"),
            "themes": packet.get("themes") if isinstance(packet.get("themes"), list) else [],
            "candidates": [
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "market": item.get("market"),
                    "source": item.get("source"),
                    "reason": _truncate(str(item.get("reason") or ""), 240),
                    "reason_dimensions": _compact_reason_dimensions(item.get("reason_dimensions")),
                    "recall_sources": _as_text_list(item.get("recall_sources"))[:6],
                    "matched_strategies": _as_text_list(item.get("matched_strategies"))[:6],
                    "strategy_tags": _as_text_list(item.get("strategy_tags"))[:8],
                    "score": item.get("score"),
                    "confidence": item.get("confidence"),
                }
                for item in packet_candidates[:limit]
                if isinstance(item, dict)
            ],
            "diagnostics": packet.get("diagnostics") if isinstance(packet.get("diagnostics"), list) else [],
            "errors": _as_text_list(packet.get("errors"))[:6],
        })
    return {
        "status": seed_result.get("status"),
        "market": seed_result.get("market"),
        "candidate_source": seed_result.get("candidate_source"),
        "candidate_count": seed_result.get("candidate_count") or len(candidates),
        "seed_pool_summary": seed_result.get("seed_pool_summary"),
        "seed_pool_summary_before_gate": seed_result.get("seed_pool_summary_before_gate"),
        "seed_gate": seed_result.get("seed_gate"),
        "seed_pool_diagnostics": seed_result.get("seed_pool_diagnostics"),
        "seed_pool_hard_exclusion": seed_result.get("seed_pool_hard_exclusion"),
        "seed_source_quality": seed_result.get("seed_source_quality"),
        "seed_market_regime": seed_result.get("seed_market_regime"),
        "seed_fact_summary": seed_result.get("seed_fact_summary"),
        "seed_fact_packets": _compact_seed_fact_packets(seed_result.get("seed_fact_packets")),
        "candidates": compact_candidates,
        "expert_packets": compact_expert_packets,
        "quality_summary": seed_result.get("quality_summary"),
        "lifecycle_summary": seed_result.get("lifecycle_summary"),
        "source_summary": _compact_source_summary(seed_result.get("source_summary")),
        "discovery_steps": _compact_discovery_steps(seed_result.get("discovery_steps")),
        "theme_observations": _compact_theme_observations(seed_result),
        "errors": _as_text_list(seed_result.get("errors"))[:6],
    }


def _compact_seed_fact_packets(value: Any, *, limit: int = DEFAULT_CANDIDATE_LIMIT) -> List[Dict[str, Any]]:
    packets = value if isinstance(value, list) else []
    return compact_seed_fact_packets_for_model(packets, limit=limit)


def _compact_seed_pool_build_result(build_result: Any) -> Dict[str, Any]:
    seeds = list(getattr(build_result, "seeds", []) or [])
    total_limit = int(getattr(build_result, "total_limit", 0) or len(seeds) or 0)
    return _sanitize_non_finite_numbers({
        "status": "ok",
        "phase": "built",
        "seed_pool_summary": _summarize_seed_items(seeds, total_limit=total_limit),
        "seed_pool_diagnostics": _compact_seed_pool_diagnostics(getattr(build_result, "diagnostics", []) or []),
        "seed_pool_hard_exclusion": getattr(build_result, "hard_exclusion", {}) or {},
        "seed_source_quality": getattr(build_result, "source_quality", {}) or {},
        "seed_market_regime": getattr(build_result, "market_regime", {}) or {},
    })


def _compact_seed_gate_result(seed_result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(seed_result, dict):
        return {"status": "invalid", "phase": "gate"}
    gate = seed_result.get("seed_gate") if isinstance(seed_result.get("seed_gate"), dict) else {}
    return _sanitize_non_finite_numbers({
        "status": gate.get("status") or "not_run",
        "phase": "gate",
        "seed_pool_summary_before_gate": seed_result.get("seed_pool_summary_before_gate"),
        "seed_pool_summary": seed_result.get("seed_pool_summary"),
        "seed_gate": {
            "status": gate.get("status"),
            "elapsed_ms": gate.get("elapsed_ms"),
            "kept_count": gate.get("kept_count"),
            "rejected_count": gate.get("rejected_count"),
            "diagnostics": _compact_seed_pool_diagnostics(gate.get("diagnostics") or []),
            "decisions": _compact_seed_gate_decisions(gate.get("decisions") or []),
            "error": gate.get("error"),
        },
        "candidate_count": seed_result.get("candidate_count"),
        "candidate_source": seed_result.get("candidate_source"),
    })


def _summarize_seed_items(seeds: Sequence[Any], *, total_limit: int) -> Dict[str, Any]:
    source_counts: Dict[str, int] = {}
    dimension_counts: Dict[str, int] = {}
    preview: List[Dict[str, Any]] = []
    for seed in seeds:
        source = str(getattr(seed, "source", "") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        trigger_signals = getattr(seed, "trigger_signals", None)
        trigger_signals = trigger_signals if isinstance(trigger_signals, list) else []
        for signal in trigger_signals:
            if not isinstance(signal, dict):
                continue
            dimension = str(signal.get("dimension") or "").strip()
            if dimension:
                dimension_counts[dimension] = dimension_counts.get(dimension, 0) + 1
        if len(preview) < 20:
            source_diagnostics = {
                "source": source,
                "priority_score": getattr(seed, "priority_score", None),
                "score_kind": "seed_recall_priority",
                "note": "source-local recall diagnostic only; not comparable across seed sources",
            }
            preview.append({
                "code": getattr(seed, "code", ""),
                "name": getattr(seed, "name", ""),
                "source": source,
                "hint": _truncate(str(getattr(seed, "hint", "") or ""), 120),
                "source_diagnostics": {key: value for key, value in source_diagnostics.items() if value is not None},
                "freshness": getattr(seed, "freshness", None),
                "trigger_signals": trigger_signals[:4],
            })
    return {
        "seed_count": len(seeds),
        "seed_sources": source_counts,
        "signal_dimensions": dimension_counts,
        "total_limit": total_limit,
        "preview": preview,
    }


def _compact_seed_pool_diagnostics(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    diagnostics: List[Dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        diagnostics.append({
            "source": item.get("source"),
            "status": item.get("status"),
            "count": item.get("count"),
            "freshness": item.get("freshness"),
            "error": _truncate(str(item.get("error") or ""), 240),
            "detail": _truncate(str(item.get("detail") or item.get("message") or ""), 240),
        })
    return diagnostics


def _compact_seed_gate_decisions(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    decisions: List[Dict[str, Any]] = []
    for item in value[:30]:
        if not isinstance(item, dict):
            continue
        decisions.append({
            "code": item.get("code"),
            "decision": item.get("decision") or item.get("action"),
            "reason": _truncate(str(item.get("reason") or ""), 160),
            "rank": item.get("rank"),
        })
    return decisions


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


def _screening_candidate_evidence_table(ctx: SelectionRunContext) -> str:
    """Return the candidate evidence markdown table for the screening prompt."""
    full = ctx.stage_full("balanced_candidate_evidence")
    md = full.get("candidate_evidence_md") if isinstance(full, dict) else ""
    return str(md or "")


def _screening_candidate_evidence_data(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    """Return compact per-candidate schema data for the screening prompt.

    Includes dimension summaries and key metrics so the screening LLM can
    actually assess each candidate's evidence coverage, rather than seeing
    only metadata references it cannot read.
    """
    full = ctx.stage_full("balanced_candidate_evidence")
    candidates = full.get("candidates") if isinstance(full, dict) else []
    if not isinstance(candidates, list):
        return []
    compact: List[Dict[str, Any]] = []
    for packet in candidates:
        if not isinstance(packet, dict):
            continue
        schema = packet.get("schema") if isinstance(packet.get("schema"), dict) else {}
        entry: Dict[str, Any] = {
            "code": packet.get("code"),
            "name": packet.get("name"),
            "bucket": packet.get("bucket_label") or packet.get("bucket"),
            "candidate_reason": packet.get("candidate_reason"),
            "candidate_score": packet.get("candidate_score"),
            "missing_evidence": packet.get("missing_evidence") or [],
        }
        for dim in ("base", "technical", "capital_flow", "news_event", "fundamental", "chip", "risk"):
            dim_data = schema.get(dim)
            if isinstance(dim_data, dict):
                entry[dim] = {k: v for k, v in dim_data.items() if k != "errors"}
            else:
                entry[dim] = {"status": "missing", "summary": "数据缺失"}
        compact.append(entry)
    return compact


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
    payload = {
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
    if isinstance(seed_result, dict):
        for key in (
            "seed_pool_summary",
            "seed_pool_summary_before_gate",
            "seed_gate",
            "seed_pool_diagnostics",
            "seed_pool_hard_exclusion",
            "seed_source_quality",
            "seed_market_regime",
            "recall_diagnostics",
            "recall_sources",
            "recall_total_in",
            "recall_total_kept",
            "thesis_desk_packets",
            "thesis_desk_diagnostics",
            "thesis_desk_committee",
            "thesis_desk_committee_elapsed_ms",
            "llm_expert_committee",
            "error",
        ):
            value = seed_result.get(key)
            if value not in (None, [], {}):
                payload[key] = value
                payload["full"][key] = value
        # Surface a top-level degraded/status so the frontend banner can flag a
        # silent desk fallback without digging into the sub-dicts.
        desk_diag = (
            seed_result.get("thesis_desk_committee")
            or seed_result.get("llm_expert_committee")
        )
        if isinstance(desk_diag, dict):
            degraded = bool(desk_diag.get("degraded")) or bool(seed_result.get("error"))
            payload["degraded"] = degraded
            payload["full"]["degraded"] = degraded
            desk_status = desk_diag.get("status")
            dims = desk_diag.get("dimensions_covered")
            if dims is not None:
                payload["dimensions_covered"] = dims
                payload["full"]["dimensions_covered"] = dims
            if desk_status and payload.get("status") == "ok" and degraded:
                payload["status"] = "partial"
    return payload


def _non_thesis_desk_mode_exit_payload(ctx: SelectionRunContext) -> Dict[str, Any]:
    reason = (
        "当前调试阶段只允许 thesis_desk_committee（三席位）候选发现模式；"
        f"本次请求模式为 {ctx.candidate_discovery_mode or 'deterministic'}，已在候选发现前停止。"
    )
    return {
        "stage": "candidate_discovery",
        "status": "skipped",
        "strategy": ctx.candidate_strategy,
        "market": ctx.market,
        "candidate_count": 0,
        "candidate_source": "mode_guard",
        "summary": {
            "strategy": ctx.candidate_strategy,
            "candidate_codes": [],
            "key_sources": [],
            "main_limitations": [reason],
            "next_required_tools": [],
            "next_step": "stop_non_thesis_desk_mode",
        },
        "full": {
            "candidates": [],
            "excluded": [],
            "tool_failures": [],
            "missing_evidence": ["thesis_desk_committee mode not selected"],
            "required_candidate_discovery_mode": "thesis_desk_committee",
            "actual_candidate_discovery_mode": ctx.candidate_discovery_mode,
            "blocked": True,
            "reason": reason,
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
        for key in (
            "expert_packets",
            "quality",
            "hard_exclusion",
            "capacity",
            "themes",
            "discovery_steps",
            "candidate_pool_run_id",
            "thesis_desk_packets",
            "thesis_desk_diagnostics",
            "thesis_desk_committee",
            "llm_expert_committee",
        ):
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
                "auction_trigger": "竞价承接转强且开盘后不快速跌破分时均线",
                "breakout_trigger": "放量突破关键压力位后不回落",
                "pullback_trigger": "回踩关键支撑或均线不破后重新放量",
                "no_chase_line": "高于关键压力位且乖离扩大时不追",
                "stop_loss": "跌破关键支撑或账户止损线",
                "failure_condition": "跌破关键支撑、资金转弱或出现重大利空",
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


def _build_meta_orchestrator_input(ctx: SelectionRunContext) -> Dict[str, Any]:
    candidate_records = _candidate_records_by_code(ctx.stage_full("candidate_discovery"))
    code_order = _prompt_code_order(ctx)
    deep_results = _order_rows_by_codes(
        _deep_dive_results_for_meta(ctx),
        code_order,
        lambda item: item.get("code") if isinstance(item, dict) else None,
    )[:PROMPT_STOCK_LIMIT]
    selected_codes = {_normalize_stock_identity_code(item.get("code")) for item in deep_results if isinstance(item, dict)}
    return {
        "user_message": ctx.user_message,
        "market": ctx.market,
        "market_context": _summarize_market_regime(ctx.market_regime),
        "candidate_discovery_summary": ctx.stage_summary("candidate_discovery"),
        "candidate_screening_summary": ctx.stage_summary("candidate_screening"),
        "desk_reports": _desk_reports_for_meta(candidate_records, deep_results),
        "deep_dive_results": deep_results,
        "fact_sheets": {
            code: _truncate_nested_for_prompt(record.get("fact_sheet"))
            for code, record in candidate_records.items()
            if _normalize_stock_identity_code(code) in selected_codes and isinstance(record, dict) and isinstance(record.get("fact_sheet"), dict)
        },
        "seed_facts": {
            code: _truncate_nested_for_prompt(record.get("seed_fact"))
            for code, record in candidate_records.items()
            if _normalize_stock_identity_code(code) in selected_codes and isinstance(record, dict) and isinstance(record.get("seed_fact"), dict)
        },
        "input_policy": {
            "raw_tool_json": "not_passed",
            "meta_agent_role": "asset_regime_and_constraint_packaging_only",
            "point_calculation_role": "if_then_condition_order_math_only",
            "stock_limit": PROMPT_STOCK_LIMIT,
        },
    }


def _build_pricing_agent_input(ctx: SelectionRunContext) -> Dict[str, Any]:
    return {
        "user_message": ctx.user_message,
        "market": ctx.market,
        "market_context": _summarize_market_regime(ctx.market_regime),
        "meta_orchestrator_summary": ctx.stage_summary("meta_orchestrator"),
        "scenario_constraint_packages": _compact_meta_packages_for_prompt(ctx),
        "deep_dive_results_summary": ctx.stage_summary("single_stock_deep_dive"),
        "realtime_or_recent_evidence": _pricing_recent_evidence(ctx),
        "account_summary": ctx.account_summary,
        "investor_profile": ctx.investor_profile,
    }


def _short_list(value: Any, limit: int) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return [item for item in value if item not in (None, "")][:limit]
    if value in (None, ""):
        return []
    return [value]


def _first_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _compact_dimension_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            continue
        compact[str(key)] = {
            "verdict": item.get("verdict"),
            "summary": _truncate(str(item.get("summary") or ""), 180),
        }
    return compact


def _compact_desk_evidence(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: Dict[str, Any] = {}
    for desk, rows in value.items():
        items = []
        for row in _short_list(rows, 4):
            if isinstance(row, dict):
                items.append({
                    "tool": row.get("tool"),
                    "summary": _truncate(str(row.get("summary") or row.get("reason") or ""), 180),
                })
            else:
                items.append(_truncate(str(row), 180))
        compact[str(desk)] = items
    return compact


def _asset_regime_from_setup(setup_type: str, item: Dict[str, Any], market: Dict[str, Any]) -> str:
    regime = str(market.get("regime") or "").lower()
    risk = str(market.get("risk_level") or "").lower()
    if regime in {"risk_off", "panic", "trending_down"} or risk in {"high", "extreme"}:
        return "Avoid_By_Market_Regime"
    setup = str(setup_type or "").lower()
    risks = " ".join(str(x) for x in _short_list(item.get("main_risks"), 8))
    if setup in {"trend_continuation", "capital_momentum"}:
        if any(token in risks for token in ("追高", "过热", "衰竭", "高位")):
            return "Right_Side_Momentum_High_Exhaustion_Risk"
        return "Right_Side_Momentum"
    if setup == "early_turn":
        return "Early_Turn_Low_Base_Confirmation"
    if setup == "quality_repair":
        return "Quality_Repair_Price_Not_Fully_Reflected"
    if setup == "theme_follow":
        return "Theme_Follow_Breadth_Dependent"
    return "Unknown"


def _meta_factual_consensus(item: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    facts: List[str] = []
    for key in ("ideal_entry_zone", "no_chase_line", "stop_loss", "failure_condition"):
        value = item.get(key)
        if value:
            facts.append(f"{key}: {value}")
    for evidence in _short_list(item.get("main_supporting_evidence"), 3):
        facts.append(str(evidence))
    if record.get("confidence") is not None:
        facts.append(f"席位工具覆盖率 confidence={record.get('confidence')}")
    return facts[:6] or ["深挖阶段已有结构化摘要,但事实共识不足。"]


def _meta_divergence_text(record: Dict[str, Any], item: Dict[str, Any]) -> str:
    desks = _short_list(record.get("desks"), 5)
    primary = record.get("primary_desk") or record.get("setup_type") or "unknown"
    risks = _short_list(item.get("main_risks") or record.get("risks"), 3)
    if risks:
        return f"主导席位为 {primary}, 但存在反方风险: {'; '.join(str(r) for r in risks)}"
    if desks:
        return f"主导席位为 {primary}, 参与席位 {', '.join(str(d) for d in desks)} 暂无显著冲突。"
    return "席位分歧信息不足,按保守条件型约束处理。"


def _opposing_desks(record: Dict[str, Any]) -> List[str]:
    primary = str(record.get("primary_desk") or "")
    return [str(desk) for desk in _short_list(record.get("desks"), 5) if str(desk) != primary]


def _meta_regime_adjustment(market: Dict[str, Any]) -> str:
    regime = str(market.get("regime") or "unknown")
    if regime == "trending_up":
        return "大盘偏强,可保留动量延续剧本,但仍需回踩确认和追高约束。"
    if regime in {"range_bound", "high_volatility"}:
        return "震荡或高波动环境,高位突破需按诱多风险处理,提高回撤确认要求。"
    if regime in {"risk_off", "panic", "trending_down"}:
        return "风险偏好下降,主动做多和追高信号降级,优先观察/防守。"
    return "市场环境不明,所有场景按条件触发处理。"


def _meta_risk_constraints(record: Dict[str, Any], item: Dict[str, Any], market: Dict[str, Any]) -> List[Dict[str, Any]]:
    constraints: List[Dict[str, Any]] = []
    for risk in _short_list(item.get("main_risks") or record.get("risks"), 5):
        constraints.append({"constraint": str(risk), "source": "desk_or_deep_dive_risk"})
    if item.get("no_chase_line"):
        constraints.append({"constraint": f"超过追高线不得追高: {item.get('no_chase_line')}", "source": "deep_dive"})
    regime = str(market.get("regime") or "")
    if regime in {"risk_off", "panic", "trending_down"}:
        constraints.append({"constraint": f"市场状态 {regime} 下不得主动追高或无条件开仓", "source": "detect_market_regime"})
    return constraints[:8]


def _default_required_pricing_scenarios() -> List[Dict[str, str]]:
    return [
        {
            "scenario_name": "Breakout_Continuation",
            "condition": "价格维持在结构失效位之上,且缩量回踩或放量续强",
            "required_output": "计算右侧顺势入场区间、止损和最小盈亏比",
        },
        {
            "scenario_name": "Fakeout_Exhaustion",
            "condition": "价格放量跌破结构失效位",
            "required_output": "计算退出条件、回避条件和风险提示;A股默认不生成做空执行单",
        },
        {
            "scenario_name": "Mean_Reversion_Pullback",
            "condition": "价格回落至均值回归锚点附近且未破前低",
            "required_output": "计算低吸区间、防守止损位和确认条件",
        },
    ]


def _main_meta_constraints(packages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    constraints: List[Dict[str, Any]] = []
    for package in packages:
        stock = package.get("stock") if isinstance(package.get("stock"), dict) else {}
        hard = package.get("hard_constraints_for_pricing_agent") if isinstance(package.get("hard_constraints_for_pricing_agent"), dict) else {}
        for key in ("invalidation_level", "mean_reversion_anchor", "max_chase_premium"):
            value = hard.get(key)
            if isinstance(value, dict):
                constraints.append({
                    "code": stock.get("code"),
                    "constraint_type": key,
                    "reason": value.get("reason") or value.get("value"),
                })
    return constraints[:12]


def _meta_packages(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    full = ctx.stage_full("meta_orchestrator")
    packages = full.get("packages") if isinstance(full, dict) else []
    return [item for item in packages or [] if isinstance(item, dict)]


def _prompt_code_order(ctx: SelectionRunContext) -> List[str]:
    """Stable top-code order for prompt budgets: executable plans first, then upstream rank."""
    order: List[str] = []

    def add(raw_code: Any) -> None:
        code = _normalize_stock_identity_code(raw_code)
        if code and code not in order:
            order.append(code)

    for item in sorted(_pricing_matrix(ctx), key=_point_calc_item_rank, reverse=True):
        add(item.get("code") if isinstance(item, dict) else None)
    for package in _meta_packages(ctx):
        stock = package.get("stock") if isinstance(package.get("stock"), dict) else {}
        add(stock.get("code"))
    for item in _deep_dive_results_for_meta(ctx):
        add(item.get("code") if isinstance(item, dict) else None)
    screening = ctx.stage_summary("candidate_screening")
    for code in _short_list(screening.get("deep_dive_targets") if isinstance(screening, dict) else None, PROMPT_STOCK_LIMIT):
        add(code)
    for code in _candidate_records_by_code(ctx.stage_full("candidate_discovery")):
        add(code)
    return order[:PROMPT_STOCK_LIMIT]


def _order_rows_by_codes(
    rows: Sequence[Dict[str, Any]],
    code_order: Sequence[str],
    code_getter: Callable[[Dict[str, Any]], Any],
) -> List[Dict[str, Any]]:
    if not code_order:
        return [item for item in rows if isinstance(item, dict)]
    rank = {code: idx for idx, code in enumerate(code_order)}

    def key(item: Dict[str, Any]) -> Tuple[int, int]:
        code = _normalize_stock_identity_code(code_getter(item))
        return (rank.get(code, len(rank) + 1), 0)

    return sorted([item for item in rows if isinstance(item, dict)], key=key)


def _truncate_nested_for_prompt(value: Any, *, max_depth: int = 4, max_items: int = 8, max_chars: int = 360) -> Any:
    if max_depth <= 0:
        return _truncate(str(value), max_chars)
    if isinstance(value, dict):
        compact: Dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                compact["_truncated_keys"] = max(0, len(value) - max_items)
                break
            compact[str(key)] = _truncate_nested_for_prompt(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
        return compact
    if isinstance(value, list):
        compact_list = [
            _truncate_nested_for_prompt(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            compact_list.append({"_truncated_items": len(value) - max_items})
        return compact_list
    if isinstance(value, str):
        return _truncate(value, max_chars)
    return value


def _compact_meta_packages_for_prompt(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    code_order = _prompt_code_order(ctx)
    packages = _order_rows_by_codes(
        _meta_packages(ctx),
        code_order,
        lambda item: (item.get("stock") or {}).get("code") if isinstance(item.get("stock"), dict) else None,
    )[:PROMPT_STOCK_LIMIT]
    compact: List[Dict[str, Any]] = []
    for package in packages:
        stock = package.get("stock") if isinstance(package.get("stock"), dict) else {}
        meta = package.get("meta_analysis") if isinstance(package.get("meta_analysis"), dict) else {}
        market = package.get("market_context") if isinstance(package.get("market_context"), dict) else {}
        constraints = package.get("hard_constraints_for_pricing_agent") if isinstance(package.get("hard_constraints_for_pricing_agent"), dict) else {}
        scenarios = package.get("required_pricing_scenarios") if isinstance(package.get("required_pricing_scenarios"), list) else []
        compact.append({
            "stock": {
                "code": stock.get("code"),
                "name": stock.get("name"),
                "market": stock.get("market"),
            },
            "meta_analysis": {
                "asset_regime": meta.get("asset_regime"),
                "dominant_thesis": meta.get("dominant_thesis"),
                "opposing_theses": _short_list(meta.get("opposing_theses"), PROMPT_CONSTRAINT_LIMIT),
                "factual_consensus": [_truncate(str(item), 180) for item in _short_list(meta.get("factual_consensus"), PROMPT_CONSTRAINT_LIMIT)],
                "strategic_divergence": _truncate(str(meta.get("strategic_divergence") or ""), 240),
            },
            "market_context": _truncate_nested_for_prompt(market, max_depth=2, max_items=6, max_chars=180),
            "hard_constraints_for_pricing_agent": _truncate_nested_for_prompt(
                constraints,
                max_depth=3,
                max_items=6,
                max_chars=220,
            ),
            "required_pricing_scenarios": [
                {
                    "scenario_name": scenario.get("scenario_name"),
                    "condition": _truncate(str(scenario.get("condition") or ""), 180),
                    "required_output": _truncate(str(scenario.get("required_output") or ""), 180),
                }
                for scenario in scenarios[:PROMPT_SCENARIO_LIMIT]
                if isinstance(scenario, dict)
            ],
        })
    return compact


def _pricing_recent_evidence(ctx: SelectionRunContext) -> Dict[str, Any]:
    deep_results = _deep_dive_results_for_meta(ctx)
    code_order = set(_prompt_code_order(ctx))
    return {
        item.get("code"): {
            "quote_basis": item.get("quote_basis"),
            "ideal_entry_zone": item.get("ideal_entry_zone"),
            "stop_loss": item.get("stop_loss"),
            "no_chase_line": item.get("no_chase_line"),
        }
        for item in deep_results
        if item.get("code") and _normalize_stock_identity_code(item.get("code")) in code_order
    }


def _pricing_action_for_scenario(name: str, asset_regime: str, market_regime: Dict[str, Any]) -> Tuple[str, str]:
    regime = str((market_regime or {}).get("regime") or "").lower()
    if "Avoid" in asset_regime or regime in {"risk_off", "panic", "trending_down"}:
        return "monitor", "plain_wait"
    if name == "Breakout_Continuation":
        return "wait", "conditional_open"
    if name == "Mean_Reversion_Pullback":
        return "wait", "conditional_open"
    return "monitor", "plain_wait"


def _pricing_entry_zone(name: str, constraints: Dict[str, Any]) -> str:
    if name == "Mean_Reversion_Pullback":
        return _constraint_reason(constraints.get("mean_reversion_anchor")) or "回踩均值锚点附近确认"
    if name == "Breakout_Continuation":
        return "结构失效位上方回踩确认或放量续强"
    return "跌破结构位时不入场,只执行退出/回避"


def _constraint_reason(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("reason") or value.get("value") or "").strip()
    return str(value or "").strip()


def _constraints_used(constraints: Dict[str, Any]) -> List[str]:
    used = []
    for key in ("invalidation_level", "mean_reversion_anchor", "max_chase_premium"):
        reason = _constraint_reason(constraints.get(key))
        if reason:
            used.append(f"{key}: {reason}")
    return used


def _main_pricing_constraints(matrix: Sequence[Dict[str, Any]]) -> List[str]:
    constraints: List[str] = []
    for item in matrix:
        for scenario in item.get("scenarios") or []:
            if isinstance(scenario, dict) and scenario.get("constraints_used"):
                constraints.extend(str(x) for x in scenario.get("constraints_used")[:2])
    return list(dict.fromkeys(constraints))[:8]


def _pricing_matrix(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    full = ctx.stage_full("pricing_agent")
    matrix = full.get("if_then_order_matrix") if isinstance(full, dict) else []
    return [item for item in matrix or [] if isinstance(item, dict)]


def _point_calc_item_rank(item: Dict[str, Any]) -> int:
    if not isinstance(item, dict):
        return -999
    score = 0
    status = str(item.get("data_status") or "").lower()
    if status == "ok":
        score += 20
    elif status == "partial":
        score -= 5
    elif status in {"insufficient_data", "failed", "tool_failed"}:
        score -= 40
    scenarios = item.get("scenarios") if isinstance(item.get("scenarios"), list) else []
    selected = _selected_pricing_scenario(item) or {}
    executable_modes = {"immediate_open", "conditional_open", "strong_watch"}
    if selected.get("execution_mode") in executable_modes:
        score += 60
    if any(isinstance(s, dict) and s.get("execution_mode") in executable_modes for s in scenarios):
        score += 30
    if _has_text_value(selected.get("entry_zone")) and _has_text_value(selected.get("stop_loss")):
        score += 20
    if _has_text_value(selected.get("risk_reward_comment")):
        score += 10
    if _as_text_list(item.get("pricing_warnings")):
        score -= min(20, len(_as_text_list(item.get("pricing_warnings"))) * 5)
    return score


def _compact_pricing_matrix_for_prompt(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for item in sorted(_pricing_matrix(ctx), key=_point_calc_item_rank, reverse=True)[:PROMPT_STOCK_LIMIT]:
        compact.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "asset_regime": item.get("asset_regime"),
            "data_status": item.get("data_status"),
            "selected_scenario": item.get("selected_scenario"),
            "scenarios": [
                {
                    "scenario_name": scenario.get("scenario_name"),
                    "action": scenario.get("action"),
                    "execution_mode": scenario.get("execution_mode"),
                    "condition": _truncate(str(scenario.get("condition") or ""), 180),
                    "entry_zone": _truncate(str(scenario.get("entry_zone") or ""), 140),
                    "stop_loss": _truncate(str(scenario.get("stop_loss") or ""), 140),
                    "failure_condition": _truncate(str(scenario.get("failure_condition") or ""), 160),
                    "risk_reward_comment": _truncate(str(scenario.get("risk_reward_comment") or ""), 180),
                    "constraints_used": _short_list(scenario.get("constraints_used"), PROMPT_CONSTRAINT_LIMIT),
                }
                for scenario in (item.get("scenarios") or [])[:PROMPT_SCENARIO_LIMIT]
                if isinstance(scenario, dict)
            ],
            "pricing_warnings": _short_list(item.get("pricing_warnings"), 3),
        })
    return compact


def _pricing_item_for_code(matrix: Sequence[Dict[str, Any]], code: Any) -> Optional[Dict[str, Any]]:
    code_text = str(code or "").strip()
    for item in matrix:
        if str(item.get("code") or "").strip() == code_text:
            return item
    return None


def _selected_pricing_scenario(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    selected = str(item.get("selected_scenario") or "")
    scenarios = item.get("scenarios") if isinstance(item.get("scenarios"), list) else []
    for scenario in scenarios:
        if isinstance(scenario, dict) and str(scenario.get("scenario_name") or "") == selected:
            return scenario
    for scenario in scenarios:
        if isinstance(scenario, dict) and scenario.get("execution_mode") in {"conditional_open", "immediate_open"}:
            return scenario
    return scenarios[0] if scenarios and isinstance(scenarios[0], dict) else None


def _deep_dive_results_for_meta(ctx: SelectionRunContext) -> List[Dict[str, Any]]:
    deep = ctx.stage_full("single_stock_deep_dive")
    results = deep.get("results") if isinstance(deep, dict) else []
    compact: List[Dict[str, Any]] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        full = item.get("full") if isinstance(item.get("full"), dict) else {}
        stock = full.get("stock") if isinstance(full.get("stock"), dict) else {}
        entry_quality = full.get("entry_quality") if isinstance(full.get("entry_quality"), dict) else {}
        code = str(summary.get("code") or stock.get("code") or "").strip()
        if not code:
            continue
        compact.append({
            "code": code,
            "name": summary.get("name") or stock.get("name") or code,
            "market": stock.get("market") or ctx.market,
            "action_bias": summary.get("action_bias") or full.get("action_bias"),
            "action_strength": summary.get("action_strength") or full.get("action_strength"),
            "quote_basis": summary.get("quote_basis") or full.get("quote_basis"),
            "ideal_entry_zone": summary.get("ideal_entry_zone") or entry_quality.get("ideal_entry_zone"),
            "no_chase_line": summary.get("no_chase_line") or entry_quality.get("no_chase_line"),
            "stop_loss": summary.get("stop_loss") or entry_quality.get("stop_loss"),
            "failure_condition": summary.get("failure_condition") or entry_quality.get("failure_condition"),
            "target_1": summary.get("target_1") or entry_quality.get("target_1"),
            "target_2": summary.get("target_2") or entry_quality.get("target_2"),
            "main_supporting_evidence": _short_list(summary.get("main_supporting_evidence"), 5),
            "main_risks": _short_list(summary.get("main_risks") or full.get("risk_flags"), 5),
            "main_missing_evidence": _short_list(summary.get("main_missing_evidence") or full.get("missing_evidence"), 5),
            "dimension_summary": _compact_dimension_summary(full.get("dimension_summary")),
        })
    return compact


def _desk_reports_for_meta(
    candidate_records: Dict[str, Dict[str, Any]],
    deep_results: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    reports: Dict[str, Any] = {}
    for item in deep_results:
        code = str(item.get("code") or "").strip()
        record = candidate_records.get(code) if code else None
        if not isinstance(record, dict):
            continue
        reports[code] = {
            "primary_desk": record.get("primary_desk"),
            "desks": _short_list(record.get("desks"), 5),
            "stance": record.get("stance"),
            "stance_by_desk": record.get("stance_by_desk") if isinstance(record.get("stance_by_desk"), dict) else {},
            "setup_type": record.get("setup_type"),
            "setup_subtype": record.get("setup_subtype"),
            "reason": record.get("reason"),
            "confidence": record.get("confidence"),
            "multi_desk_conviction": record.get("multi_desk_conviction"),
            "conflict_flags": _short_list(record.get("conflict_flags"), 8),
            "risks": _short_list(record.get("risks"), 8),
            "llm_expert_evidence": _compact_desk_evidence(record.get("llm_expert_evidence")),
        }
    return reports


def _fallback_meta_orchestrator(ctx: SelectionRunContext) -> Dict[str, Any]:
    deep_results = _deep_dive_results_for_meta(ctx)
    candidate_records = _candidate_records_by_code(ctx.stage_full("candidate_discovery"))
    market = _summarize_market_regime(ctx.market_regime)
    packages = []
    for item in deep_results:
        code = str(item.get("code") or "").strip()
        record = candidate_records.get(code, {}) if code else {}
        setup_type = str(record.get("setup_type") or "").strip()
        asset_regime = _asset_regime_from_setup(setup_type, item, market)
        invalidation_text = item.get("stop_loss") or item.get("failure_condition") or "跌破关键结构位则论点失效"
        mean_anchor_text = item.get("ideal_entry_zone") or item.get("no_chase_line") or "等待回踩确认"
        no_chase_text = item.get("no_chase_line") or "高于追高线且无回踩确认时禁止追高"
        package = {
            "stock": {"code": code, "name": item.get("name") or code, "market": item.get("market") or ctx.market},
            "meta_analysis": {
                "factual_consensus": _meta_factual_consensus(item, record),
                "strategic_divergence": _meta_divergence_text(record, item),
                "asset_regime": asset_regime,
                "dominant_thesis": record.get("primary_desk") or record.get("setup_type") or "unknown",
                "opposing_theses": _opposing_desks(record),
            },
            "market_context": {
                "market_regime": market.get("regime") or "unknown",
                "volatility_bucket": market.get("volatility_bucket") or "unknown",
                "risk_level": market.get("risk_level") or "unknown",
                "regime_weight_adjustment": _meta_regime_adjustment(market),
                "market_context_warnings": [] if market.get("status") == "ok" else ["市场环境数据不完整，Meta 结论降级为条件型。"],
            },
            "hard_constraints_for_pricing_agent": {
                "invalidation_level": {
                    "price": _first_number(invalidation_text),
                    "source": "single_stock_deep_dive",
                    "reason": str(invalidation_text),
                },
                "mean_reversion_anchor": {
                    "price": _first_number(mean_anchor_text),
                    "source": "single_stock_deep_dive",
                    "reason": str(mean_anchor_text),
                },
                "max_chase_premium": {
                    "value": "2.0%",
                    "source": "meta_fallback",
                    "reason": str(no_chase_text),
                },
                "risk_constraints": _meta_risk_constraints(record, item, market),
            },
            "required_pricing_scenarios": _default_required_pricing_scenarios(),
            "handoff_notes": {
                "for_pricing_agent": "不要重新判断股票好坏，只按 hard_constraints 与实时 ATR/盘口计算条件单。",
                "for_judge": "若点位计算层无法给出满足盈亏比的条件单，即使 Meta 定性偏正面也必须降级。",
            },
        }
        packages.append(package)
    return {
        "stage": "meta_orchestrator",
        "status": "partial" if packages else "insufficient_data",
        "summary": {
            "package_count": len(packages),
            "asset_regimes": [
                {"code": pkg["stock"]["code"], "asset_regime": pkg["meta_analysis"]["asset_regime"]}
                for pkg in packages
            ],
            "market_context_note": _meta_regime_adjustment(market),
            "main_constraints": _main_meta_constraints(packages),
        },
        "full": {
            "packages": packages,
            "tool_failures": [],
            "missing_evidence": [] if packages else ["single_stock_deep_dive"],
        },
        "full_ref": "meta_orchestrator.json",
    }


def _fallback_pricing_agent(ctx: SelectionRunContext) -> Dict[str, Any]:
    packages = _meta_packages(ctx)
    matrix = []
    for package in packages:
        stock = package.get("stock") if isinstance(package.get("stock"), dict) else {}
        constraints = package.get("hard_constraints_for_pricing_agent") if isinstance(package.get("hard_constraints_for_pricing_agent"), dict) else {}
        asset_regime = ((package.get("meta_analysis") or {}).get("asset_regime") if isinstance(package.get("meta_analysis"), dict) else "") or "Unknown"
        scenarios = []
        for scenario in package.get("required_pricing_scenarios") or _default_required_pricing_scenarios():
            if not isinstance(scenario, dict):
                continue
            name = str(scenario.get("scenario_name") or "Scenario")
            action, execution_mode = _pricing_action_for_scenario(name, asset_regime, ctx.market_regime)
            scenarios.append({
                "scenario_name": name,
                "condition": scenario.get("condition") or "等待条件触发",
                "action": action,
                "execution_mode": execution_mode,
                "entry_zone": _pricing_entry_zone(name, constraints),
                "stop_loss": _constraint_reason(constraints.get("invalidation_level")) or "跌破结构失效位",
                "failure_condition": _constraint_reason(constraints.get("invalidation_level")) or "右侧/低吸论点被证伪",
                "risk_reward_comment": "fallback 只生成条件型矩阵，需结合实时价/ATR 复核。",
                "constraints_used": _constraints_used(constraints),
            })
        matrix.append({
            "code": stock.get("code"),
            "name": stock.get("name"),
            "asset_regime": asset_regime,
            "data_status": "partial",
            "scenarios": scenarios,
            "selected_scenario": "Breakout_Continuation" if "Avoid" not in asset_regime else "Mean_Reversion_Pullback",
            "pricing_warnings": ["点位计算 fallback 未使用实时盘口/ATR，只保留条件型计划。"],
        })
    tradable_count = sum(
        1
        for item in matrix
        for scenario in item.get("scenarios", [])
        if scenario.get("execution_mode") in {"conditional_open", "immediate_open"}
    )
    return {
        "stage": "pricing_agent",
        "status": "partial" if matrix else "insufficient_data",
        "summary": {
            "priced_count": len(matrix),
            "tradable_count": tradable_count,
            "main_pricing_constraints": _main_pricing_constraints(matrix),
            "pricing_note": "点位计算 fallback 已生成 If-Then 条件矩阵；真实点位需结合实时价/ATR/盘口复核。",
        },
        "full": {
            "if_then_order_matrix": matrix,
            "constraints_echo": _main_meta_constraints(packages),
            "missing_evidence": ["realtime_orderbook", "atr"] if matrix else ["meta_orchestrator"],
        },
        "full_ref": "pricing_agent.json",
    }


def _fallback_portfolio_allocation(ctx: SelectionRunContext) -> Dict[str, Any]:
    deep = ctx.stage_full("single_stock_deep_dive")
    pricing_matrix = _pricing_matrix(ctx)
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
        pricing_item = _pricing_item_for_code(pricing_matrix, summary.get("code"))
        selected_scenario = _selected_pricing_scenario(pricing_item)
        plans.append({
            "rank": idx,
            "code": summary.get("code"),
            "name": summary.get("name"),
            "action": action,
            "action_strength": summary.get("action_strength") or "weak",
            "execution_mode": (selected_scenario or {}).get("execution_mode") or ("conditional_open" if action == "open" else "plain_wait"),
            "initial_position_pct": initial_pct,
            "initial_amount": (available_cash * initial_pct / 100) if isinstance(available_cash, (int, float)) else None,
            "entry_condition": (selected_scenario or {}).get("condition") or summary.get("ideal_entry_zone") or "等待确认",
            "add_condition": "突破后回踩不破再评估",
            "stop_loss_condition": (selected_scenario or {}).get("stop_loss") or summary.get("stop_loss") or "跌破关键支撑",
            "take_profit_condition": "到达第一压力位分批止盈",
            "review_trigger": "下一交易日开盘或关键价格触发",
            "auto_downgrade_rules": ["如果价格高于 no_chase_line，降级为 wait"],
            "reason": "来自单股深度分析和 Meta/点位计算条件矩阵的条件型计划。",
            "risk_flags": summary.get("main_risks") or [],
            "pricing_scenario": (selected_scenario or {}).get("scenario_name"),
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
            "execution_matrix": pricing_matrix,
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
        "balanced_candidate_evidence": ctx.stages.get("balanced_candidate_evidence", SelectionStage()).to_dict(include_full=True),
        "candidate_screening": ctx.stages.get("candidate_screening", SelectionStage()).to_dict(include_full=True),
        "single_stock_deep_dive": ctx.stages.get("single_stock_deep_dive", SelectionStage()).to_dict(include_full=True),
        "meta_orchestrator": ctx.stages.get("meta_orchestrator", SelectionStage()).to_dict(include_full=True),
        "pricing_agent": ctx.stages.get("pricing_agent", SelectionStage()).to_dict(include_full=True),
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


_DEEP_DIVE_POOL_FALLBACK_NOTE = "进入深挖：筛选未通过，按候选池顺序兜底"


def _mark_deep_dive_pool_fallback(payload: Dict[str, Any]) -> None:
    """Tag a deep-dive payload that only entered via candidate-pool order fallback.

    Why: 当筛选阶段没有显式放行足够标的时，深挖会按候选池顺序兜底补足，
    报告需要写明这一 provenance，避免读者误以为这些标的通过了筛选。
    """
    if not isinstance(payload, dict):
        return
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["deep_dive_provenance"] = "pool_fallback"
        summary["deep_dive_provenance_note"] = _DEEP_DIVE_POOL_FALLBACK_NOTE
    full = payload.get("full")
    if not isinstance(full, dict):
        full = {}
        payload["full"] = full
    full["deep_dive_provenance"] = "pool_fallback"
    full["deep_dive_provenance_note"] = _DEEP_DIVE_POOL_FALLBACK_NOTE


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


_DEEP_DIVE_SETUP_TYPES = {
    "trend_continuation",
    "early_turn",
    "theme_follow",
    "quality_repair",
    "capital_momentum",
    "unknown",
}

# 召回 source → setup_type 的过渡期映射（P1：上游尚未产出 setup_type 时使用）。
_SETUP_BY_SOURCE = {
    "low_base_structure": "early_turn",
    "limit_up_pool": "capital_momentum",
    "dragon_tiger": "capital_momentum",
    "capital_flow_anomaly": "capital_momentum",
    "hot_rank": "capital_momentum",
    "margin_financing": "capital_momentum",
    "block_trade": "capital_momentum",
    "northbound_stock_connect": "capital_momentum",
    "strong_sector": "theme_follow",
    "sector_theme": "theme_follow",
    "event_impact": "theme_follow",
    "news_momentum": "theme_follow",
    "fundamental_snapshot": "quality_repair",
    "valuation_liquidity": "quality_repair",
    "daily_screener": "trend_continuation",
    "alphasift": "trend_continuation",
    "sequoia": "trend_continuation",
}

# 关键词兜底：source 不具指向性时，从策略标签/理由维度文本里推断。
_SETUP_KEYWORDS = (
    ("early_turn", ("低位", "启动", "转强", "拐点", "超跌", "洗盘", "错杀", "low_base", "early_turn")),
    ("capital_momentum", ("涨停", "连板", "龙虎", "封板", "主力", "游资", "北向", "limit", "dragon", "moneyflow")),
    ("theme_follow", ("板块", "题材", "补涨", "概念", "sector", "theme")),
    ("quality_repair", ("业绩", "估值", "盈利", "景气", "基本面", "亏损收窄", "修复", "fundamental", "valuation")),
    ("trend_continuation", ("趋势", "突破", "均线", "放量", "强势", "海龟", "旗形", "rps", "breakout", "延续")),
)


def _candidate_records_by_code(full: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    candidates = full.get("candidates") if isinstance(full, dict) else []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code not in records:
            records[code] = item
    return records


def _infer_setup_type(candidate: Dict[str, Any]) -> str:
    """P1 过渡期推断：优先用上游显式 setup_type，否则由 source/策略标签推断，
    无法判断时返回 unknown（深挖落到保守通用 playbook）。"""
    explicit = str(candidate.get("setup_type") or "").strip().lower()
    if explicit in _DEEP_DIVE_SETUP_TYPES and explicit != "unknown":
        return explicit
    source = str(candidate.get("source") or "").strip().lower()
    mapped = _SETUP_BY_SOURCE.get(source)
    if mapped:
        return mapped
    text_parts: List[str] = []
    for key in ("strategy_tags", "matched_strategies", "recall_sources"):
        text_parts.extend(_as_text_list(candidate.get(key)))
    text_parts.append(str(candidate.get("reason") or ""))
    dims = candidate.get("reason_dimensions")
    if isinstance(dims, dict):
        text_parts.extend(str(v) for v in dims.keys())
    blob = " ".join(text_parts).lower()
    for setup, keywords in _SETUP_KEYWORDS:
        if any(kw in blob for kw in keywords):
            return setup
    return "unknown"


def _deep_dive_setup_fields(candidate: Optional[Dict[str, Any]], *, market: str) -> Dict[str, Any]:
    """Build the routing fields injected into the deep-dive payload (flag-on)."""
    candidate = candidate if isinstance(candidate, dict) else {}
    fields: Dict[str, Any] = {
        "setup_router_enabled": True,
        "setup_type": _infer_setup_type(candidate),
        "market": str(candidate.get("market") or market or "cn").strip().lower() or "cn",
    }
    subtype = str(candidate.get("setup_subtype") or "").strip().lower()
    if subtype:
        fields["setup_subtype"] = subtype
    for key in ("fact_sheet", "conflict_flags"):
        value = candidate.get(key)
        if value:
            fields[key] = value
    upstream = candidate.get("llm_expert_evidence")
    if upstream:
        fields["upstream_evidence"] = upstream
    return fields



def _deep_dive_targets(summary: Dict[str, Any], full: Dict[str, Any]) -> List[str]:
    shortlist = full.get("shortlist") if isinstance(full, dict) else []
    scored: List[tuple] = []
    for item in shortlist or []:
        if isinstance(item, dict) and item.get("screening_result") == "deep_dive" and item.get("code"):
            scored.append((item.get("score") or 0, str(item["code"])))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [code for _, code in scored]
    targets = summary.get("deep_dive_targets") if isinstance(summary, dict) else []
    if isinstance(targets, list) and targets:
        return [str(item) for item in targets if item]
    return []


def _monitor_targets(summary: Dict[str, Any], full: Dict[str, Any]) -> List[str]:
    shortlist = full.get("shortlist") if isinstance(full, dict) else []
    scored: List[tuple] = []
    for item in shortlist or []:
        if isinstance(item, dict) and item.get("screening_result") == "monitor" and item.get("code"):
            scored.append((item.get("score") or 0, str(item["code"])))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [code for _, code in scored]
    targets = summary.get("monitor_targets") if isinstance(summary, dict) else []
    if isinstance(targets, list) and targets:
        return [str(item) for item in targets if item]
    return []


def _select_deep_dive_targets(
    *,
    candidates: List[str],
    screening_summary: Dict[str, Any],
    screening_full: Dict[str, Any],
    limit: int,
) -> Tuple[List[str], Dict[str, str]]:
    """Choose deep-dive targets from screening results before pool order.

    Returns the ordered codes and a provenance map: ``screening`` for codes the
    screening stage explicitly routed to deep dive/monitor, ``pool_fallback`` for
    codes pulled in by candidate-pool order because screening surfaced too few.
    """
    normalized_limit = max(0, int(limit or 0))
    if normalized_limit <= 0:
        return [], {}

    rejected_codes = _screening_rejected_codes(screening_full)
    ordered: List[str] = []
    provenance: Dict[str, str] = {}

    def add(code: Any, source: str) -> bool:
        normalized = _normalize_stock_identity_code(code)
        if not normalized or normalized in rejected_codes or normalized in provenance:
            return False
        ordered.append(normalized)
        provenance[normalized] = source
        return len(ordered) >= normalized_limit

    for code in _deep_dive_targets(screening_summary, screening_full):
        if add(code, "screening"):
            return ordered, provenance

    for code in _monitor_targets(screening_summary, screening_full):
        if add(code, "screening"):
            return ordered, provenance

    for code in candidates:
        if add(code, "pool_fallback"):
            return ordered, provenance

    return ordered, provenance


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
    return max(1, min(5, value))


def _deep_dive_setup_router_enabled() -> bool:
    try:
        config = Config.get_instance()
        return bool(getattr(config, "agent_deep_dive_setup_router_enabled", True))
    except Exception:
        raw = os.getenv("AGENT_DEEP_DIVE_SETUP_ROUTER_ENABLED")
        if raw is None or str(raw).strip() == "":
            return True
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    try:
        value = float(str(raw).strip()) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _conditional_entry_score_min() -> float:
    return _env_float("AGENT_CONDITIONAL_ENTRY_SCORE_MIN", CONDITIONAL_ENTRY_SCORE_MIN_DEFAULT, minimum=0.0, maximum=100.0)


def _strong_watch_score_min() -> float:
    return _env_float("AGENT_STRONG_WATCH_SCORE_MIN", STRONG_WATCH_SCORE_MIN_DEFAULT, minimum=0.0, maximum=100.0)


def _no_chase_pct_default() -> float:
    return _env_float("AGENT_NO_CHASE_PCT_DEFAULT", NO_CHASE_PCT_DEFAULT, minimum=0.0, maximum=30.0)


def _conditional_entry_min_strength() -> str:
    raw = os.getenv("AGENT_CONDITIONAL_ENTRY_MIN_STRENGTH", CONDITIONAL_ENTRY_MIN_STRENGTH_DEFAULT)
    value = str(raw or CONDITIONAL_ENTRY_MIN_STRENGTH_DEFAULT).strip().lower()
    return value if value in ACTION_STRENGTH_RANK else CONDITIONAL_ENTRY_MIN_STRENGTH_DEFAULT


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
    for stage_name in (
        "candidate_discovery",
        "candidate_screening",
        "single_stock_deep_dive",
        "meta_orchestrator",
        "pricing_agent",
        "portfolio_allocation",
        "adversarial_review",
    ):
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
        return f"{verdict}，最终动作 {action}；详见辅助审查摘要。"
    if verdict:
        return f"{verdict}；详见辅助审查摘要。"
    if action:
        return f"最终动作 {action}；详见辅助审查摘要。"
    return "-"


def _meta_report_note(summary: Dict[str, Any]) -> str:
    if not isinstance(summary, dict) or not summary:
        return "-"
    regimes = summary.get("asset_regimes") if isinstance(summary.get("asset_regimes"), list) else []
    labels = []
    for item in regimes[:3]:
        if isinstance(item, dict):
            labels.append(f"{item.get('code')}: {item.get('asset_regime')}")
    constraint_count = len(summary.get("main_constraints") or []) if isinstance(summary.get("main_constraints"), list) else 0
    note = summary.get("market_context_note") or ""
    prefix = "；".join(labels) if labels else f"约束包 {summary.get('package_count', 0)} 个"
    return f"{prefix}；硬约束 {constraint_count} 条；{note}".strip("；")


def _pricing_report_note(summary: Dict[str, Any]) -> str:
    if not isinstance(summary, dict) or not summary:
        return "-"
    return (
        f"已定价 {summary.get('priced_count', 0)} 只；"
        f"可条件执行场景 {summary.get('tradable_count', 0)} 个；"
        f"{summary.get('pricing_note') or ''}"
    ).strip("；")


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
            "auction_trigger": summary.get("auction_trigger") or entry_quality.get("auction_trigger"),
            "breakout_trigger": summary.get("breakout_trigger") or entry_quality.get("breakout_trigger"),
            "pullback_trigger": summary.get("pullback_trigger") or entry_quality.get("pullback_trigger"),
            "no_chase_line": summary.get("no_chase_line") or entry_quality.get("no_chase_line"),
            "stop_loss": summary.get("stop_loss") or entry_quality.get("stop_loss"),
            "failure_condition": summary.get("failure_condition") or entry_quality.get("failure_condition"),
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
            "execution_mode": plan.get("execution_mode") or target.get("execution_mode"),
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
    for item in items:
        item["execution_mode"] = _execution_mode(item)
        if item["execution_mode"] == "conditional_open" and not item.get("no_chase_line"):
            item["no_chase_line"] = _default_no_chase_condition()
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
    mode = _execution_mode(item)
    if mode == "immediate_open":
        return _action_rank(item.get("action"))
    if mode == "conditional_open":
        return 1
    if mode == "strong_watch":
        return 12
    if _is_observable_item(item):
        return 20 + _action_rank(item.get("action"))
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


def _strength_at_least(value: Any, minimum: str) -> bool:
    strength = str(value or "").strip().lower()
    return ACTION_STRENGTH_RANK.get(strength, 0) >= ACTION_STRENGTH_RANK.get(minimum, ACTION_STRENGTH_RANK["medium"])


def _has_text_value(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip()
        return bool(text and text not in {"-", "无", "暂无", "null", "None"})
    return value not in (None, "")


def _has_entry_condition(item: Dict[str, Any]) -> bool:
    return any(
        _has_text_value(item.get(key))
        for key in (
            "entry_condition",
            "ideal_entry_zone",
            "auction_trigger",
            "breakout_trigger",
            "pullback_trigger",
        )
    )


def _has_exit_condition(item: Dict[str, Any]) -> bool:
    return (
        any(_has_text_value(item.get(key)) for key in ("stop_loss_condition", "stop_loss", "failure_condition"))
        or bool(_as_text_list(item.get("failure_conditions")))
    )


def _has_consensus_signal(item: Dict[str, Any]) -> bool:
    labels = " ".join(_as_text_list(item.get("candidate_labels"))).lower()
    source = str(item.get("candidate_source") or "").lower()
    reason = str(item.get("candidate_reason") or item.get("reason") or "").lower()
    return any(marker in text for text in (labels, source, reason) for marker in ("共振", "多专家", "多源", "multi"))


def _has_hard_bearish_risk(item: Dict[str, Any]) -> bool:
    values: List[str] = []
    for key in ("risk_flags", "failure_conditions", "missing_evidence"):
        values.extend(_as_text_list(item.get(key)))
    for key in ("plan_reason", "reason", "candidate_reason", "candidate_source"):
        if item.get(key):
            values.append(str(item.get(key)))
    text = "；".join(values).lower()
    if re.search(r"(?<![a-z0-9])\*?st(?![a-z0-9])", text, flags=re.IGNORECASE):
        return True
    return any(marker.lower() in text for marker in HARD_BEARISH_RISK_MARKERS)


def _is_conditional_entry_item(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action != "wait":
        return False
    if not _strength_at_least(item.get("action_strength"), _conditional_entry_min_strength()):
        return False
    score = _safe_float(item.get("candidate_score"))
    if score < _conditional_entry_score_min() and not (score >= _strong_watch_score_min() and _has_consensus_signal(item)):
        return False
    if not _has_entry_condition(item):
        return False
    if not _has_exit_condition(item):
        return False
    if _has_hard_bearish_risk(item):
        return False
    return True


def _is_strong_watch_item(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid"}:
        return False
    if _has_hard_bearish_risk(item):
        return False
    score = _safe_float(item.get("candidate_score"))
    return score >= _strong_watch_score_min() or _has_consensus_signal(item)


def _execution_mode(item: Dict[str, Any]) -> str:
    explicit = str(item.get("execution_mode") or "").strip().lower()
    if explicit in {"immediate_open", "conditional_open", "strong_watch", "plain_wait", "reject"}:
        return explicit
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid"}:
        return "reject"
    if _is_actionable_recommendation(item):
        return "immediate_open" if action in {"open", "buy"} else "conditional_open"
    if _is_conditional_entry_item(item):
        return "conditional_open"
    if _is_strong_watch_item(item):
        return "strong_watch"
    return "plain_wait"


def _default_no_chase_condition() -> str:
    return f"高开超过默认阈值 {_format_score(_no_chase_pct_default())}% 且无回踩确认不追；缩量冲高或板块分化不追。"


def _is_observable_item(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid"}:
        return False
    return bool(item.get("has_candidate") or item.get("has_deep_dive") or item.get("has_plan"))


def _has_opportunity_hard_exclusion(item: Dict[str, Any]) -> bool:
    values: List[str] = []
    for key in ("risk_flags", "missing_evidence"):
        values.extend(_as_text_list(item.get(key)))
    for key in ("plan_reason", "reason", "candidate_reason", "candidate_source"):
        if item.get(key):
            values.append(str(item.get(key)))
    text = "；".join(values).lower()
    if re.search(r"(?<![a-z0-9])\*?st(?![a-z0-9])", text, flags=re.IGNORECASE):
        return True
    return any(marker.lower() in text for marker in OPPORTUNITY_HARD_EXCLUSION_MARKERS)


def _is_headline_opportunity_item(item: Dict[str, Any]) -> bool:
    action = str(item.get("action") or "").strip().lower()
    if action in {"reject", "avoid"}:
        return False
    if _has_opportunity_hard_exclusion(item):
        return False
    mode = _execution_mode(item)
    if mode in {"immediate_open", "conditional_open", "strong_watch"}:
        return True
    if not item.get("has_deep_dive"):
        return False
    if not _strength_at_least(item.get("action_strength"), "medium"):
        return False
    return bool(item.get("supporting_evidence") or item.get("reason") or _has_entry_condition(item))


def _headline_opportunity_items(*, displayed_recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in displayed_recommendations if _is_headline_opportunity_item(item)]


def _headline_watch_items(
    *,
    displayed_recommendations: List[Dict[str, Any]],
    primary_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep the conclusion table aligned with the body recommendation list."""

    if not displayed_recommendations:
        return []
    primary_code = (
        _normalize_stock_identity_code(primary_items[0].get("code"))
        if primary_items and isinstance(primary_items[0], dict)
        else None
    )
    items: List[Dict[str, Any]] = []
    for item in displayed_recommendations:
        code = _normalize_stock_identity_code(item.get("code"))
        if primary_code and code == primary_code:
            continue
        if _is_observable_item(item):
            items.append(item)
    return items


def _opportunity_candidate_label(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "暂无高质量机会标的"
    return _stock_label(items[0])


def _execution_candidate_label(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "暂无可执行标的"
    label = _stock_label(items[0])
    mode = _execution_mode(items[0])
    if mode == "conditional_open":
        return f"{label}（条件触发）"
    if mode == "immediate_open":
        return f"{label}（可执行）"
    return label


def _watch_candidate_labels(items: List[Dict[str, Any]]) -> str:
    labels = [_stock_label(item) for item in items[:4]]
    return "、".join(labels) if labels else "-"


def _observation_status(item: Dict[str, Any]) -> str:
    mode = _execution_mode(item)
    if mode == "conditional_open":
        return "条件入场"
    if mode == "strong_watch":
        return "强观察"
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
    action = str(item.get("action") or "").strip().lower()
    mode = _execution_mode(item)
    if mode == "immediate_open":
        medal = {1: "🥇 首选", 2: "🥈 次选", 3: "🥉 观察"}.get(rank, f"第 {rank} 顺位")
    elif mode == "conditional_open":
        medal = f"⚡ 条件入场 {rank}"
    elif mode == "strong_watch":
        medal = f"👀 强观察 {rank}"
    elif action in {"reject", "avoid"}:
        medal = f"⛔ 排除 {rank}"
    elif action in {"monitor", "watch"}:
        medal = f"👀 观察 {rank}"
    else:
        medal = f"⏳ 等待确认 {rank}"
    score = item.get("candidate_score")
    title_suffix = f"（候选分 {_format_score(score)}）" if score is not None else ""
    lines = [
        f"### {medal}：{_markdown_cell(_stock_label(item))}{title_suffix}",
        "",
    ]
    if mode == "conditional_open":
        lines.extend(_render_conditional_entry_table(item))
    elif mode == "strong_watch":
        lines.extend(_render_strong_watch_table(item))
    else:
        actionable = mode == "immediate_open" or _is_actionable_recommendation(item)
        lines.extend(_render_standard_recommendation_table(item, actionable=actionable))
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


def _render_standard_recommendation_table(item: Dict[str, Any], *, actionable: bool) -> List[str]:
    rows: List[str] = [
        "| 项目 | 决策 |",
        "| --- | --- |",
        f"| 入场结论 | {_markdown_cell(item.get('action') or 'wait')} |",
        f"| 动作强度 | {_markdown_cell(item.get('action_strength') or '-')} |",
        f"| 行情口径 | {_markdown_cell(_quote_basis_label(item.get('quote_basis')))} |",
    ]
    if actionable:
        rows.append(
            f"| 理想入场区间 | {_markdown_cell(item.get('ideal_entry_zone') or item.get('entry_condition') or '等待回踩或突破确认')} |"
        )
    else:
        wait_entry = item.get("ideal_entry_zone") or item.get("entry_condition")
        if _has_meaningful_wait_field(wait_entry):
            rows.append(f"| 关注条件 | {_markdown_cell(wait_entry)} |")

    # Only show these rows when actionable or when they have real content.
    _optional_rows: List[tuple[str, str]] = [
        ("次优入场区间", (item.get("secondary_entry_zone") or "-") if actionable else "-"),
        ("禁止追高线", (item.get("no_chase_line") or "-") if actionable else "-"),
    ]
    for label, value in _optional_rows:
        if value != "-":
            rows.append(f"| {label} | {_markdown_cell(value)} |")

    if actionable:
        rows.append(f"| 首仓比例 | {_markdown_cell(_format_pct(item.get('initial_position_pct')))} |")
        add_condition = item.get("add_condition") or "-"
        if _has_meaningful_wait_field(add_condition):
            rows.append(f"| 加仓条件 | {_markdown_cell(add_condition)} |")
        stop_loss = item.get("stop_loss") or item.get("stop_loss_condition") or "-"
        if _has_meaningful_wait_field(stop_loss):
            rows.append(f"| 止损位 | {_markdown_cell(stop_loss)} |")
    else:
        wait_stop = item.get("stop_loss") or item.get("stop_loss_condition") or item.get("failure_condition")
        if _has_meaningful_wait_field(wait_stop):
            rows.append(f"| 失效条件 | {_markdown_cell(wait_stop)} |")

    _target_rows: List[tuple[str, str]] = [
        ("第一目标位", (item.get("target_1") or item.get("take_profit_condition") or "-") if actionable else "-"),
        ("第二目标位", (item.get("target_2") or "-") if actionable else "-"),
    ]
    for label, value in _target_rows:
        if value != "-":
            rows.append(f"| {label} | {_markdown_cell(value)} |")

    rows.append(f"| 复查触发 | {_markdown_cell(item.get('review_trigger') or '每日盘后复查技术趋势状态、资金流向及关键风险变化。')} |")
    rows.append("")
    return rows


def _has_meaningful_wait_field(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or text in {"-", "无", "暂无", "None", "null"}:
        return False
    low_signal_templates = (
        "当前不形成可执行买点",
        "仅保留观察条件",
        "当前不建议加仓",
        "等待趋势、资金和基本面缺口改善后再评估",
        "未触发建仓，不设置交易止损",
        "仅跟踪关键支撑是否失守",
    )
    return not any(template in text for template in low_signal_templates)


def _render_conditional_entry_table(item: Dict[str, Any]) -> List[str]:
    return [
        "| 项目 | 决策 |",
        "| --- | --- |",
        "| 看盘动作 | 条件入场，不是无条件追买 |",
        f"| 动作强度 | {_markdown_cell(item.get('action_strength') or '-')} |",
        f"| 行情口径 | {_markdown_cell(_quote_basis_label(item.get('quote_basis')))} |",
        f"| 明日触发条件 | {_markdown_cell(_conditional_entry_trigger(item))} |",
        f"| 入场区间 | {_markdown_cell(item.get('entry_condition') or item.get('ideal_entry_zone') or item.get('pullback_trigger') or '缺失：未给出可执行入场区间')} |",
        f"| 止盈目标 | {_markdown_cell(_take_profit_condition_text(item))} |",
        f"| 止损位 | {_markdown_cell(item.get('stop_loss_condition') or item.get('stop_loss') or '缺失：未给出止损位')} |",
        f"| 可试探仓位 | {_markdown_cell(_conditional_position_text(item))} |",
        f"| 禁止追高 | {_markdown_cell(item.get('no_chase_line') or _default_no_chase_condition())} |",
        f"| 失效条件 | {_markdown_cell(_failure_condition_text(item))} |",
        f"| 加仓条件 | {_markdown_cell(item.get('add_condition') or '首仓后量价继续确认、回踩不破且板块同步走强，再按账户上限评估。')} |",
        f"| 复查触发 | {_markdown_cell(item.get('review_trigger') or '次日竞价、开盘 15 分钟、收盘后各复查一次。')} |",
        "",
    ]


def _render_strong_watch_table(item: Dict[str, Any]) -> List[str]:
    return [
        "| 项目 | 决策 |",
        "| --- | --- |",
        "| 看盘动作 | 强观察，暂不形成交易脚本 |",
        f"| 动作强度 | {_markdown_cell(item.get('action_strength') or '-')} |",
        f"| 候选分 | {_markdown_cell(_format_score(item.get('candidate_score')) if item.get('candidate_score') is not None else '-')} |",
        f"| 不能直接入场原因 | {_markdown_cell(_strong_watch_blocker(item))} |",
        f"| 明日重点观察 | {_markdown_cell(_strong_watch_focus(item))} |",
        f"| 触发升级条件 | {_markdown_cell(_strong_watch_upgrade_condition(item))} |",
        f"| 作废条件 | {_markdown_cell(_failure_condition_text(item))} |",
        "",
    ]


def _conditional_entry_trigger(item: Dict[str, Any]) -> str:
    triggers = [
        item.get("entry_condition"),
        item.get("auction_trigger"),
        item.get("breakout_trigger"),
        item.get("pullback_trigger"),
        item.get("ideal_entry_zone"),
    ]
    text = "；".join(str(value).strip() for value in triggers if _has_text_value(value))
    return text or "等待竞价承接、放量突破或回踩不破后再小仓试探。"


def _conditional_position_text(item: Dict[str, Any]) -> str:
    pct = item.get("initial_position_pct")
    pct_num = _safe_float(pct)
    if pct_num > 0:
        return _format_pct(pct)
    return "5%-10% 试探仓，需受账户现金与单票上限约束；账户缺失时不写确定仓位。"


def _take_profit_condition_text(item: Dict[str, Any]) -> str:
    if _has_text_value(item.get("take_profit_condition")):
        return str(item.get("take_profit_condition"))
    values = [
        item.get("target_1"),
        item.get("target_2"),
    ]
    targets = [str(value).strip() for value in values if _has_text_value(value)]
    return " / ".join(targets) if targets else "缺失：未给出止盈目标"


def _failure_condition_text(item: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in ("failure_condition", "stop_loss_condition", "stop_loss"):
        value = item.get(key)
        if _has_text_value(value):
            values.append(str(value).strip())
    values.extend(_as_text_list(item.get("failure_conditions"))[:3])
    return "；".join(list(dict.fromkeys(value for value in values if value))) or "跌破关键支撑、资金转弱、板块退潮或出现重大利空。"


def _strong_watch_blocker(item: Dict[str, Any]) -> str:
    missing = _as_text_list(item.get("missing_evidence"))
    risks = _as_text_list(item.get("risk_flags"))
    blockers = missing[:2] or risks[:2]
    if blockers:
        return "；".join(blockers)
    if not _has_exit_condition(item):
        return "缺少止损或失效条件，不能升级为条件入场。"
    if not _has_entry_condition(item):
        return "缺少明确触发条件，不能升级为条件入场。"
    return "证据仍未满足直接建仓要求。"


def _strong_watch_focus(item: Dict[str, Any]) -> str:
    labels = _as_text_list(item.get("candidate_labels"))
    focus = labels[:3] if labels else ["竞价承接", "板块强度", "成交量", "资金流"]
    return "、".join(focus)


def _strong_watch_upgrade_condition(item: Dict[str, Any]) -> str:
    if _has_entry_condition(item) and not _has_exit_condition(item):
        return "补足止损或失效条件后，可升级为条件入场。"
    if _has_exit_condition(item) and not _has_entry_condition(item):
        return "形成明确竞价、突破或回踩触发后，可升级为条件入场。"
    return "资金承接、技术触发和失效条件同时补齐后，可升级为条件入场。"


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
    if text == "capital_flow:limit_up_pool":
        return "涨停资金活跃"
    if text == "capital_flow:popularity_rank":
        return "人气资金关注"
    if text == "capital_flow:hot_money_activity":
        return "游资/龙虎榜活跃"
    if text == "capital_flow:multi_source":
        return "多资金来源共振"
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


_KV_PATTERN = re.compile(
    r"(?:估值|value)\s*=\s*([\d.]+)\s*[;；]\s*threshold\s*=\s*([\d.]+)\s*[;；]\s*deviation\s*=\s*([-\d.eE]+)"
)
_DICT_PATTERN = re.compile(r"\{['\"](?:card_id|dimension|interpretation)['\"].*?\}")
_RAW_METRIC_DICT = re.compile(r"估值\s*=\s*\{[^}]+\}(?:\s*[;；]\s*threshold\s*=\s*\{[^}]*\})?")


def _humanize_kv_patterns(text: str) -> str:
    """Replace residual value=X；threshold=Y；deviation=Z and raw dict patterns in report text."""
    def _replace_kv(m: re.Match) -> str:
        v, t, d = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return f"当前值{v:.1f}（阈值{t:.1f}，偏离{d:+.1f}%）"

    text = _KV_PATTERN.sub(_replace_kv, text)

    def _replace_dict(m: re.Match) -> str:
        try:
            import ast
            d = ast.literal_eval(m.group(0))
            if isinstance(d, dict):
                return str(d.get("interpretation") or d.get("detail") or d.get("summary") or m.group(0))
        except Exception:
            pass
        return m.group(0)

    text = _DICT_PATTERN.sub(_replace_dict, text)
    text = _RAW_METRIC_DICT.sub("基本面指标", text)
    return text


def _translate_report_text(value: Any) -> str:
    if isinstance(value, dict):
        text = str(
            value.get("interpretation")
            or value.get("detail")
            or value.get("summary")
            or value.get("text")
            or value.get("reason")
            or ""
        )
    else:
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
    text = _humanize_kv_patterns(text)
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
        result: List[str] = []
        for item in value:
            if not item:
                continue
            if isinstance(item, dict):
                text = (
                    item.get("interpretation")
                    or item.get("detail")
                    or item.get("summary")
                    or item.get("text")
                    or item.get("reason")
                )
                if text:
                    result.append(str(text))
                    continue
            result.append(str(item))
        return result
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
    if isinstance(value, dict):
        value = (
            value.get("interpretation")
            or value.get("detail")
            or value.get("summary")
            or value.get("text")
            or str(value)
        )
    text = str(value if value is not None else "-")
    text = _humanize_kv_patterns(text)
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
