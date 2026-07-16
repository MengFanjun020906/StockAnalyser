# -*- coding: utf-8 -*-
"""
Tests for AgentExecutor with mocked LLM adapter.

Covers:
- ReAct loop: tool-calling → result feedback → final answer
- Dashboard JSON parsing (markdown blocks, raw JSON, json_repair)
- Max step limit
- Tool execution error handling
- _serialize_tool_result for various types
- _build_user_message formatting
"""

import json
import time
import unittest
import sys
import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Keep this test runnable when optional LLM runtime deps are not installed.
try:
    import litellm  # noqa: F401
except Exception:
    sys.modules["litellm"] = MagicMock()
try:
    import json_repair  # noqa: F401
except ModuleNotFoundError:
    json_repair_stub = MagicMock()
    json_repair_stub.repair_json.side_effect = lambda content, **_kwargs: content
    sys.modules["json_repair"] = json_repair_stub

from src.agent.executor import AgentExecutor, AgentResult, _build_context_tool_argument_guard
from src.agent.llm_adapter import LLMResponse, ToolCall
from src.agent.runner import (
    _is_failed_tool_result,
    compact_tool_result_for_model,
    parse_dashboard_json,
    resolve_tool_etl_profile,
    run_agent_loop,
    serialize_tool_result,
)
from src.agent.metric_semantics import registered_metric_semantics
from src.agent.tools.registry import ToolRegistry, ToolDefinition, ToolParameter
from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext


# ============================================================
# Helpers
# ============================================================

def _make_registry_with_echo():
    """Create a registry with a simple echo tool."""
    registry = ToolRegistry()
    tool = ToolDefinition(
        name="echo",
        description="Echoes back the input",
        parameters=[
            ToolParameter(name="message", type="string", description="Message to echo"),
        ],
        handler=lambda message: {"echo": message},
    )
    registry.register(tool)
    return registry


def _make_registry_with_symbol_regime():
    registry = _make_registry_with_echo()
    registry.register(
        ToolDefinition(
            name="get_symbol_regime_probability",
            description="symbol regime probability",
            parameters=[ToolParameter(name="stock_code", type="string", description="code")],
            handler=lambda stock_code, **kwargs: {
                "status": "ok",
                "stock_code": stock_code,
                "regime": "trending_up",
                "sample_count": 16,
                "windows": {"30": {"n": 16, "p_up": 0.6, "p_below_current": 0.4, "low_confidence": False}},
                "reentry_reference": {"reentry_price": 98.5, "low_confidence": False},
            },
        )
    )
    return registry


