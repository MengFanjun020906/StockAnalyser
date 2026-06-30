#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke-run the real stock-selection pipeline through Judge sanity.

This script uses the production ToolRegistry and AgentExecutor staging path.
Only LLM responses are deterministic local JSON so the smoke does not depend on
an external model being available.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.executor import AgentExecutor
from src.agent.factory import get_tool_registry
from src.agent.llm_adapter import LLMResponse
from src.agent.llm_telemetry import llm_telemetry_scope, record_llm_telemetry
from src.schemas.agent_context import (
    AccountContext,
    AgentUserContext,
    InvestorProfile,
    ReportContext,
)

SMOKE_SYMBOL = "600519"
SMOKE_NAME = "贵州茅台"


class SmokeLLMAdapter:
    """Deterministic LLM adapter for a real pipeline smoke run."""

    def __init__(self) -> None:
        self.call_text_stages: List[str] = []
        self._stage_index = 0
        self.call_with_tools_count = 0

    def call_text(self, messages: List[Dict[str, Any]], timeout: float | None = None) -> LLMResponse:
        payload = self._next_stage_payload()
        self.call_text_stages.append(payload["stage"])
        response = LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            usage={"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
            provider="smoke",
            model="local-json",
        )
        record_llm_telemetry(
            model=response.model,
            provider=response.provider,
            usage=response.usage,
            latency_ms=0,
            tool_calls=0,
            ok=True,
        )
        return response

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        timeout: float | None = None,
        response_format: Dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> LLMResponse:
        self.call_with_tools_count += 1
        # Committee desks receive this no-tool final JSON. It is enough to
        # exercise committee aggregation and the downstream selection pipeline.
        payload = {
            "candidates": [
                {
                    "code": SMOKE_SYMBOL,
                    "name": SMOKE_NAME,
                    "market": "cn",
                    "score": 88,
                    "confidence": 0.8,
                    "stance": "support",
                    "setup_type": "trend_continuation",
                    "reason": "smoke 固定候选，用于验证真实 pipeline 的 sanity 接入。",
                    "evidence": [
                        {"tool": "seed_fact_packet", "summary": "seed fact 已由真实链路构建。"}
                    ],
                    "risks": [{"type": "smoke", "summary": "仅用于本地 smoke。"}],
                }
            ],
            "data_quality": {"freshness": "partial"},
        }
        response = LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            tool_calls=[],
            usage={"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
            provider="smoke",
            model="local-json",
        )
        record_llm_telemetry(
            model=response.model,
            provider=response.provider,
            usage=response.usage,
            latency_ms=0,
            tool_calls=len(response.tool_calls),
            ok=True,
        )
        return response

    def _next_stage_payload(self) -> Dict[str, Any]:
        stages = [
            self._candidate_screening_payload,
            self._single_stock_deep_dive_payload,
            self._meta_orchestrator_payload,
            self._pricing_agent_payload,
            self._portfolio_allocation_payload,
            self._adversarial_review_payload,
            self._judge_decision_payload,
        ]
        if self._stage_index >= len(stages):
            raise AssertionError(f"unexpected extra LLM stage call: {self._stage_index + 1}")
        builder = stages[self._stage_index]
        self._stage_index += 1
        return builder()

    def _candidate_screening_payload(self) -> Dict[str, Any]:
        return {
            "stage": "candidate_screening",
            "status": "ok",
            "summary": {"deep_dive_targets": [SMOKE_SYMBOL], "monitor_targets": [], "rejected_targets": []},
            "full": {
                "shortlist": [
                    {"code": SMOKE_SYMBOL, "name": SMOKE_NAME, "screening_result": "deep_dive", "score": 88}
                ]
            },
        }

    def _single_stock_deep_dive_payload(self) -> Dict[str, Any]:
        return {
            "stage": "single_stock_deep_dive",
            "status": "ok",
            "summary": {
                "code": SMOKE_SYMBOL,
                "name": SMOKE_NAME,
                "action_bias": "open",
                "action_strength": "medium",
                "main_supporting_evidence": ["真实 pipeline smoke 候选进入深挖。"],
            },
            "full": {
                "stock": {"code": SMOKE_SYMBOL, "name": SMOKE_NAME, "market": "cn"},
                "missing_evidence": [],
            },
        }

    def _meta_orchestrator_payload(self) -> Dict[str, Any]:
        return {
            "stage": "meta_orchestrator",
            "status": "ok",
            "summary": {"selected_assets": [SMOKE_SYMBOL], "main_constraints": []},
            "full": {"constraint_packages": [{"code": SMOKE_SYMBOL, "asset_regime": "trend_continuation"}]},
        }

    def _pricing_agent_payload(self) -> Dict[str, Any]:
        return {
            "stage": "pricing_agent",
            "status": "ok",
            "summary": {"priced_count": 1, "tradable_count": 1, "main_pricing_constraints": []},
            "full": {
                "if_then_order_matrix": [
                    {
                        "code": SMOKE_SYMBOL,
                        "name": SMOKE_NAME,
                        "selected_scenario": "Breakout_Continuation",
                        "scenarios": [
                            {
                                "scenario_name": "Breakout_Continuation",
                                "condition": "突破确认",
                                "action": "open",
                                "execution_mode": "immediate_open",
                                "entry_zone": "10.0-10.2",
                                "stop_loss": "跌破 9.5",
                                "failure_condition": "放量跌破 9.5",
                            }
                        ],
                    }
                ]
            },
        }

    def _portfolio_allocation_payload(self) -> Dict[str, Any]:
        return {
            "stage": "portfolio_allocation",
            "status": "ok",
            "summary": {
                "portfolio_action": "open",
                "recommended_position_count": 1,
                "initial_total_position_pct": 35,
                "reserved_cash_pct": 65,
                "core_reason": "故意给出超上限首仓，用于触发 Judge sanity 截断。",
                "positions_plan_brief": [{"code": SMOKE_SYMBOL, "action": "open", "initial_position_pct": 35}],
            },
            "full": {
                "positions_plan": [
                    {
                        "rank": 1,
                        "code": SMOKE_SYMBOL,
                        "name": SMOKE_NAME,
                        "action": "open",
                        "initial_position_pct": 35,
                        "initial_amount": 17500,
                        "entry_condition": "突破确认",
                        "stop_loss_condition": "跌破 9.5",
                        "review_trigger": "下一交易日",
                    }
                ],
                "risk_controls": [],
            },
        }

    def _adversarial_review_payload(self) -> Dict[str, Any]:
        return {
            "stage": "adversarial_review",
            "status": "ok",
            "summary": {"opposing_summary": "仓位过高，需要截断。", "recommended_verdict": "accept_with_changes"},
            "full": {"top_risk_points": ["单票首仓过高"]},
        }

    def _judge_decision_payload(self) -> Dict[str, Any]:
        return {
            "stage": "judge_decision",
            "status": "ok",
            "summary": {
                "primary_plan_verdict": "accept",
                "final_action": "open",
                "decision_summary": "原始 Judge 接受开仓。",
                "next_step": "render_final_report",
            },
            "full": {"winner": "primary"},
        }


