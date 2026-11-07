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

from src.agent.executor import (
    AgentExecutor,
    AgentResult,
    _build_context_tool_argument_guard,
    _compact_report_repair_evidence,
    _normalize_planning_execute_final_content,
    _planning_report_quality_issues,
)
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
from src.agent.tools.signal_tools import submit_trade_plan_tool
from src.schemas.agent_context import AgentUserContext, PositionContext, ReportContext


def _late_dimension_tool_calls():
    calls = [
        {
            "step": index,
            "tool": "analyze_trend",
            "arguments": {"stock_code": "600667", "window": index},
            "success": True,
            "result_preview": '{"status":"ok","trend":"bullish"}',
        }
        for index in range(1, 18)
    ]
    calls.extend([
        {
            "step": 18,
            "tool": "search_comprehensive_intel",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","intel":{"items":[{"title":"公司公告"}]}}',
        },
        {
            "step": 19,
            "tool": "search_stock_news",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"success":true,"results":[{"title":"行业新闻"}]}',
        },
        {
            "step": 20,
            "tool": "get_stock_info",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"partial","name":"太极实业"}',
        },
        {
            "step": 21,
            "tool": "get_tushare_financial_indicators",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","items":[{"roe":8.4,"debt_to_assets":52.1}]}',
        },
        {
            "step": 22,
            "tool": "get_tushare_daily_basic",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","items":[{"pe_ttm":96.54,"pb":5.15}]}',
        },
    ])
    return calls


def test_report_repair_balances_late_news_and_fundamental_evidence():
    evidence = _compact_report_repair_evidence(_late_dimension_tool_calls())
    selected_tools = {item["tool"] for item in evidence}

    assert "search_comprehensive_intel" in selected_tools
    assert "search_stock_news" in selected_tools
    assert "get_stock_info" in selected_tools
    assert "get_tushare_financial_indicators" in selected_tools
    assert "get_tushare_daily_basic" in selected_tools


def test_report_quality_rejects_missing_claim_when_dimension_evidence_exists():
    report = (
        "## 最终结论\n\n等待回踩。\n\n"
        "## 多维证据\n\n基本面健全度：0/10（完全缺失，无法评分）\n\n"
        "消息面明确性：0/10（无有效信息）\n\n"
        "## 交易计划\n\n等待入场，设置止损和止盈。\n\n"
        "## 策略 4 / 缠论确定性评分\n\n影子评分不改变动作。\n\n"
        "## 风险与缺失\n\n失效条件为跌破关键支撑，风险证据需要持续复核。"
    )

    issues = _planning_report_quality_issues(
        report,
        report_language="zh",
        tool_calls=_late_dimension_tool_calls(),
    )

    assert "report_contradicts_fundamental_evidence" in issues
    assert "report_contradicts_news_evidence" in issues


def test_news_transport_success_without_dated_events_is_partial_not_missing():
    from src.agent.dimension_evidence import build_dimension_evidence_summary

    summary = build_dimension_evidence_summary([
        {
            "tool": "search_stock_news",
            "success": True,
            "result_preview": '{"success":true,"results":[{"title":"行业新闻"}]}',
        },
    ])

    assert summary["intel"]["quality"] == "partial"
    assert summary["intel"]["partial_tools"] == ["search_stock_news"]
    assert summary["intel"]["quality"] != "unavailable"


def test_earnings_forecast_is_partial_fundamental_evidence_not_missing():
    from src.agent.dimension_evidence import build_dimension_evidence_summary

    summary = build_dimension_evidence_summary([
        {
            "tool": "get_stock_info",
            "success": True,
            "result_preview": '{"status":"partial","fundamental_context":{"status":"failed"}}',
        },
        {
            "tool": "get_tushare_forecast",
            "success": True,
            "result_preview": (
                '{"status":"ok","items":[{"ann_date":"20260715",'
                '"end_date":"20260630","type":"首亏",'
                '"net_profit_min":-30000,"net_profit_max":-22000}]}'
            ),
        },
    ])

    assert summary["fundamental"]["quality"] == "partial"
    assert summary["fundamental"]["available_tools"] == ["get_tushare_forecast"]
    assert "fundamental_evidence_unavailable" not in summary["fundamental"]["missing_evidence"]