def _make_registry_with_theme_prefetch_tools():
    registry = _make_registry_with_echo()
    registry.register(
        ToolDefinition(
            name="get_stockapi_hot_sectors",
            description="hot sectors",
            parameters=[],
            handler=lambda **kwargs: {
                "status": "partial",
                "sectors": [
                    {"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 92}
                ],
            },
        )
    )
    registry.register(
        ToolDefinition(
            name="get_stockapi_limit_up_pool",
            description="limit up pool",
            parameters=[],
            handler=lambda **kwargs: {
                "status": "partial",
                "items": [
                    {"code": "300001", "name": "光模块龙头", "concepts": "CPO 光模块", "limit_up_streak": 2, "bomb_num": 0}
                ],
            },
        )
    )
    registry.register(
        ToolDefinition(
            name="get_stockapi_hot_sector_leaders",
            description="hot sector leaders",
            parameters=[],
            handler=lambda **kwargs: {
                "status": "partial",
                "items": [
                    {"code": "300001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "net_inflow": 500_000_000}
                ],
            },
        )
    )
    registry.register(
        ToolDefinition(
            name="get_stockapi_popularity_rank",
            description="popularity rank",
            parameters=[],
            handler=lambda **kwargs: {
                "status": "partial",
                "items": [
                    {"code": "300001", "name": "光模块龙头", "concepts": ["CPO"], "rank": 2}
                ],
            },
        )
    )
    return registry


def _make_counting_echo_registry(counter):
    """Create an echo registry that records real handler executions."""
    registry = ToolRegistry()
    tool = ToolDefinition(
        name="echo",
        description="Echoes back the input",
        parameters=[
            ToolParameter(name="message", type="string", description="Message to echo"),
        ],
        handler=lambda message: counter.append(message) or {"echo": message},
    )
    registry.register(tool)
    return registry


def _make_mock_adapter():
    """Create a MagicMock LLMToolAdapter."""
    adapter = MagicMock()
    return adapter


SAMPLE_DASHBOARD = {
    "stock_name": "贵州茅台",
    "sentiment_score": 75,
    "trend_prediction": "看多",
    "operation_advice": "持有",
    "decision_type": "hold",
    "confidence_level": "中",
    "dashboard": {
        "core_conclusion": {
            "one_sentence": "茅台近期震荡走强",
            "signal_type": "🟡持有观望",
        },
    },
    "analysis_summary": "Overall bullish trend",
    "key_points": "Strong revenue growth",
    "risk_warning": "High valuation",
    "buy_reason": "Sector leader",
    "trend_analysis": "Upward trend",
    "technical_analysis": "MACD golden cross",
}


# ============================================================
# AgentExecutor Tests
# ============================================================

class TestAgentExecutor(unittest.TestCase):
    """Test the ReAct loop logic."""

    def test_prompt_omits_hardcoded_trend_baseline_when_default_policy_is_empty(self):
        """Explicit skill runs should not silently keep the legacy trend baseline."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )

        executor = AgentExecutor(
            registry,
            adapter,
            skill_instructions="### 技能 1: 缠论\n- 关注中枢与背驰",
            default_skill_policy="",
            max_steps=2,
        )
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        prompt = adapter.call_with_tools.call_args.args[0][0]["content"]
        self.assertIn("### 技能 1: 缠论", prompt)
        self.assertNotIn("专注于趋势交易", prompt)
        self.assertNotIn("多头排列：MA5 > MA10 > MA20", prompt)

    def test_prompt_keeps_injected_default_policy_for_implicit_default_run(self):
        """Implicit default runs can still inject the default bull-trend baseline explicitly."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )

        executor = AgentExecutor(
            registry,
            adapter,
            skill_instructions="### 技能 1: 默认多头趋势",
            default_skill_policy="## 默认技能基线（必须严格遵守）\n- **多头排列必须条件**：MA5 > MA10 > MA20",
            use_legacy_default_prompt=True,
            max_steps=2,
        )
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        prompt = adapter.call_with_tools.call_args.args[0][0]["content"]
        self.assertIn("### 技能 1: 默认多头趋势", prompt)
        self.assertIn("专注于趋势交易", prompt)
        self.assertIn("多头排列必须条件", prompt)
        self.assertIn("多头排列：MA5 > MA10 > MA20", prompt)

    def test_simple_text_response(self):
        """Agent returns text immediately (no tool calls) with JSON dashboard."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        # LLM returns a text response with the dashboard JSON
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 100},
            provider="openai",
        )

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        self.assertIsNotNone(result.dashboard)
        self.assertEqual(result.dashboard["sentiment_score"], 75)
        self.assertEqual(result.total_steps, 1)
        self.assertEqual(result.provider, "openai")
        self.assertEqual(len(result.tool_calls_log), 0)

    def test_tool_call_then_text(self):
        """Agent calls a tool, gets result, then returns final answer."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        # Step 1: LLM requests tool call
        step1_response = LLMResponse(
            content="Let me check the data.",
            tool_calls=[
                ToolCall(id="call_1", name="echo", arguments={"message": "hello"}),
            ],
            usage={"total_tokens": 50},
            provider="gemini",
        )
        # Step 2: LLM returns final text
        step2_response = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 80},
            provider="gemini",
        )
        adapter.call_with_tools.side_effect = [step1_response, step2_response]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        self.assertEqual(result.total_steps, 2)
        self.assertEqual(result.total_tokens, 130)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertEqual(result.tool_calls_log[0]["tool"], "echo")
        self.assertTrue(result.tool_calls_log[0]["success"])

    def test_context_tool_argument_guard_scopes_portfolio_snapshot_to_selected_account(self):
        context = AgentUserContext(
            accounts=[{"account_id": 3, "account_name": "5w账户", "total_equity": 50000}],
            positions=[],
            report=ReportContext(intent="position_review", analysis_mode="planning_execute"),
        )

        guard = _build_context_tool_argument_guard({"agent_user_context": context})

        self.assertIsNotNone(guard)
        self.assertEqual(
            guard("get_portfolio_snapshot", {"include_positions": True}),
            {"include_positions": True, "account_id": 3},
        )

    def test_context_tool_argument_guard_corrects_search_stock_name_from_positions(self):
        context = AgentUserContext(
            accounts=[{"account_id": 3, "account_name": "5w账户", "total_equity": 50000}],
            positions=[
                PositionContext(
                    symbol="601399",
                    quantity=100,
                    account_id=3,
                    stock_name="国机重装",
                )
            ],
            report=ReportContext(intent="position_review", analysis_mode="planning_execute"),
        )

        guard = _build_context_tool_argument_guard({"agent_user_context": context})

        self.assertIsNotNone(guard)
        self.assertEqual(
            guard(
                "search_comprehensive_intel",
                {"stock_code": "601399", "stock_name": "国投电力"},
            )["stock_name"],
            "国机重装",
        )

    def test_candidate_discovery_keeps_structured_result_json(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="discover_watchlist_candidates",
                description="Discover candidates",
                parameters=[],
                handler=lambda **_: {
                    "status": "ok",
                    "candidate_source": "multi_recall",
                    "candidates": [
                        {
                            "code": "301183",
                            "source": "sequoia:multi_strategy",
                            "matched_strategies": ["ma_volume", "turtle_trade", "rps_breakout"],
                            "reason": "多策略共振。",
                            "signal_score": 100,
                        }
                    ],
                },
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Gather candidates.",
                tool_calls=[ToolCall(id="c1", name="discover_watchlist_candidates", arguments={})],
                usage={"total_tokens": 20},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[{"role": "user", "content": "帮我选股"}],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
        )

        self.assertTrue(result.success)
        self.assertIn("result_json", result.tool_calls_log[0])
        self.assertEqual(result.tool_calls_log[0]["result_json"]["candidates"][0]["code"], "301183")

    def test_multiple_tool_calls_in_one_step(self):
        """Agent requests multiple tool calls in a single response."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        step1 = LLMResponse(
            content="Gathering data.",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"message": "a"}),
                ToolCall(id="c2", name="echo", arguments={"message": "b"}),
            ],
            usage={"total_tokens": 40},
            provider="openai",
        )
        step2 = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 60},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [step1, step2]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 2)

    def test_large_tool_result_is_compacted_before_next_llm_turn(self):
        """Raw OHLC arrays should stay out of the LLM-visible tool message."""
        registry = ToolRegistry()
        rows = [
            {
                "date": f"2026-01-{(idx % 28) + 1:02d}",
                "open": 10 + idx,
                "high": 11 + idx,
                "low": 9 + idx,
                "close": 10.5 + idx,
                "volume": 100000 + idx,
                "raw_blob": "RAW_ROW_SHOULD_NOT_REACH_MODEL_" + ("x" * 80),
            }
            for idx in range(240)
        ]
        registry.register(
            ToolDefinition(
                name="get_daily_history",
                description="history",
                parameters=[ToolParameter(name="stock_code", type="string", description="stock")],
                handler=lambda stock_code: {
                    "code": stock_code,
                    "source": "unit-test",
                    "actual_records": len(rows),
                    "data": rows,
                },
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Need history.",
                tool_calls=[ToolCall(id="h1", name="get_daily_history", arguments={"stock_code": "600519"})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[{"role": "user", "content": "Analyze 600519"}],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
        )

        self.assertTrue(result.success)
        second_call_messages = adapter.call_with_tools.call_args_list[1].args[0]
        tool_message = next(msg for msg in second_call_messages if msg.get("role") == "tool")
        visible = json.loads(tool_message["content"])
        self.assertEqual(visible["context_policy"], "compact_tool_fact_card")
        self.assertEqual(visible["result"]["actual_records"], 240)
        self.assertEqual(len(visible["result"]["latest_bars"]), 8)
        self.assertIn("close_summary", visible["result"])
        self.assertNotIn("RAW_ROW_SHOULD_NOT_REACH_MODEL", tool_message["content"])
        self.assertLess(len(tool_message["content"]), result.tool_calls_log[0]["result_length"])
        self.assertEqual(result.tool_calls_log[0]["context_policy"], "compact_tool_fact_card")
        self.assertLess(result.tool_calls_log[0]["model_result_length"], result.tool_calls_log[0]["result_length"])

    def test_repeated_cached_tool_result_still_uses_compact_context(self):
        """Cache reuse should not reintroduce raw payloads into messages."""
        executions = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="large echo",
                parameters=[ToolParameter(name="message", type="string", description="message")],
                handler=lambda message: executions.append(message) or {
                    "message": message,
                    "items": [{"idx": idx, "blob": "RAW_CACHED_BLOB_" + ("y" * 80)} for idx in range(80)],
                },
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="first",
                tool_calls=[ToolCall(id="e1", name="echo", arguments={"message": "same"})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content="repeat",
                tool_calls=[ToolCall(id="e2", name="echo", arguments={"message": "same"})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[{"role": "user", "content": "Analyze cached result"}],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=4,
        )

        self.assertTrue(result.success)
        self.assertEqual(executions, ["same"])
        tool_messages = [msg for msg in result.messages if msg.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertTrue(result.tool_calls_log[1]["cached"])
        for msg in tool_messages:
            self.assertNotIn("RAW_CACHED_BLOB", msg["content"])
            self.assertEqual(json.loads(msg["content"])["context_policy"], "compact_tool_fact_card")

    def test_compact_failed_tool_result_still_detects_failure(self):
        """Failure checks must inspect compact wrapper.result, not only wrapper status."""
        compact = json.loads(
            compact_tool_result_for_model(
                "get_realtime_quote",
                json.dumps({"status": "failed", "error": "quote unavailable"}),
            )
        )

        self.assertTrue(_is_failed_tool_result(compact))
        self.assertEqual(compact["result"]["status"], "failed")

    def test_unavailable_provider_result_is_completed_without_becoming_evidence(self):
        result = {
            "status": "unavailable",
            "data_available": False,
            "provider_errors": ["upstream unavailable"],
        }

        self.assertFalse(_is_failed_tool_result(result))

    def test_compact_degraded_tools_keep_cache_and_provider_diagnostics(self):
        capital = json.loads(compact_tool_result_for_model(
            "get_capital_flow",
            json.dumps({
                "status": "stale",
                "stock_code": "600519",
                "main_net_inflow": 123.0,
                "cache_hit": True,
                "cache_age_seconds": 120,
                "live_diagnostics": {"status": "failed", "errors": ["token expired"]},
            }),
        ))["result"]
        chip = json.loads(compact_tool_result_for_model(
            "get_chip_distribution",
            json.dumps({
                "status": "unavailable",
                "stock_code": "600519",
                "data_available": False,
                "provider_errors": ["endpoint timeout"],
                "cost_90_low": 10.0,
                "cost_90_high": 12.0,
            }),
        ))["result"]

        self.assertTrue(capital["cache_hit"])
        self.assertEqual(capital["live_diagnostics"]["status"], "failed")
        self.assertFalse(chip["data_available"])
        self.assertEqual(chip["provider_errors"], ["endpoint timeout"])
        self.assertEqual(chip["cost_90_low"], 10.0)

    def test_compact_sector_rankings_keeps_top_and_bottom_lists(self):
        compact = json.loads(compact_tool_result_for_model(
            "get_sector_rankings",
            json.dumps({
                "status": "ok",
                "data_source": "stockapi:hotBkJlrDr",
                "top_sectors": [{"name": "机器人", "change_pct": 3.2}],
                "bottom_sectors": [{"name": "银行", "change_pct": -1.1}],
            }),
        ))["result"]

        self.assertEqual(compact["data_source"], "stockapi:hotBkJlrDr")
        self.assertEqual(compact["top_sectors"]["items"][0]["name"], "机器人")
        self.assertEqual(compact["bottom_sectors"]["items"][0]["name"], "银行")

    def test_capital_flow_compact_context_keeps_only_effective_fields(self):
        raw = {
            "stock_code": "002156",
            "status": "ok",
            "query": {"start_date": None, "end_date": None, "page_no": 1, "page_size": 50},
            "main_net_inflow": 38125300.0,
            "main_inflow_5d": -48161100.0,
            "main_inflow_10d": -771445800.0,
            "net_inflow": -538865000.0,
            "net_inflow_5d": -3548161100.0,
            "net_inflow_10d": -7716445800.0,
            "amount_unit": "CNY",
            "main_inflow_definition": "(buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount) * 10000",
            "net_inflow_definition": "net_mf_amount * 10000",
            "latest_date": "2026-06-08",
            "source_update": "tushare_moneyflow_after_market_close",
            "source_chain": [
                {
                    "provider": "capital_stock:tushare_moneyflow",
                    "result": "ok",
                    "duration_ms": 2071,
                }
            ],
            "sector_rankings": {"top_inflow_sectors": [], "top_outflow_sectors": []},
            "error_summary": None,
            "errors": [],
        }

        compact = json.loads(
            compact_tool_result_for_model("get_capital_flow", json.dumps(raw, ensure_ascii=False))
        )

        self.assertEqual(
            compact["result"],
            {
                "stock_code": "002156",
                "status": "ok",
                "main_net_inflow": 38125300.0,
                "main_inflow_5d": -48161100.0,
                "main_inflow_10d": -771445800.0,
                "net_inflow": -538865000.0,
                "net_inflow_5d": -3548161100.0,
                "net_inflow_10d": -7716445800.0,
                "amount_unit": "CNY",
                "semantic_ref": "capital_flow.v1",
                "semantic_risk_level": "P0",
                "field_semantics": {
                    "main_net_inflow": "主力口径=(buy_lg_amount+buy_elg_amount-sell_lg_amount-sell_elg_amount)*10000, CNY",
                    "main_inflow_5d": "5日主力口径累计, CNY",
                    "main_inflow_10d": "10日主力口径累计, CNY",
                    "net_inflow": "Tushare net_mf_amount全口径主动净流入*10000, CNY; 不等于主力资金",
                    "net_inflow_5d": "5日全口径主动净流入累计, CNY; 不等于主力资金",
                    "net_inflow_10d": "10日全口径主动净流入累计, CNY; 不等于主力资金",
                },
                "main_inflow_definition": "(buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount) * 10000",
                "net_inflow_definition": "net_mf_amount * 10000",
                "latest_date": "2026-06-08",
            },
        )
        visible = json.dumps(compact["result"], ensure_ascii=False)
        self.assertNotIn("query", visible)
        self.assertNotIn("source_chain", visible)
        self.assertNotIn("source_update", visible)
        self.assertIn("不等于主力资金", visible)

    def test_metric_semantics_registry_only_injects_visible_high_risk_fields(self):
        compact = json.loads(
            compact_tool_result_for_model(
                "get_chip_distribution",
                json.dumps(
                    {
                        "stock_code": "002156",
                        "status": "ok",
                        "profit_ratio": 0.115,
                        "avg_cost": 67.26,
                        "source_chain": [{"provider": "mock"}],
                    },
                    ensure_ascii=False,
                ),
            )
        )

        result = compact["result"]
        self.assertEqual(result["semantic_ref"], "chip_distribution.v1")
        self.assertEqual(result["semantic_risk_level"], "P0")
        self.assertEqual(
            sorted(result["field_semantics"].keys()),
            ["avg_cost", "profit_ratio"],
        )
        self.assertIn("缺失时不能估算", json.dumps(result["field_semantics"], ensure_ascii=False))
        self.assertNotIn("source_chain", json.dumps(result, ensure_ascii=False))

    def test_metric_semantics_registry_is_intentionally_sparse(self):
        specs = registered_metric_semantics()

        self.assertIn("get_capital_flow", specs)
        self.assertIn("get_chip_distribution", specs)
        self.assertLessEqual(len(specs), 6)

    def test_all_registered_tools_have_explicit_etl_profile(self):
        from src.agent.factory import get_tool_registry

        registry = get_tool_registry()
        profiles = {
            tool.name: resolve_tool_etl_profile(tool.name)
            for tool in registry.list_tools()
        }

        missing = sorted(name for name, profile in profiles.items() if profile == "generic")
        self.assertEqual(missing, [])
        self.assertGreaterEqual(len(profiles), 70)

    def test_max_steps_exceeded(self):
        """Agent keeps calling tools until max_steps is hit."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        # Always return tool calls, never final text
        tool_response = LLMResponse(
            content="Still working.",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"message": "loop"}),
            ],
            usage={"total_tokens": 20},
            provider="openai",
        )
        adapter.call_with_tools.return_value = tool_response

        executor = AgentExecutor(registry, adapter, max_steps=3)
        result = executor.run("Analyze loop")

        self.assertFalse(result.success)
        self.assertIn("max steps", result.error.lower())
        self.assertEqual(result.total_steps, 3)

    def test_final_step_forces_synthesis_after_tool_budget(self):
        """Final step disables tools so the model can synthesize from existing evidence."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        tool_response = LLMResponse(
            content="Still gathering.",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"message": "loop"}),
            ],
            usage={"total_tokens": 20},
            provider="openai",
        )
        final_response = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 40},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [tool_response, tool_response, final_response]

        executor = AgentExecutor(registry, adapter, max_steps=3)
        result = executor.run("Analyze loop")

        self.assertTrue(result.success)
        self.assertEqual(result.total_steps, 3)
        self.assertEqual(len(result.tool_calls_log), 2)
        final_call_args = adapter.call_with_tools.call_args.args
        self.assertEqual(final_call_args[1], [])
        self.assertIn("工具预算最后一步", final_call_args[0][-1]["content"])

    def test_budget_phase_warns_before_final_step(self):
        """Conserve and critical phases warn the model before final hard stop."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        tool_response = LLMResponse(
            content="Gathering.",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"message": "a"}),
            ],
            usage={"total_tokens": 10},
            provider="openai",
        )
        final_response = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 40},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [
            tool_response,
            tool_response,
            tool_response,
            tool_response,
            final_response,
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze with budget")

        self.assertTrue(result.success)
        final_messages = adapter.call_with_tools.call_args.args[0]
        combined = "\n".join(
            str(message.get("content", ""))
            for message in final_messages
            if isinstance(message, dict)
        )
        self.assertIn("工具节约阶段", combined)
        self.assertIn("关键预算阶段", combined)

    def test_repeated_tool_call_reuses_cached_result(self):
        """Identical successful tool calls reuse prior results instead of re-executing."""
        executed = []
        registry = _make_counting_echo_registry(executed)
        adapter = _make_mock_adapter()

        repeated_tool = LLMResponse(
            content="Again.",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"message": "same"}),
            ],
            usage={"total_tokens": 10},
            provider="openai",
        )
        final_response = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 40},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [repeated_tool, repeated_tool, final_response]

        executor = AgentExecutor(registry, adapter, max_steps=4)
        result = executor.run("Analyze repeated")

        self.assertTrue(result.success)
        self.assertEqual(executed, ["same"])
        self.assertEqual(len(result.tool_calls_log), 2)
        self.assertFalse(result.tool_calls_log[0]["cached"])
        self.assertTrue(result.tool_calls_log[1]["cached"])

    def test_tool_execution_error(self):
        """Tool raises exception — should be logged and error sent to LLM."""
        def _always_fail():
            raise RuntimeError("db down")

        registry = ToolRegistry()
        tool = ToolDefinition(
            name="failing_tool",
            description="Always fails",
            parameters=[],
            handler=_always_fail,
        )
        registry.register(tool)
        adapter = _make_mock_adapter()

        step1 = LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="f1", name="failing_tool", arguments={}),
            ],
            usage={"total_tokens": 30},
            provider="openai",
        )
        step2 = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [step1, step2]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test error handling")

        # Should still succeed overall (agent handles tool errors gracefully)
        self.assertTrue(result.success)
        # The failing tool call should be logged as failure
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0]["success"])

    def test_structured_failed_tool_result_is_logged_as_failure(self):
        """Tool payloads with failed status should not be marked OK in traces."""
        def _structured_fail():
            return {"status": "failed", "errors": ["capital_flow timeout"]}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_capital_flow",
                description="Get capital flow",
                parameters=[],
                handler=_structured_fail,
            )
        )
        adapter = _make_mock_adapter()

        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="f1", name="get_capital_flow", arguments={})],
                usage={"total_tokens": 30},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD),
                tool_calls=[],
                usage={"total_tokens": 50},
                provider="openai",
            ),
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test structured tool failure")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0]["success"])
        self.assertIn("capital_flow timeout", result.tool_calls_log[0]["result_preview"])

    def test_partial_tool_result_without_usable_data_is_logged_as_failure(self):
        """Partial payloads with only errors should not be marked OK in traces."""
        def _partial_fail():
            return {
                "stock_code": "600519",
                "status": "partial",
                "main_net_inflow": None,
                "inflow_5d": None,
                "inflow_10d": None,
                "sector_rankings": {
                    "top_inflow_sectors": [],
                    "top_outflow_sectors": [],
                },
                "errors": ["capital flow fetch failed"],
            }

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_capital_flow",
                description="Get capital flow",
                parameters=[],
                handler=_partial_fail,
            )
        )
        adapter = _make_mock_adapter()

        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="f1", name="get_capital_flow", arguments={})],
                usage={"total_tokens": 30},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD),
                tool_calls=[],
                usage={"total_tokens": 50},
                provider="openai",
            ),
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test partial tool failure")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0]["success"])
        self.assertIn("capital flow fetch failed", result.tool_calls_log[0]["result_preview"])

    def test_tool_result_with_errors_but_partial_data_is_logged_as_success(self):
        """Partial payloads with usable data keep errors as diagnostics."""
        def _partial_with_errors():
            return {
                "stock_code": "600519",
                "status": "partial",
                "main_net_inflow": 123.4,
                "inflow_5d": None,
                "inflow_10d": None,
                "sector_rankings": {
                    "top_inflow_sectors": [],
                    "top_outflow_sectors": [],
                },
                "errors": ["capital flow stage timeout"],
            }

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_capital_flow",
                description="Get capital flow",
                parameters=[],
                handler=_partial_with_errors,
            )
        )
        adapter = _make_mock_adapter()

        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="f1", name="get_capital_flow", arguments={})],
                usage={"total_tokens": 30},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD),
                tool_calls=[],
                usage={"total_tokens": 50},
                provider="openai",
            ),
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test partial with errors")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertTrue(result.tool_calls_log[0]["success"])

    def test_not_supported_tool_result_is_not_logged_as_failure(self):
        """Unsupported-but-valid tool payloads remain visible without being treated as crashes."""
        def _not_supported():
            return {"status": "not_supported", "note": "A-share only"}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_capital_flow",
                description="Get capital flow",
                parameters=[],
                handler=_not_supported,
            )
        )
        adapter = _make_mock_adapter()

        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="f1", name="get_capital_flow", arguments={})],
                usage={"total_tokens": 30},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD),
                tool_calls=[],
                usage={"total_tokens": 50},
                provider="openai",
            ),
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test unsupported tool result")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertTrue(result.tool_calls_log[0]["success"])

    def test_unknown_tool_called(self):
        """LLM requests a tool not in the registry — should handle gracefully."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        step1 = LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="u1", name="nonexistent_tool", arguments={}),
            ],
            usage={"total_tokens": 20},
            provider="openai",
        )
        step2 = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )
        adapter.call_with_tools.side_effect = [step1, step2]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Test unknown tool")

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0]["success"])
        self.assertFalse(result.tool_calls_log[0]["cached"])

    def test_non_retriable_tool_failure_is_cached_across_hk_variants(self):
        """Equivalent HK code variants should not re-execute a non-retriable failing tool."""
        calls = []

        def _quote(stock_code):
            calls.append(stock_code)
            return {
                "error": f"No realtime quote available for {stock_code}",
                "retriable": False,
                "note": "Skip retry",
            }

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[
                    ToolParameter(name="stock_code", type="string", description="Stock code"),
                ],
                handler=_quote,
            )
        )
        adapter = _make_mock_adapter()

        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="q1", name="get_realtime_quote", arguments={"stock_code": "hk01810"}),
                ],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="q2", name="get_realtime_quote", arguments={"stock_code": "1810.HK"}),
                ],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze HK01810")

        self.assertTrue(result.success)
        self.assertEqual(calls, ["hk01810"])
        self.assertEqual(len(result.tool_calls_log), 2)
        self.assertFalse(result.tool_calls_log[0]["cached"])
        self.assertTrue(result.tool_calls_log[1]["cached"])

    def test_model_trace_deduplicates_and_keeps_order(self):
        """Model trace should keep call order and de-duplicate repeated models."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        step1 = LLMResponse(
            content="first tool call",
            tool_calls=[ToolCall(id="m1", name="echo", arguments={"message": "a"})],
            usage={"total_tokens": 10},
            provider="gemini",
            model="gemini/gemini-2.0-flash",
        )
        step2 = LLMResponse(
            content="second tool call",
            tool_calls=[ToolCall(id="m2", name="echo", arguments={"message": "b"})],
            usage={"total_tokens": 10},
            provider="gemini",
            model="gemini/gemini-2.0-flash",
        )
        step3 = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 10},
            provider="openai",
            model="openai/gpt-4o-mini",
        )
        adapter.call_with_tools.side_effect = [step1, step2, step3]

        executor = AgentExecutor(registry, adapter, max_steps=5)
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        self.assertEqual(result.model, "gemini/gemini-2.0-flash, openai/gpt-4o-mini")

    def test_model_trace_skips_error_provider(self):
        """Error provider placeholder should not appear in model trace."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="llm failed",
            tool_calls=[],
            usage={"total_tokens": 3},
            provider="error",
            model="",
        )

        executor = AgentExecutor(registry, adapter, max_steps=2)
        result = executor.run("Analyze 600519")

        self.assertFalse(result.success)
        self.assertEqual(result.model, "")

    def test_error_provider_preserves_failure_reason_in_agent_result(self):
        """LLM adapter error responses must surface as failed Agent results, not final answers."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="No LLM configured. Please set LITELLM_MODEL, LLM_CHANNELS, or provider API keys before using Agent.",
            tool_calls=[],
            usage={"total_tokens": 1},
            provider="error",
            model="",
        )

        executor = AgentExecutor(registry, adapter, max_steps=2)
        result = executor.run("Analyze 600519")

        self.assertFalse(result.success)
        self.assertEqual(result.content, "")
        self.assertEqual(
            result.error,
            "No LLM configured. Please set LITELLM_MODEL, LLM_CHANNELS, or provider API keys before using Agent.",
        )
        self.assertEqual(result.total_steps, 1)
        self.assertEqual(result.total_tokens, 1)
        self.assertEqual(result.model, "")

    def test_timeout_budget_aborts_single_agent_loop(self):
        """Single-agent executor should stop once the configured timeout budget is exhausted."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()

        def _slow_llm(*_args, **_kwargs):
            time.sleep(0.03)
            return LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            )

        adapter.call_with_tools.side_effect = _slow_llm

        executor = AgentExecutor(registry, adapter, max_steps=2, timeout_seconds=0.01)
        result = executor.run("Analyze 600519")

        self.assertFalse(result.success)
        self.assertIn("timed out", (result.error or "").lower())

    def test_parallel_tool_timeout_marks_only_pending_calls(self):
        """Parallel tool batches should emit timeout errors for unfinished tools."""
        registry = ToolRegistry()

        def _maybe_slow_echo(message):
            if message == "slow":
                time.sleep(0.05)
            return {"echo": message}

        registry.register(
            ToolDefinition(
                name="echo",
                description="Echoes back the input",
                parameters=[
                    ToolParameter(name="message", type="string", description="Message to echo"),
                ],
                handler=_maybe_slow_echo,
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Gathering data.",
                tool_calls=[
                    ToolCall(id="fast", name="echo", arguments={"message": "fast"}),
                    ToolCall(id="slow", name="echo", arguments={"message": "slow"}),
                ],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Analyze"},
            ],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
            tool_call_timeout_seconds=0.01,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 2)
        timeout_logs = [log for log in result.tool_calls_log if log.get("timeout")]
        self.assertEqual(len(timeout_logs), 1)
        self.assertEqual(timeout_logs[0]["arguments"]["message"], "slow")

    def test_single_tool_timeout_marks_tool_failed(self):
        """Single tool calls should also respect the configured tool timeout."""
        registry = ToolRegistry()

        def _slow_echo(message):
            time.sleep(0.05)
            return {"echo": message}

        registry.register(
            ToolDefinition(
                name="echo",
                description="Echoes back the input",
                parameters=[
                    ToolParameter(name="message", type="string", description="Message to echo"),
                ],
                handler=_slow_echo,
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Gathering data.",
                tool_calls=[ToolCall(id="slow", name="echo", arguments={"message": "slow"})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Analyze"},
            ],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
            tool_call_timeout_seconds=0.01,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertTrue(result.tool_calls_log[0].get("timeout"))
        self.assertEqual(result.tool_calls_log[0]["arguments"]["message"], "slow")

    def test_heavy_diagnostic_tool_gets_runner_timeout_floor(self):
        """Known heavy tools should return structured diagnostics instead of runner shell timeout."""
        registry = ToolRegistry()

        def _regime_tool(market="cn", persist=True):
            time.sleep(0.05)
            return {
                "status": "stale_fallback",
                "market": market,
                "regime": "high_volatility",
                "persisted": bool(persist),
                "component_diagnostics": {"market_history": {"status": "timeout"}},
            }

        registry.register(
            ToolDefinition(
                name="detect_market_regime",
                description="Detect market regime",
                parameters=[],
                handler=_regime_tool,
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Gathering regime.",
                tool_calls=[ToolCall(id="regime", name="detect_market_regime", arguments={"market": "cn", "persist": True})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Analyze"},
            ],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
            tool_call_timeout_seconds=0.01,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0].get("timeout"))
        self.assertTrue(result.tool_calls_log[0]["success"])
        self.assertIn("stale_fallback", result.tool_calls_log[0]["result_preview"])

    def test_capital_flow_gets_runner_timeout_floor(self):
        """Capital-flow should return its own timeout/fallback diagnostics."""
        registry = ToolRegistry()

        def _capital_flow_tool(stock_code="600519"):
            time.sleep(0.05)
            return {
                "stock_code": stock_code,
                "status": "failed",
                "source_chain": [{"provider": "capital_stock:tushare_moneyflow_dc", "result": "failed"}],
                "errors": ["tushare_moneyflow_dc:timeout:budget_exhausted"],
            }

        registry.register(
            ToolDefinition(
                name="get_capital_flow",
                description="Get capital flow",
                parameters=[],
                handler=_capital_flow_tool,
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="Gathering capital flow.",
                tool_calls=[ToolCall(id="flow", name="get_capital_flow", arguments={"stock_code": "603667"})],
                usage={"total_tokens": 10},
                provider="openai",
            ),
            LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            ),
        ]

        result = run_agent_loop(
            messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Analyze"},
            ],
            tool_registry=registry,
            llm_adapter=adapter,
            max_steps=3,
            tool_call_timeout_seconds=0.01,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertFalse(result.tool_calls_log[0].get("timeout"))
        self.assertFalse(result.tool_calls_log[0]["success"])
        self.assertIn("tushare_moneyflow_dc:timeout", result.tool_calls_log[0]["result_preview"])

    def test_llm_call_receives_remaining_timeout_budget(self):
        """LLM tool calls should receive the remaining wall-clock budget."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        captured = {}

        def _capture_timeout(*_args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return LLMResponse(
                content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
                tool_calls=[],
                usage={"total_tokens": 10},
                provider="openai",
            )

        adapter.call_with_tools.side_effect = _capture_timeout

        executor = AgentExecutor(registry, adapter, max_steps=2, timeout_seconds=1.0)
        result = executor.run("Analyze 600519")

        self.assertTrue(result.success)
        self.assertIsNotNone(captured.get("timeout"))
        self.assertGreater(captured["timeout"], 0.0)
        self.assertLessEqual(captured["timeout"], 1.0)

    def test_min_step_budget_skips_followup_llm_call(self):
        """When step>0 and remaining budget is too small, no extra LLM call should be made."""
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="Need one tool first.",
            tool_calls=[ToolCall(id="echo_1", name="echo", arguments={"message": "hello"})],
            usage={"total_tokens": 10},
            provider="openai",
        )

        with patch(
            "src.agent.runner._remaining_timeout_seconds",
            side_effect=[9.0, 9.0, 7.5, 7.5],
        ):
            result = run_agent_loop(
                messages=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "Analyze"},
                ],
                tool_registry=registry,
                llm_adapter=adapter,
                max_steps=3,
                max_wall_clock_seconds=10.0,
            )

        self.assertFalse(result.success)
        self.assertIn("insufficient budget", (result.error or "").lower())
        self.assertEqual(adapter.call_with_tools.call_count, 1)
        self.assertEqual(len(result.tool_calls_log), 1)
        self.assertEqual(result.total_steps, 1)


