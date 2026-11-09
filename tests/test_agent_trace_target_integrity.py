"""A saved stock selection must not override a newly named Trace target."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api.v1.endpoints import agent


@pytest.mark.parametrize("message,code", [
    ("分析立讯精密 002475 未来 1 至 5 个交易日", "600519"),
    ("分析腾讯 HK00700", "600519"),
    ("分析 AAPL 的 MACD", "MSFT"),
])
def test_trace_rejects_conflicting_explicit_target_before_loading_data(message, code):
    request = agent.AgentTraceRunRequest(message=message, stock_code=code, inject_portfolio_context=False)
    with patch.object(agent, "get_config", return_value=SimpleNamespace(is_agent_available=lambda: True)), \
            patch.object(agent, "_prefetch_trace_composite_verdict", side_effect=AssertionError("Conflicting symbols reached market-data prefetch")) as prefetch, \
            patch.object(agent, "_build_executor") as executor:
        for endpoint in (agent.run_agent_trace, agent.stream_agent_trace):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(endpoint(request))
            assert exc.value.status_code == 422
            assert exc.value.detail["error"] == "trace_target_conflict"
        prefetch.assert_not_called()
        executor.assert_not_called()


@pytest.mark.parametrize("message,code", [
    ("分析 002475.SZ", "SZ002475"),
    ("分析 700.HK", "HK00700"),
    ("分析 AAPL 的 MACD", "AAPL"),
    ("比较 002475 和 600519", "002475"),
])
def test_trace_accepts_equivalent_symbols_and_explicit_target_in_comparison(message, code):
    context = agent._build_trace_context(
        request=agent.AgentTraceRunRequest(message=message, stock_code=code, inject_portfolio_context=False),
        config=SimpleNamespace(report_language="zh"),
    )
    assert context["stock_code"]


@pytest.fixture
def mismatched_trace(tmp_path):
    files = {
        "request.json": {"message": "分析立讯精密 002475", "stock_code": "600519"},
        "context.json": {"agent_user_context": {"report": {
            "intent": "entry_analysis", "primary_symbol": "600519", "target_symbols": ["600519"],
            "user_prompt": "分析立讯精密 002475",
        }}},
        "summary.json": {"success": True},
        "composite_verdict.json": {"status": "ok", "snapshot": {"symbol": "600519"}},
        "risk_gate.json": {"trade_plan": {"symbol": "600519", "action": "open"},
                           "risk_gate": {"allowed_action": "open"}},
        "debate.json": {"enabled": True, "success": True, "judge_decision": {"final_action": "open"}},
        "tool_calls.json": [{"tool": "analyze_trend", "success": True, "arguments": {"stock_code": "002475"}}],
    }
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "final.md").write_text(
        "立讯精密分析\n<!-- deterministic-price-plan:start -->\n"
        "入场区间：1268.49 元 - 1272.17 元\n<!-- deterministic-price-plan:end -->", encoding="utf-8",
    )
    return tmp_path


def test_history_quarantines_wrong_security_price_and_decision_without_mutating_files(mismatched_trace):
    before = {path.name: path.read_bytes() for path in mismatched_trace.iterdir()}
    result = agent._trace_history_item_from_artifact("trace-mismatch", mismatched_trace)["result"]
    assert result["success"] is False
    assert "002475" in result["error"] and "600519" in result["error"]
    assert "1268.49" not in result["content"]
    assert result["report_quality"]["status"] == "target_mismatch"
    assert result["risk_gate"]["error_code"] == "target_symbol_mismatch"
    assert result["composite_verdict"]["error_code"] == "target_symbol_mismatch"
    assert result["debate"]["success"] is False
    assert not result["debate"].get("judge_decision")
    assert {path.name: path.read_bytes() for path in mismatched_trace.iterdir()} == before


def test_mismatched_trace_cannot_be_resumed_with_its_old_evidence(mismatched_trace):
    with patch.object(agent, "_find_trace_artifact_dir", return_value=mismatched_trace):
        with pytest.raises(HTTPException) as exc:
            agent._build_trace_context(
                request=agent.AgentTraceRunRequest(message="分析 002475", resume_from_session_id="trace-mismatch"),
                config=SimpleNamespace(report_language="zh"),
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "resume_trace_target_conflict"