def test_planning_report_hides_leading_internal_json_when_markdown_exists():
    content = """基于证据，以下是决策结果。

```json
{"intent":"entry_analysis","action":"reject","summary":"当前不宜追高"}
```

---

## 最终结论

当前不宜追高。

## 多维证据

基本面存在亏损风险。
"""

    normalized = _normalize_planning_execute_final_content(content)

    assert normalized.startswith("## 最终结论")
    assert "```json" not in normalized
    assert '"intent"' not in normalized
    assert "当前不宜追高" in normalized


def test_planning_report_renders_pure_internal_json_as_readable_markdown():
    content = """```json
{
  "action": "wait",
  "summary": "等待基本面和价格共同确认",
  "evidence": {"fundamental": ["最新财报显示利润承压"]},
  "plan": {"entry": "暂不入场"},
  "risks": ["业绩仍未改善"]
}
```"""

    normalized = _normalize_planning_execute_final_content(content)

    assert normalized.startswith("## 最终结论")
    assert "等待基本面和价格共同确认" in normalized
    assert "### 基本面" in normalized
    assert "最新财报显示利润承压" in normalized
    assert "```json" not in normalized


def test_planning_report_renders_mapping_evidence_without_double_bullets():
    content = """```json
{"action":"wait","summary":"等待确认","evidence":{"fundamental":{"status":"partial","reason":"财报数据不完整"}}}
```"""

    normalized = _normalize_planning_execute_final_content(content)

    assert "- - " not in normalized
    assert "- 状态：partial" in normalized


def test_planning_report_renders_embedded_dashboard_json_after_intro():
    content = """已完成数据采集与交叉验证。

## 决策仪表盘 JSON

```json
{
  "action": "reject",
  "summary": "当前不宜追高",
  "evidence": {"technical": ["RSI 过热"], "fundamental": ["盈利承压"]},
  "plan": {"entry": "N/A", "conditions": ["等待风险收益比修复"]},
  "risks": ["回撤风险"],
  "strategy4_chan_score": {
    "verdict_status": "not_evaluable",
    "cannot_formalize_reason": "模型自行编写的错误解释"
  }
}
```

## 结论要点

重复的自由文本。
"""

    normalized = _normalize_planning_execute_final_content(content)

    assert normalized.startswith("## 最终结论")
    assert "### 技术面" in normalized
    assert "RSI 过热" in normalized
    assert "## 交易计划" in normalized
    assert "```json" not in normalized
    assert "cannot_formalize_reason" not in normalized
    assert "模型自行编写的错误解释" not in normalized