# ============================================================
# Dashboard parsing
# ============================================================

class TestDashboardParsing(unittest.TestCase):
    """Test parse_dashboard_json with various input formats."""

    def test_parse_markdown_json_block(self):
        content = f"Here is my analysis:\n```json\n{json.dumps(SAMPLE_DASHBOARD)}\n```\nDone."
        result = parse_dashboard_json(content)
        self.assertIsNotNone(result)
        self.assertEqual(result["sentiment_score"], 75)

    def test_parse_raw_json(self):
        content = json.dumps(SAMPLE_DASHBOARD)
        result = parse_dashboard_json(content)
        self.assertIsNotNone(result)

    def test_parse_json_in_text(self):
        content = f"Let me present: {json.dumps(SAMPLE_DASHBOARD)} — that's all."
        result = parse_dashboard_json(content)
        self.assertIsNotNone(result)

    def test_parse_empty_content(self):
        self.assertIsNone(parse_dashboard_json(""))
        self.assertIsNone(parse_dashboard_json(None))

    def test_parse_no_json(self):
        self.assertIsNone(parse_dashboard_json("This is just plain text with no JSON"))

    def test_try_parse_json_prefers_final_balanced_object(self):
        from src.agent.runner import try_parse_json

        content = (
            '草稿：{"winner":"primary","final_action":"hold"}\n'
            '最终 JSON：\n'
            '{"winner":"opposing","final_action":"wait","decision_summary":"等待确认"}'
        )

        result = try_parse_json(content)

        self.assertIsNotNone(result)
        self.assertEqual(result["winner"], "opposing")
        self.assertEqual(result["final_action"], "wait")