def _context() -> AgentUserContext:
    return AgentUserContext(
        investor=InvestorProfile(
            risk_preference="balanced",
            trading_horizon="medium_term",
            max_single_position_pct=15,
            max_total_equity_exposure_pct=80,
            default_stop_loss_pct=8,
        ),
        accounts=[
            AccountContext(
                account_id=1,
                account_name="smoke",
                total_equity=50000,
                available_cash=50000,
                total_market_value=0,
            )
        ],
        report=ReportContext(
            analysis_mode="planning_execute",
            intent="watchlist_scan",
            include_watchlist_ranking=True,
            target_symbols=[SMOKE_SYMBOL],
        ),
    )


def main() -> int:
    os.environ.setdefault("AGENT_SEED_POOL_TOTAL_LIMIT", "1")
    os.environ.setdefault("AGENT_SEED_FACT_TOOLS", "get_stock_business_context")
    os.environ.setdefault("AGENT_SEED_FACT_MAX_WORKERS", "1")
    os.environ.setdefault("AGENT_SEED_FACT_TOOL_TIMEOUT_SECONDS", "3")

    adapter = SmokeLLMAdapter()
    registry = get_tool_registry()
    assert len(registry.list_names()) >= 50, "expected production ToolRegistry"

    executor = AgentExecutor(registry, adapter, max_steps=1, timeout_seconds=30)
    events: List[Dict[str, Any]] = []
    smoke_dir = Path(os.getenv("DSA_SMOKE_OUTPUT_DIR", tempfile.gettempdir())).expanduser()
    out_path = smoke_dir / "dsa_judge_sanity_pipeline_smoke.json"
    telemetry_dir = smoke_dir / "dsa_judge_sanity_pipeline_smoke_trace"
    telemetry_path = telemetry_dir / "llm_usage.jsonl"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    if telemetry_path.exists():
        telemetry_path.unlink()
    with llm_telemetry_scope(
        trace_id="smoke-judge-sanity-pipeline",
        artifact_dir=str(telemetry_dir),
    ):
        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="我现在有5w元，帮我从候选里选一只下周可关注的股票",
            context={
                "session_id": "smoke-judge-sanity-pipeline",
                "agent_user_context": _context(),
            },
            progress_callback=lambda event: events.append(event),
        )

    assert result.success is True, result.error
    assert result.stock_selection["success"] is True, result.stock_selection.get("error")
    report = result.stock_selection["final_report_json"]
    judge = report["judge_decision"]
    allocation = report["portfolio_allocation"]
    checks = judge["full"].get("sanity_checks") or []
    plan = allocation["full"]["positions_plan"][0]

    assert report["candidate_discovery"]["full"]["candidate_count"] >= 1
    assert [event["type"] for event in events if event.get("type") == "selection_judge_done"]
    telemetry_events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(telemetry_events) >= 7
    assert {event["stage"] for event in telemetry_events} >= {
        "candidate_screening",
        "single_stock_deep_dive",
        "meta_orchestrator",
        "pricing_agent",
        "portfolio_allocation",
        "adversarial_review",
        "judge_decision",
    }

    rule_ids = {check.get("rule_id") for check in checks}
    assert rule_ids, "Judge sanity did not leave audit checks"
    assert rule_ids & {
        "max_single_position_clamp",
        "risk_defense_downgrade",
        "risk_defense_already_wait",
        "open_without_position_plan",
    }, f"unexpected Judge sanity rules: {sorted(str(rule) for rule in rule_ids)}"

    if "max_single_position_clamp" in rule_ids:
        assert plan["initial_position_pct"] <= 15
        assert allocation["summary"]["initial_total_position_pct"] <= 15

    if {"risk_defense_downgrade", "risk_defense_already_wait", "open_without_position_plan"} & rule_ids:
        assert judge["summary"]["final_action"] == "wait"

    output = {
        "success": result.success,
        "tool_count": len(registry.list_names()),
        "llm_call_text_stages": adapter.call_text_stages,
        "committee_llm_calls": adapter.call_with_tools_count,
        "events": [event.get("type") for event in events],
        "candidate_count": report["candidate_discovery"]["full"]["candidate_count"],
        "llm_telemetry_count": len(telemetry_events),
        "llm_telemetry_path": str(telemetry_path),
        "sanity_checks": checks,
        "allocation_plan": plan,
        "judge_summary": judge["summary"],
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"smoke_output={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