def test_single_stock_fundamental_supplement_is_deterministic_and_auditable():
    registry = _make_registry_with_echo()
    registry.register(ToolDefinition(
        name="search_stock_fundamentals",
        description="fundamental fallback",
        parameters=[
            ToolParameter(name="stock_code", type="string", description="code"),
            ToolParameter(name="stock_name", type="string", description="name", required=False),
        ],
        handler=lambda **_kwargs: {
            "status": "partial",
            "coverage": {"financial_performance": "available"},
            "materials": [{
                "dimension": "financial_performance",
                "title": "公司业绩预告",
                "snippet": "净利润承压。",
                "url": "https://example.com/forecast",
                "source": "example.com",
                "published_date": "2026-07-15",
            }],
        },
    ))
    executor = AgentExecutor(registry, _make_mock_adapter())
    calls = [{
        "tool": "get_stock_info",
        "success": True,
        "result_preview": '{"status":"partial","fundamental_context":{"status":"failed"}}',
    }]

    supplemented = executor._supplement_single_stock_fundamental_evidence(
        tool_calls=calls,
        context={"stock_code": "600330", "stock_name": "天通股份"},
        progress_callback=None,
    )

    assert supplemented is True
    assert calls[-1]["tool"] == "search_stock_fundamentals"
    assert calls[-1]["deterministic_supplement"] is True
    assert calls[-1]["result_json"]["materials"][0]["url"] == "https://example.com/forecast"


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

    def test_message_injects_prefetched_composite_scoring_evidence(self):
        msg = self.executor._build_user_message(
            "Analyze",
            context={
                "stock_code": "600519",
                "trace_composite_verdict": {
                    "schema_version": "stock-composite-shadow.v1",
                    "composite_verdict": {
                        "score": 15,
                        "scale_max": 20,
                        "strategy4_rule_version": "strategy4.v1",
                    },
                },
            },
        )

        self.assertIn("个股确定性综合评分", msg)
        self.assertIn('"strategy4_rule_version": "strategy4.v1"', msg)
        self.assertIn('"score": 15', msg)

    def test_message_requires_partial_composite_evidence_to_be_reported(self):
        msg = self.executor._build_user_message(
            "Analyze",
            context={
                "stock_code": "600487",
                "trace_composite_verdict": {
                    "schema_version": "stock-composite-shadow.v1",
                    "status": "ok",
                    "snapshot": {
                        "symbol": "600487",
                        "s4": {
                            "status": "partial",
                            "weekly_provisional": True,
                            "resonance_count": 2,
                        },
                    },
                    "composite_verdict": {
                        "symbol": "600487",
                        "verdict_status": "not_evaluable",
                        "score": None,
                    },
                },
            },
        )

        self.assertIn("最终报告必须包含“策略 4 / 缠论确定性评分”", msg)
        self.assertIn("partial/provisional", msg)

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

    def test_entry_chat_keeps_natural_answer_and_rejects_submitted_trade_plan(self):
        registry = _make_registry_with_echo()
        registry.register(submit_trade_plan_tool)
        adapter = _make_mock_adapter()
        plan = {
            "symbol": "600519",
            "action": "open",
            "entry_zone_low": 1470.0,
            "entry_zone_high": 1500.0,
            "stop_loss_price": 1420.0,
            "invalidation_conditions": ["日线收盘跌破 1420"],
        }
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="plan-1", name="submit_trade_plan", arguments=plan)],
                usage={"total_tokens": 20},
                provider="openai",
            ),
            LLMResponse(
                content="结论偏积极，等待价格进入计划区间。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
            ),
        ]
        executor = AgentExecutor(registry, adapter, max_steps=3)

        result = executor.chat(
            "分析 600519 是否适合买入",
            session_id="test-entry-trade-plan",
            context={
                "stock_code": "600519",
                "signal_lifecycle_entry_intent": True,
            },
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, "结论偏积极，等待价格进入计划区间。")
        self.assertIsNone(result.trade_plan)
        prompt = adapter.call_with_tools.call_args_list[0].args[0][0]["content"]
        self.assertNotIn("submit_trade_plan", prompt)
        self.assertIn("模型不提交价格计划", prompt)

    def test_deprecated_submit_trade_plan_tool_cannot_create_a_plan(self):
        result = submit_trade_plan_tool.handler(
            symbol="600519",
            action="open",
            entry_zone_low=1470.0,
            entry_zone_high=1500.0,
            stop_loss_price=1420.0,
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("deterministic_price_plan", result["error"])
        self.assertNotIn("trade_plan", result)

    def test_waiting_entry_chat_rejects_conditional_trade_plan(self):
        registry = _make_registry_with_echo()
        registry.register(submit_trade_plan_tool)
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    id="plan-wait-1",
                    name="submit_trade_plan",
                    arguments={
                        "symbol": "600519",
                        "action": "open",
                        "current_action": "wait",
                        "entry_zone_low": 1470.0,
                        "entry_zone_high": 1500.0,
                        "stop_loss_price": 1420.0,
                        "invalidation_conditions": ["日线收盘跌破 1420"],
                    },
                )],
                usage={"total_tokens": 20},
                provider="openai",
            ),
            LLMResponse(
                content="当前不追价，等待价格进入计划区间。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
            ),
        ]
        executor = AgentExecutor(registry, adapter, max_steps=3)

        result = executor.chat(
            "分析 600519 是否适合买入",
            session_id="test-entry-wait-plan",
            context={
                "stock_code": "600519",
                "signal_lifecycle_entry_intent": True,
            },
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.trade_plan)
        prompt = adapter.call_with_tools.call_args_list[0].args[0][0]["content"]
        self.assertIn("模型不提交价格计划", prompt)
        self.assertNotIn("submit_trade_plan", prompt)

    def test_generic_single_stock_qa_rejects_llm_multi_zone_observation_plan(self):
        registry = _make_registry_with_echo()
        registry.register(submit_trade_plan_tool)
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    id="plan-observation-1",
                    name="submit_trade_plan",
                    arguments={
                        "symbol": "600330",
                        "action": "open",
                        "current_action": "reject",
                        "entry_zone_low": 19.6,
                        "entry_zone_high": 20.5,
                        "entry_zones": [
                            {"label": "ideal", "low": 19.6, "high": 20.5},
                            {"label": "secondary", "low": 21.2, "high": 22.0},
                        ],
                        "stop_loss_price": 18.5,
                    },
                )],
                usage={"total_tokens": 20},
                provider="openai",
            ),
            LLMResponse(
                content="当前不建议入场，但保留两个观察区间。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
            ),
        ]
        executor = AgentExecutor(registry, adapter, max_steps=3)

        result = executor.chat(
            "分析一下天通股份",
            session_id="test-qa-observation-plan",
            context={"signal_lifecycle_plan_candidate": True},
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.trade_plan)
        prompt = adapter.call_with_tools.call_args_list[0].args[0][0]["content"]
        self.assertIn("不得自行填写或修改入场价", prompt)
        self.assertNotIn("submit_trade_plan", prompt)

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
                content=(
                    "## 最终结论\n\n持仓策略：持有，但不提高仓位。\n\n"
                    "## 多维证据\n\n当前行情证据支持继续观察，其他维度缺失需降低确定性。\n\n"
                    "## 交易计划\n\n维持当前动作，跌破失效条件后复查并执行止损纪律。\n\n"
                    "## 策略 4 / 缠论确定性评分\n\n评分仅作影子证据，不直接替代交易判断。\n\n"
                    "## 风险与缺失\n\n仓位风险和证据缺失必须持续复核。"
                ),
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

    def test_failed_debate_does_not_replace_a_valid_primary_report(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[ToolParameter(name="stock_code", type="string", description="Stock code")],
                handler=lambda stock_code: {"price": 100, "stock_code": stock_code},
            )
        )
        primary_report = (
            "## 最终结论\n\n当前不追高，等待回踩确认。\n\n"
            "## 核心证据\n\n技术面、资金面、消息面和数据质量均已逐项检查。\n\n"
            "## 交易计划\n\n入场、止损、止盈和失效条件完整。\n\n"
            "## 策略 4 / 缠论确定性评分\n\n评分处于影子模式，临时证据不提升为正式结论。\n\n"
            "## 风险与缺失\n\n风险和缺失证据已明确披露。"
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
                content=primary_report,
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
                model="openai/tool-model",
            ),
        ]
        adapter.call_text.side_effect = [
            LLMResponse(content='{"direction":"bullish","action":"hold","summary":"主观点","evidence":["price=100"]}', usage={"total_tokens": 3}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方","evidence":["回撤风险"]}', usage={"total_tokens": 4}, provider="openai", model="openai/debate"),
            LLMResponse(content="", usage={"total_tokens": 5}, provider="openai", model="openai/debate"),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600519",
                target_symbols=["600519"],
            )
        )

        result = AgentExecutor(registry, adapter, max_steps=3).chat(
            "分析 600519 是否适合买入",
            session_id="test-debate-fallback",
            context={"stock_code": "600519", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, primary_report)
        self.assertNotIn("## 对抗式辩论裁决", result.content)
        self.assertFalse(result.debate["success"])
        self.assertEqual(result.debate["error"], "llm_json_parse_failed:judge_decision")
        self.assertEqual(result.total_tokens, 42)

    def test_truncated_opposing_json_stops_debate_before_judge(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[ToolParameter(name="stock_code", type="string", description="Stock code")],
                handler=lambda stock_code: {"price": 23.25, "stock_code": stock_code},
            )
        )
        primary_report = (
            "## 最终结论\n\n等待回踩，不追高；现价没有提供足够安全边际。\n\n"
            "## 多维证据\n\n现有价格证据支持等待，技术信号需要与资金、消息和基本面共同复核。\n\n"
            "## 交易计划\n\n等待入场条件后再决策，并设置明确止损、止盈和失效条件。\n\n"
            "## 策略 4 / 缠论确定性评分\n\n临时周线证据不提升正式分数，影子评分只用于审计。\n\n"
            "## 风险与缺失\n\n资金和消息证据不足，任何缺失都必须降低确定性和动作强度。"
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="checking",
                tool_calls=[ToolCall(id="q1", name="get_realtime_quote", arguments={"stock_code": "600330"})],
                usage={"total_tokens": 10},
                provider="openai",
                model="openai/tool-model",
            ),
            LLMResponse(content=primary_report, tool_calls=[], usage={"total_tokens": 20}, provider="openai", model="openai/tool-model"),
        ]
        adapter.call_text.side_effect = [
            LLMResponse(content='{"direction":"bullish","action":"wait","summary":"主观点"}', usage={"total_tokens": 3}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方输出被截断', usage={"total_tokens": 4}, provider="openai", model="openai/debate"),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600330",
                target_symbols=["600330"],
            )
        )

        result = AgentExecutor(registry, adapter, max_steps=3).chat(
            "天通股份可以加仓吗",
            session_id="test-truncated-opposing",
            context={"stock_code": "600330", "agent_user_context": context},
        )

        self.assertEqual(result.content, primary_report)
        self.assertFalse(result.debate["success"])
        self.assertEqual(result.debate["error"], "llm_json_parse_failed:opposing_thesis")
        self.assertEqual(adapter.call_text.call_count, 2)
        self.assertEqual(result.total_tokens, 37)

    def test_short_primary_report_is_repaired_before_debate(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[ToolParameter(name="stock_code", type="string", description="Stock code")],
                handler=lambda stock_code: {"price": 23.25, "stock_code": stock_code},
            )
        )
        repaired_report = (
            "## 最终结论\n\n当前不追高，等待价格回到计划区间并确认。\n\n"
            "## 多维证据\n\n技术面显示短期过热；资金面和消息面按现有工具证据审慎处理。\n\n"
            "## 交易计划\n\n仅在计划区间出现确认信号后执行，严格设置止损与失效条件。\n\n"
            "## 策略 4 / 缠论确定性评分\n\n正式分数与临时分数分开披露，不用临时周线提升动作等级。\n\n"
            "## 风险与缺失\n\n不得追高；缺失证据会降低确定性并触发等待。"
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="checking",
                tool_calls=[ToolCall(id="q1", name="get_realtime_quote", arguments={"stock_code": "600330"})],
                usage={"total_tokens": 10},
                provider="openai",
                model="openai/tool-model",
            ),
            LLMResponse(
                content="交易计划已记录，供后续风险复核。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
                model="openai/tool-model",
            ),
        ]
        adapter.call_text.side_effect = [
            LLMResponse(content=repaired_report, usage={"total_tokens": 6}, provider="openai", model="openai/report-repair"),
            LLMResponse(content='{"direction":"bullish","action":"wait","summary":"主观点","evidence":["price=23.25"]}', usage={"total_tokens": 3}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"direction":"neutral_bearish","action":"wait","summary":"反方","evidence":["追高风险"]}', usage={"total_tokens": 4}, provider="openai", model="openai/debate"),
            LLMResponse(content='{"winner":"no_trade","final_action":"wait","reason":"等待确认","risk_controls":["不得追高"]}', usage={"total_tokens": 5}, provider="openai", model="openai/debate"),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600330",
                target_symbols=["600330"],
            )
        )

        result = AgentExecutor(registry, adapter, max_steps=3).chat(
            "天通股份可以加仓吗",
            session_id="test-short-report-repair",
            context={"stock_code": "600330", "agent_user_context": context},
        )

        self.assertTrue(result.success)
        self.assertTrue(result.content.startswith(repaired_report))
        self.assertIn("## 对抗式辩论裁决", result.content)
        self.assertEqual(result.report_quality["status"], "repaired")
        self.assertIn("report_too_short", result.report_quality["initial_issues"])
        self.assertEqual(adapter.call_text.call_count, 4)
        self.assertEqual(result.total_tokens, 48)

    def test_failed_report_repair_uses_auditable_fallback_and_still_runs_debate(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_realtime_quote",
                description="Get realtime quote",
                parameters=[ToolParameter(name="stock_code", type="string", description="Stock code")],
                handler=lambda stock_code: {"price": 23.25, "stock_code": stock_code},
            )
        )
        adapter = _make_mock_adapter()
        adapter.call_with_tools.side_effect = [
            LLMResponse(
                content="checking",
                tool_calls=[ToolCall(id="q1", name="get_realtime_quote", arguments={"stock_code": "600330"})],
                usage={"total_tokens": 10},
                provider="openai",
                model="openai/tool-model",
            ),
            LLMResponse(
                content="交易计划已记录。",
                tool_calls=[],
                usage={"total_tokens": 20},
                provider="openai",
                model="openai/tool-model",
            ),
        ]
        adapter.call_text.side_effect = [
            LLMResponse(content="", usage={"total_tokens": 6}, provider="openai", model="openai/report-repair"),
            LLMResponse(content='{"direction":"bullish","action":"monitor","summary":"主观点"}', usage={"total_tokens": 1}),
            LLMResponse(content='{"direction":"bearish","action":"wait","summary":"反方观点"}', usage={"total_tokens": 1}),
            LLMResponse(content=json.dumps({
                "winner": "no_trade",
                "final_action": "wait",
                "decision_summary": "四维证据不足，保持观察。",
                "reason": "证据不足",
                "dimension_assessments": [
                    {"dimension": dimension, "verdict": "insufficient_data", "plan_verdict": "insufficient", "weight": "medium", "summary": "证据不足", "evidence": [], "evidence_refs": [], "reason_codes": [], "scenario_ids": [], "missing": ["该维度证据不足"]}
                    for dimension in ("technical", "capital_flow", "fundamental", "news_event")
                ],
            }, ensure_ascii=False), usage={"total_tokens": 1}),
        ]
        context = AgentUserContext(
            report=ReportContext(
                analysis_mode="planning_execute",
                intent="entry_analysis",
                primary_symbol="600330",
                target_symbols=["600330"],
            )
        )

        price_plan = {
            "plan_id": "plan-test",
            "generation_status": "generated",
            "scenarios": [{"scenario_id": "scenario-1", "role": "observation"}],
        }
        with patch.object(AgentExecutor, "_prepare_price_plan_for_debate", return_value=price_plan):
            result = AgentExecutor(registry, adapter, max_steps=3).chat(
                "天通股份可以加仓吗",
                session_id="test-report-fallback",
                context={"stock_code": "600330", "agent_user_context": context},
            )

        self.assertTrue(result.success)
        self.assertIn("## 分析状态", result.content)
        self.assertIn("模型主报告未通过完整度校验", result.content)
        self.assertIn("## 策略 4 / 缠论确定性评分", result.content)
        self.assertIn("## 对抗式辩论裁决", result.content)
        self.assertIsNotNone(result.debate)
        self.assertTrue(result.debate["success"])
        self.assertEqual(result.report_quality["status"], "fallback")
        self.assertEqual(result.report_quality["source"], "deterministic_fallback")
        self.assertEqual(adapter.call_text.call_count, 4)
        self.assertEqual(result.total_tokens, 39)


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