# ============================================================
# Serialization
# ============================================================

class TestSerializeToolResult(unittest.TestCase):
    """Test serialize_tool_result for various types."""

    def test_serialize_none(self):
        result = serialize_tool_result(None)
        self.assertEqual(json.loads(result), {"result": None})

    def test_serialize_string(self):
        result = serialize_tool_result("hello")
        self.assertEqual(result, "hello")

    def test_serialize_dict(self):
        d = {"key": "value", "num": 42}
        result = serialize_tool_result(d)
        self.assertEqual(json.loads(result), d)

    def test_serialize_list(self):
        lst = [1, 2, 3]
        result = serialize_tool_result(lst)
        self.assertEqual(json.loads(result), lst)

    def test_serialize_dataclass(self):
        @dataclass
        class Sample:
            name: str = "test"
            value: int = 42

        result = serialize_tool_result(Sample())
        parsed = json.loads(result)
        self.assertEqual(parsed["name"], "test")
        self.assertEqual(parsed["value"], 42)


# ============================================================
# User message builder
# ============================================================

class TestBuildUserMessage(unittest.TestCase):
    """Test _build_user_message formatting."""

    def setUp(self):
        self.executor = AgentExecutor(
            ToolRegistry(), _make_mock_adapter(), max_steps=1
        )

    def test_basic_message(self):
        msg = self.executor._build_user_message("Analyze 600519")
        self.assertIn("Analyze 600519", msg)
        self.assertIn("决策仪表盘", msg)

    def test_message_with_context(self):
        msg = self.executor._build_user_message(
            "Analyze",
            context={"stock_code": "600519", "report_type": "daily"},
        )
        self.assertIn("股票代码: 600519", msg)
        self.assertIn("报告类型: daily", msg)

    def test_message_injects_planning_context_and_plan(self):
        context = AgentUserContext(
            positions=[PositionContext(symbol="600519", quantity=100)],
            report=ReportContext(
                analysis_mode="planning_execute",
                primary_symbol="600519",
                target_symbols=["600519"],
            ),
        )

        msg = self.executor._build_user_message(
            "Analyze",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertIn("AgentUserContext", msg)
        self.assertIn("Planner 工具执行计划", msg)
        self.assertIn('"intent": "position_review"', msg)
        self.assertIn("capability -> tools", msg)

    def test_run_uses_planning_system_prompt_when_context_requests_it(self):
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )
        executor = AgentExecutor(registry, adapter, max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                primary_symbol="600519",
                target_symbols=["600519"],
            )
        )

        result = executor.run(
            "Analyze 600519",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        prompt = adapter.call_with_tools.call_args.args[0][0]["content"]
        self.assertIn("Planning -> Execute", prompt)
        self.assertIn("账户", prompt)

    def test_single_stock_planning_run_prefetches_symbol_regime_probability(self):
        registry = _make_registry_with_symbol_regime()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )
        executor = AgentExecutor(registry, adapter, max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600519",
                target_symbols=["600519"],
            )
        )

        result = executor.run(
            "600519 可以买吗",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        user_message = adapter.call_with_tools.call_args.args[0][1]["content"]
        self.assertIn("单股 Regime 概率证据", user_message)
        self.assertIn('"stock_code": "600519"', user_message)
        self.assertEqual(result.tool_calls_log[0]["tool"], "get_symbol_regime_probability")
        self.assertTrue(result.tool_calls_log[0]["prefetch"])

    def test_single_stock_planning_run_prefetches_theme_profile(self):
        registry = _make_registry_with_theme_prefetch_tools()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content=json.dumps(SAMPLE_DASHBOARD, ensure_ascii=False),
            tool_calls=[],
            usage={"total_tokens": 50},
            provider="openai",
        )
        executor = AgentExecutor(registry, adapter, max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="300001",
                target_symbols=["300001"],
            )
        )

        result = executor.run(
            "300001 可以买吗",
            context={"stock_code": "300001", "stock_name": "光模块龙头", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        user_message = adapter.call_with_tools.call_args.args[0][1]["content"]
        self.assertIn("单股主线动量分型证据", user_message)
        self.assertIn('"symbol": "300001"', user_message)
        self.assertIn('"stock_role": "core_leader"', user_message)
        self.assertIn('"chase_permission": "conditional_only"', user_message)
        prefetch_tools = [call["tool"] for call in result.tool_calls_log if call.get("prefetch")]
        self.assertIn("get_stockapi_hot_sectors", prefetch_tools)
        self.assertIn("get_stockapi_limit_up_pool", prefetch_tools)
        self.assertIn("get_stockapi_hot_sector_leaders", prefetch_tools)
        self.assertIn("get_stockapi_popularity_rank", prefetch_tools)

    def test_watchlist_scan_does_not_prefetch_symbol_regime_probability(self):
        registry = _make_registry_with_symbol_regime()
        adapter = _make_mock_adapter()
        adapter.call_text.return_value = LLMResponse(
            content='{"stage":"candidate_discovery","status":"failed","summary":{},"full":{"candidates":[]}}',
            provider="openai",
        )
        executor = AgentExecutor(registry, adapter, max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="watchlist_scan",
                target_symbols=[],
            )
        )

        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="帮我选股",
            context={"agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertFalse(any(call.get("tool") == "get_symbol_regime_probability" for call in result.tool_calls_log))

    def test_watchlist_scan_does_not_prefetch_single_stock_theme_profile(self):
        registry = _make_registry_with_theme_prefetch_tools()
        executor = AgentExecutor(registry, _make_mock_adapter(), max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="watchlist_scan",
                target_symbols=[],
            )
        )

        prefetch = executor._prefetch_single_symbol_regime_context({"agent_user_context": context})

        self.assertEqual(prefetch, {})

    def test_chat_uses_planning_system_prompt_when_context_requests_it(self):
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="单股入场分析需要先按 Planner 完成工具链路。",
            tool_calls=[],
            usage={"total_tokens": 20},
            provider="openai",
        )
        executor = AgentExecutor(registry, adapter, max_steps=2)
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600519",
                target_symbols=["600519"],
            )
        )

        result = executor.chat(
            "帮我分析 600519 是否适合入场",
            session_id="test-planning-chat",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        prompt = adapter.call_with_tools.call_args.args[0][0]["content"]
        user_message = adapter.call_with_tools.call_args.args[0][-1]["content"]
        self.assertIn("Planning -> Execute", prompt)
        self.assertIn("Execute Protocol", prompt)
        self.assertIn("AgentUserContext", user_message)
        self.assertIn("Planner 工具执行计划", user_message)

    def test_watchlist_selection_partial_report_does_not_fall_back_to_react_loop(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="discover_watchlist_candidates",
                description="Discover candidates",
                parameters=[],
                handler=lambda **_: {"status": "ok", "candidates": [{"code": "600519", "name": "贵州茅台"}]},
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_text.side_effect = [
            LLMResponse(
                content='{"stage":"candidate_discovery","status":"ok","summary":{"candidate_codes":["600519"]},"full":{"candidates":[{"code":"600519","name":"贵州茅台"}]}}',
                provider="openai",
                model="openai/test",
            ),
            RuntimeError("llm provider timeout"),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="watchlist_scan",
            )
        )

        executor = AgentExecutor(registry, adapter, max_steps=1, orchestration_mode="expert_graph")
        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="我现在有5w元，你帮我选一下股，并做一下持仓配置的分析",
            context={"agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertIn("选股分析报告：下周可关注候选", result.content)
        self.assertIsNotNone(result.stock_selection)
        self.assertEqual(result.stock_selection["final_report_json"]["orchestration_mode"], "expert_graph")
        self.assertIsNotNone(result.stock_selection["final_report_json"]["expert_state"])
        self.assertFalse(adapter.call_with_tools.called)

    def test_watchlist_scan_without_candidates_uses_staged_selection(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="discover_watchlist_candidates",
                description="Discover candidates",
                parameters=[],
                handler=lambda **_: {"status": "ok", "candidates": [{"code": "600519", "name": "贵州茅台"}]},
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_text.side_effect = [
            LLMResponse(content='{"stage":"candidate_discovery","status":"ok","summary":{"candidate_codes":["600519"]},"full":{"candidates":[{"code":"600519","name":"贵州茅台"}]}}'),
            LLMResponse(content='{"stage":"candidate_screening","status":"ok","summary":{"deep_dive_targets":["600519"]},"full":{"shortlist":[]}}'),
            LLMResponse(content='{"stage":"single_stock_deep_dive","status":"ok","summary":{"code":"600519","name":"贵州茅台","action_bias":"wait"},"full":{"stock":{"code":"600519","name":"贵州茅台"},"missing_evidence":[]}}'),
            LLMResponse(content='{"stage":"portfolio_allocation","status":"ok","summary":{"portfolio_action":"wait","core_reason":"等待确认"},"full":{"positions_plan":[]}}'),
            LLMResponse(content='{"stage":"adversarial_review","status":"ok","summary":{"opposing_summary":"反方等待"},"full":{"opposing_thesis":{}}}'),
            LLMResponse(content='{"stage":"judge_decision","status":"ok","summary":{"primary_plan_verdict":"accept","final_action":"wait","decision_summary":"等待确认","next_step":"render_final_report"},"full":{"winner":"mixed"}}'),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="watchlist_scan",
            )
        )

        executor = AgentExecutor(registry, adapter, max_steps=1)
        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="我现在有5w元，你帮我选一下股，并做一下持仓配置的分析",
            context={"agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertIn("选股分析报告：下周可关注候选", result.content)
        self.assertIsNotNone(result.stock_selection)
        self.assertFalse(adapter.call_with_tools.called)

    def test_planning_execute_final_content_strips_step_labels(self):
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="第三步：证据整合与综合判断\n## 入场决策表格\n内容",
            tool_calls=[],
            usage={"total_tokens": 10},
            provider="openai",
        )
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                report_type="analysis",
            )
        )

        executor = AgentExecutor(registry, adapter, max_steps=1)
        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="测试",
            context={"agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertNotIn("第三步：", result.content)
        self.assertIn("## 入场决策表格", result.content)

    def test_planning_execute_final_content_keeps_chapter_titles(self):
        registry = _make_registry_with_echo()
        adapter = _make_mock_adapter()
        adapter.call_with_tools.return_value = LLMResponse(
            content="第三章：长期逻辑\n内容",
            tool_calls=[],
            usage={"total_tokens": 10},
            provider="openai",
        )
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                report_type="analysis",
            )
        )

        executor = AgentExecutor(registry, adapter, max_steps=1)
        result = executor._run_loop(
            messages=[],
            tool_decls=[],
            parse_dashboard=False,
            original_task="测试",
            context={"agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertIn("第三章：长期逻辑", result.content)

    def test_chat_appends_debate_appendix_after_planning_execute_tools(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[ToolParameter(name="stock_code", type="string", description="Stock code")],
                handler=lambda stock_code: {"price": 100, "stock_code": stock_code},
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="checking",
                tool_calls=[ToolCall(id="q1", name="get_realtime_quote", arguments={"stock_code": "600519"})],
                usage={"total_tokens": 10},
                provider="openai",
                model="openai/tool-model",
            ),
            LLMResponse(
                content="## 最终结论\n\n持仓策略：持有。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
                model="openai/tool-model",
            ),
        ]
        adapter.call_text.side_effect = [
            LLMResponse(content='{"direction":"bullish","action":"hold","summary":"主观点持有","evidence":["price=100"],"failure_conditions":["跌破成本"],"account_impact":"仓位受限"}', usage={"total_tokens": 3}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"direction":"neutral_bearish","action":"reduce","summary":"反方减仓","evidence":["仓位风险"],"failure_conditions":["趋势确认"],"primary_challenges":["仓位风险"],"account_impact":"回撤风险"}', usage={"total_tokens": 4}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"winner":"primary","final_action":"hold","reason":"持有证据更强但保留风控","accepted_arguments":["price=100"],"rejected_arguments":["立即减仓"],"risk_controls":["跌破成本复查"],"unresolved_conflicts":[]}', usage={"total_tokens": 5}, provider="openai", model="openai/debate"),
        ]
        context = AgentUserContext(
            positions=[PositionContext(symbol="600519", quantity=100, avg_cost=90)],
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="position_review",
                primary_symbol="600519",
                target_symbols=["600519"],
            ),
        )

        executor = AgentExecutor(registry, adapter, max_steps=3)
        result = executor.chat(
            "我持有 600519，适合继续拿长线吗？",
            session_id="test-debate",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertIn("## 对抗式辩论裁决", result.content)
        self.assertEqual(result.debate["judge_decision"]["final_action"], "hold")
        self.assertIn("## 最终结论", result.debate["debug_outputs"]["primary_report_raw"])
        self.assertIn("## 对抗式辩论裁决", result.debate["debug_outputs"]["final_report_with_debate"])
        self.assertEqual(adapter.call_text.call_count, 3)
        self.assertEqual(result.total_tokens, 42)


# ============================================================
# AgentResult dataclass
# ============================================================

class TestAgentResult(unittest.TestCase):
    """Test AgentResult defaults."""

    def test_defaults(self):
        r = AgentResult()
        self.assertFalse(r.success)
        self.assertEqual(r.content, "")
        self.assertIsNone(r.dashboard)
        self.assertEqual(r.tool_calls_log, [])
        self.assertEqual(r.total_steps, 0)
        self.assertEqual(r.total_tokens, 0)
        self.assertIsNone(r.error)


if __name__ == '__main__':
    unittest.main()
