import json
from types import SimpleNamespace

from api.v1.endpoints.agent import (
    AgentTraceRunRequest,
    TraceArtifactWriter,
    _build_judge_sanity_summary,
    _build_llm_telemetry_summary,
)


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
        stock_selection=stock_selection,
    )

    writer.finalize(result=result)

    llm_summary = json.loads((writer.path / "llm_telemetry.json").read_text(encoding="utf-8"))
    judge_summary = json.loads((writer.path / "judge_sanity.json").read_text(encoding="utf-8"))
    summary = json.loads((writer.path / "summary.json").read_text(encoding="utf-8"))
    assert llm_summary["total_calls"] == 1
    assert llm_summary["total_tokens"] == 150
    assert judge_summary["final_action"] == "wait"
    assert judge_summary["sanity_checks"][0]["rule_id"] == "open_without_position_plan"
    assert summary["llm_telemetry"]["total_calls"] == 1
    assert summary["judge_sanity"]["required_change_count"] == 1
