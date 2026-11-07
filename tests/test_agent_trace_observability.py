import json
import time
from types import SimpleNamespace

from api.v1.endpoints.agent import (
    AgentTraceRunRequest,
    TraceArtifactWriter,
    _build_judge_sanity_summary,
    _build_llm_telemetry_summary,
    _trace_history_item_from_artifact,
)


def test_trace_read_hides_internal_json_and_recovers_forecast_fundamentals(tmp_path):
    trace_dir = tmp_path / "20260813-trace-readable"
    trace_dir.mkdir()
    original = """基于证据，以下是决策结果。

```json
{"action":"reject","summary":"当前不宜追高"}
```

---

## 最终结论

当前不宜追高。

## 多维证据

基本面存在亏损风险。
"""
    (trace_dir / "final.md").write_text(original, encoding="utf-8")
    (trace_dir / "summary.json").write_text('{"success":true}', encoding="utf-8")
    (trace_dir / "request.json").write_text(
        '{"message":"分析天通股份","analysis_mode":"planning_execute"}',
        encoding="utf-8",
    )
    (trace_dir / "context.json").write_text('{}', encoding="utf-8")
    (trace_dir / "tool_calls.json").write_text(json.dumps([{
        "tool": "get_tushare_forecast",
        "success": True,
        "result_preview": (
            '{"status":"ok","items":[{"ann_date":"20260715",'
            '"type":"首亏","net_profit_min":-30000,"net_profit_max":-22000}]}'
        ),
    }], ensure_ascii=False), encoding="utf-8")
    (trace_dir / "dimensions.json").write_text(json.dumps({
        "fundamental": {
            "stage": "fundamental",
            "status": "degraded",
            "source": "single_agent_tool_evidence",
            "quality": {
                "dimension": "fundamental",
                "quality": "unavailable",
                "available_tools": [],
                "partial_tools": [],
                "failed_tools": ["get_stock_info"],
                "missing_evidence": ["fundamental_evidence_unavailable"],
            },
            "opinion": {
                "quality": "unavailable",
                "evidence_tools": [],
                "missing_evidence": ["fundamental_evidence_unavailable"],
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    item = _trace_history_item_from_artifact("trace-readable", trace_dir)

    assert item["result"]["content"].startswith("## 最终结论")
    assert "```json" not in item["result"]["content"]
    assert item["result"]["dimension_evidence"]["fundamental"]["quality"]["quality"] == "partial"
    assert item["result"]["dimension_evidence"]["fundamental"]["opinion"]["quality"] == "partial"
    assert (trace_dir / "final.md").read_text(encoding="utf-8") == original


def test_report_presentation_handles_large_internal_json_without_regex_backtracking():
    from src.agent.report_presentation import normalize_planning_execute_final_content

    payload = {
        "action": "reject",
        "summary": "暂不入场",
        "evidence": {"technical": [f"证据 {index}" for index in range(5000)]},
    }
    content = (
        "基于证据，以下是决策结果。\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n\n---\n\n## 最终结论\n\n暂不入场。"
    )

    started = time.perf_counter()
    normalized = normalize_planning_execute_final_content(content)

    assert time.perf_counter() - started < 0.5
    assert normalized == "## 最终结论\n\n暂不入场。"


def test_trace_read_hides_legacy_fundamental_supplement_without_rewriting_artifact(tmp_path):
    trace_dir = tmp_path / "20260813-trace-legacy-fundamental-supplement"
    trace_dir.mkdir()
    original = """## 最终结论

暂不入场。

## 基本面补充资料

以下内容来自公开网页检索。

- **财务表现**：[网页摘要](https://example.com/report)

<!-- deterministic-price-plan:start -->
## 确定性价格计划

- 计划状态：仅观察
<!-- deterministic-price-plan:end -->
"""
    (trace_dir / "final.md").write_text(original, encoding="utf-8")
    (trace_dir / "summary.json").write_text('{"success":true}', encoding="utf-8")
    (trace_dir / "request.json").write_text(
        '{"message":"分析天通股份","analysis_mode":"planning_execute"}',
        encoding="utf-8",
    )
    (trace_dir / "context.json").write_text('{}', encoding="utf-8")
    (trace_dir / "tool_calls.json").write_text('[]', encoding="utf-8")

    item = _trace_history_item_from_artifact(
        "trace-legacy-fundamental-supplement",
        trace_dir,
    )

    content = item["result"]["content"]
    assert "基本面补充资料" not in content
    assert "网页摘要" not in content
    assert "<!-- deterministic-price-plan:start -->" in content
    assert "## 确定性价格计划" in content
    assert (trace_dir / "final.md").read_text(encoding="utf-8") == original


def test_build_llm_telemetry_summary(tmp_path):
    events = [
        {
            "ok": True,
            "stage": "candidate_screening",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "latency_ms": 1200.5,
            "estimated_cost": 0.001,
        },
        {
            "ok": False,
            "stage": "judge_decision",
            "input_tokens": 80,
            "output_tokens": 0,
            "total_tokens": 80,
            "latency_ms": 300.0,
            "estimated_cost": 0.0004,
        },
    ]
    (tmp_path / "llm_usage.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events),
        encoding="utf-8",
    )

    summary = _build_llm_telemetry_summary(tmp_path)

    assert summary["total_calls"] == 2
    assert summary["ok_calls"] == 1
    assert summary["failed_calls"] == 1
    assert summary["input_tokens"] == 180
    assert summary["output_tokens"] == 50
    assert summary["total_tokens"] == 230
    assert summary["total_latency_ms"] == 1500.5
    assert summary["estimated_cost"] == 0.0014
    assert {item["stage"] for item in summary["by_stage"]} == {"candidate_screening", "judge_decision"}


def test_build_judge_sanity_summary_extracts_plan_changes():
    stock_selection = {
        "final_report_json": {
            "judge_decision": {
                "summary": {
                    "final_action": "watch",
                    "decision_summary": "等待回踩确认。",
                },
                "full": {
                    "primary_plan_verdict": "downgraded",
                    "sanity_checks": [
                        {
                            "rule_id": "open_without_position_plan",
                            "action": "downgrade",
                            "from_action": "open",
                            "to_action": "watch",
                            "reason": "缺少明确入场条件。",
                        }
                    ],
                    "required_plan_changes": [
                        {"field": "action", "from": "open", "to": "watch"}
                    ],
                },
            },
        },
    }

    summary = _build_judge_sanity_summary(stock_selection)

    assert summary is not None
    assert summary["final_action"] == "watch"
    assert summary["primary_plan_verdict"] == "downgraded"
    assert summary["decision_summary"] == "等待回踩确认。"
    assert summary["check_count"] == 1
    assert summary["required_change_count"] == 1
    assert summary["sanity_checks"][0]["rule_id"] == "open_without_position_plan"


def test_trace_artifact_finalize_writes_observability_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr("api.v1.endpoints.agent._trace_artifact_root", lambda: tmp_path)
    writer = TraceArtifactWriter("trace-observability-test")
    request = AgentTraceRunRequest(message="帮我选一下下周可以入手的股票")
    writer.initialize(request=request, context={})
    (writer.path / "llm_usage.jsonl").write_text(
        json.dumps(
            {
                "ok": True,
                "stage": "judge_decision",
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "latency_ms": 800,
                "estimated_cost": 0.001,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    stock_selection = {
        "final_report_json": {
            "judge_decision": {
                "summary": {"final_action": "wait", "decision_summary": "降级等待。"},
                "full": {
                    "primary_plan_verdict": "downgraded",
                    "sanity_checks": [{"rule_id": "open_without_position_plan", "action": "downgrade"}],
                    "required_plan_changes": [{"field": "action", "to": "wait"}],
                },
            }
        },
        "selection_context": {"stages": {}},
    }
    result = SimpleNamespace(
        success=True,
        error=None,
        total_steps=0,
        total_tokens=0,
        provider="smoke",
        model="local-json",
        content="ok",
        tool_calls_log=[],
        debate=None,
        report_quality={
            "status": "fallback",
            "initial_issues": ["report_too_short"],
            "source": "deterministic_fallback",
        },
        stock_selection=stock_selection,
    )

    writer.finalize(result=result)

    llm_summary = json.loads((writer.path / "llm_telemetry.json").read_text(encoding="utf-8"))
    judge_summary = json.loads((writer.path / "judge_sanity.json").read_text(encoding="utf-8"))
    summary = json.loads((writer.path / "summary.json").read_text(encoding="utf-8"))
    report_quality = json.loads((writer.path / "report_quality.json").read_text(encoding="utf-8"))
    assert llm_summary["total_calls"] == 1
    assert llm_summary["total_tokens"] == 150
    assert judge_summary["final_action"] == "wait"
    assert judge_summary["sanity_checks"][0]["rule_id"] == "open_without_position_plan"
    assert summary["llm_telemetry"]["total_calls"] == 1
    assert summary["judge_sanity"]["required_change_count"] == 1
    assert report_quality["status"] == "fallback"
    assert summary["report_quality"]["initial_issues"] == ["report_too_short"]


def test_trace_artifact_writer_aggregates_dimension_opinions(tmp_path, monkeypatch):
    monkeypatch.setattr("api.v1.endpoints.agent._trace_artifact_root", lambda: tmp_path)
    writer = TraceArtifactWriter("trace-dimensions-test")
    request = AgentTraceRunRequest(message="分析 600519")
    writer.initialize(request=request, context={})

    writer.append_event({
        "type": "stage_done",
        "stage": "capital",
        "status": "completed",
        "duration": 1.2,
        "dimension_status": {
            "quality": "partial",
            "missing_evidence": ["margin_trading"],
        },
        "dimension_opinion": {
            "signal": "hold",
            "confidence": 0.6,
            "reasoning": "资金证据部分可用",
            "quality": "partial",
            "missing_evidence": ["margin_trading"],
        },
    })

    dimensions = json.loads((writer.path / "dimensions.json").read_text(encoding="utf-8"))
    assert dimensions["capital"]["quality"]["quality"] == "partial"
    assert dimensions["capital"]["opinion"]["missing_evidence"] == ["margin_trading"]


def test_trace_artifact_writer_builds_dimensions_from_single_agent_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("api.v1.endpoints.agent._trace_artifact_root", lambda: tmp_path)
    writer = TraceArtifactWriter("trace-single-agent-dimensions")
    request = AgentTraceRunRequest(message="分析太极实业")
    writer.initialize(request=request, context={})
    tool_calls = [
        {
            "step": 1,
            "tool": "search_comprehensive_intel",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","intel":{"items":[{"title":"公司公告"}]}}',
        },
        {
            "step": 2,
            "tool": "get_tushare_financial_indicators",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","items":[{"roe":8.4}]}',
        },
    ]
    result = SimpleNamespace(
        success=True,
        content="ok",
        error=None,
        total_steps=2,
        total_tokens=0,
        provider="test",
        model="test",
        tool_calls_log=tool_calls,
        debate=None,
        report_quality=None,
        stock_selection=None,
    )

    writer.finalize(result=result)

    dimensions = json.loads((writer.path / "dimensions.json").read_text(encoding="utf-8"))
    assert dimensions["intel"]["quality"]["quality"] != "unavailable"
    assert dimensions["fundamental"]["quality"]["quality"] != "unavailable"
    assert dimensions["intel"]["source"] == "single_agent_tool_evidence"


def test_trace_finalize_preserves_multi_agent_opinion_and_adds_tool_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr("api.v1.endpoints.agent._trace_artifact_root", lambda: tmp_path)
    writer = TraceArtifactWriter("trace-multi-agent-dimensions")
    request = AgentTraceRunRequest(message="分析太极实业")
    writer.initialize(request=request, context={})
    opinion = {
        "signal": "hold",
        "confidence": 0.6,
        "quality": "partial",
        "reasoning": "财务数据部分可用",
        "missing_evidence": ["cash_flow"],
    }
    writer.append_event({
        "type": "stage_done",
        "stage": "fundamental",
        "status": "completed",
        "duration": 1.0,
        "dimension_status": {"quality": "partial", "missing_evidence": ["cash_flow"]},
        "dimension_opinion": opinion,
    })
    result = SimpleNamespace(
        success=True,
        content="ok",
        error=None,
        total_steps=1,
        total_tokens=0,
        provider="test",
        model="test",
        tool_calls_log=[{
            "step": 1,
            "tool": "get_tushare_financial_indicators",
            "arguments": {"stock_code": "600667"},
            "success": True,
            "result_preview": '{"status":"ok","items":[{"roe":8.4}]}',
        }],
        debate=None,
        report_quality=None,
        stock_selection=None,
    )

    writer.finalize(result=result)

    dimensions = json.loads((writer.path / "dimensions.json").read_text(encoding="utf-8"))
    assert dimensions["fundamental"]["opinion"] == opinion
    assert dimensions["fundamental"]["tool_evidence"]["available_tools"] == [
        "get_tushare_financial_indicators"
    ]
