"""Trace delivery, frozen evidence and read-only Jev comparison contracts."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks
import pytest

from api.v1.endpoints import agent
from src.services.history_loader import capture_history_reads, _observed_history
from src.services.jev_evaluation_service import JevEvaluationService
from src.services.jev_client import JevClientError
from src.services.jev_trace_service import JevTraceService, compare_trace_review, trace_baseline
from tests.test_jev_evaluation import bars, config, repository, response  # noqa: F401


def _result():
    return SimpleNamespace(success=True, content="original report", stock_selection=None, price_plan=None,
                           error=None, total_steps=1, total_tokens=10, provider="unit", model="unit",
                           trade_plan_result=None, tool_calls_log=[],
                           debate={"success": True, "judge_decision": {"final_action": "open", "reason": "original evidence"}})


def _context():
    return {"stock_code": "600519", "as_of": "2026-07-24", "agent_user_context": {
        "report": {"primary_symbol": "600519", "target_symbols": ["600519"], "intent": "entry_analysis"},
        "investor": {"trading_horizon": "swing"},
    }}


@pytest.fixture
def trace_service(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 10}
    loader = MagicMock(side_effect=AssertionError("Trace must not reload daily data"))
    evaluator = JevEvaluationService(config=config(), repository=repository, client=client, history_loader=loader)
    return JevTraceService(evaluator)


def _prepare(service, path, *, result=None, histories=None):
    return service.prepare(path=path, session_id="trace-unit", symbol="600519", result=result or _result(),
                           context=_context(), risk_gate={"risk_gate": {"allowed_action": "wait", "blocked_reasons": ["account cap"]}},
                           histories=histories if histories is not None else {"600519": (bars(), "trace-test")})


def test_review_freezes_inputs_and_preserves_original_report_and_risk_action(trace_service, tmp_path):
    result = _result()
    before = deepcopy(result.__dict__)
    frame = bars()
    prepared = _prepare(trace_service, tmp_path, result=result, histories={"600519": (frame, "trace-test")})
    assert prepared["status"] == "pending"
    assert prepared["baseline"]["action"] == "wait"
    trace_service.evaluator.client.evaluate.assert_not_called()
    frame.loc[39, "close"] = 99999
    trace_service.finish(tmp_path, prepared)
    stored = trace_service.read(tmp_path, "trace-unit")
    assert stored["status"] == "ok"
    assert stored["evaluation"]["state"]["metrics"]["last_close"] == 139
    assert stored["baseline"]["action"] == "wait"
    assert result.__dict__ == before
    assert "jev_review_done" in (tmp_path / "events.ndjson").read_text()


def test_missing_snapshot_does_not_reload_or_call_provider(trace_service, tmp_path):
    review = _prepare(trace_service, tmp_path, histories={})
    assert review["status"] == "insufficient_data"
    assert review["reason"] == "trace_history_unavailable"
    trace_service.evaluator.history_loader.assert_not_called()
    trace_service.evaluator.client.evaluate.assert_not_called()


def test_provider_failure_stays_in_review_without_changing_original(trace_service, tmp_path):
    prepared = _prepare(trace_service, tmp_path)
    trace_service.evaluator.client.evaluate.side_effect = JevClientError("timeout")
    trace_service.finish(tmp_path, prepared)
    review = trace_service.read(tmp_path, "trace-unit")
    assert review["status"] == "error" and review["reason"] == "timeout"
    assert review["baseline"]["action"] == "wait"


def test_expired_and_superseded_jobs_are_not_misattached(trace_service, tmp_path):
    old = _prepare(trace_service, tmp_path)
    new = _prepare(trace_service, tmp_path)
    trace_service.finish(tmp_path, old)
    trace_service.evaluator.client.evaluate.assert_not_called()
    assert trace_service.read(tmp_path, "trace-unit")["capture_id"] == new["capture_id"]
    new["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    trace_service._write(tmp_path, new)
    assert trace_service.read(tmp_path, "trace-unit")["status"] == "interrupted"


def test_legacy_lookup_is_exactly_trace_scoped_and_never_runs_inference(trace_service, tmp_path):
    repository = trace_service.evaluator.repo
    for source_ref, source in [("trace-other", "agent"), ("trace-unit", "manual")]:
        payload = trace_service.evaluator.prepare("600519", as_of=date(2026, 7, 24), source=source,
                                                source_ref=source_ref, daily_snapshot=(bars(), "unit"))
        trace_service.evaluator.evaluate_prepared(payload)
    trace_service.evaluator.client.evaluate.reset_mock()
    assert trace_service.read(tmp_path, "trace-unit")["status"] == "not_recorded"
    assert len(repository.list_recent()) == 2
    trace_service.evaluator.client.evaluate.assert_not_called()
    assert not (tmp_path / "jev.json").exists()


@pytest.mark.parametrize("action,horizon,expected", [
    ("open", "swing", "divergent"), ("hold", "short_term", "divergent"),
    ("open", "long_term", "not_comparable"), ("wait", "swing", "no_direct_conflict"),
    (None, "swing", "not_comparable"),
])
def test_comparison_respects_final_action_and_horizon(action, horizon, expected):
    evaluation = {"status": "ok", "answers": {
        f"{kind}_{day}": {"choice": choice} for day in (1, 3, 5)
        for kind, choice in [("direction", "bearish"), ("risk", "high"), ("entry", "wait")]
    }}
    before = deepcopy(evaluation)
    comparison = compare_trace_review(evaluation, {"action": action, "trading_horizon": horizon}, shared_snapshot=True)
    assert comparison["status"] == expected
    assert evaluation == before
    assert compare_trace_review(evaluation, {"action": "open"}, shared_snapshot=False)["status"] == "not_comparable"


def test_insufficient_answers_are_never_labeled_agreement():
    evaluation = {"status": "ok", "answers": {"direction_1": {"choice": "insufficient"}}}
    assert compare_trace_review(evaluation, {"action": "open", "trading_horizon": "swing"}, shared_snapshot=True)["status"] == "not_comparable"


def test_history_capture_is_scoped_and_owns_its_data():
    frame = bars()
    with capture_history_reads() as outer:
        _observed_history("600519", frame, "unit")
        with capture_history_reads() as inner:
            _observed_history("AAPL", frame, "unit")
        assert "AAPL" not in outer
        assert "600519" not in inner
    frame.loc[39, "close"] = 999
    assert outer["600519"][0].iloc[-1]["close"] == 139
    _observed_history("AAPL", frame, "unit")
    assert "AAPL" not in outer


@pytest.mark.parametrize("stream", [False, True])
def test_api_delivers_original_report_before_background_provider_call(trace_service, tmp_path, stream):
    cfg = config()
    cfg.is_agent_available = lambda: True
    executor = MagicMock()

    def chat(**kwargs):
        _observed_history("600519", bars(), "trace-test")
        return _result()

    executor.chat.side_effect = chat
    tasks = BackgroundTasks()
    request = agent.AgentTraceRunRequest(message="600519", stock_code="600519", inject_portfolio_context=False)
    with patch.object(agent, "get_config", return_value=cfg), \
            patch.object(agent, "_build_trace_context", return_value=_context()), \
            patch.object(agent, "_prefetch_trace_composite_verdict", return_value=None), \
            patch.object(agent, "_prepare_signal_lifecycle_context"), \
            patch.object(agent, "_build_executor", return_value=executor), \
            patch.object(agent, "_trace_artifact_root", return_value=tmp_path), \
            patch.object(agent, "_safe_artifact_finalize"), \
            patch.object(agent, "_capture_signal_lifecycle") as capture, \
            patch("src.agent.conversation.conversation_manager.clear"), \
            patch("src.storage.get_db", return_value=MagicMock()), \
            patch("src.services.jev_trace_service.JevTraceService", return_value=trace_service):
        async def exercise():
            if stream:
                response = await agent.stream_agent_trace(request, tasks)
                chunks = [chunk async for chunk in response.body_iterator]
                assert '"content": "original report"' in "".join(chunks)
                assert '"status": "pending"' in "".join(chunks)
            else:
                response = await agent.run_agent_trace(request, tasks)
                assert response.content == "original report"
                assert response.jev_review["status"] == "pending"
            trace_service.evaluator.client.evaluate.assert_not_called()
            assert capture.call_args.kwargs["capture_jev"] is False
            await tasks()
            trace_service.evaluator.client.evaluate.assert_called_once()
        asyncio.run(exercise())


def test_failed_debate_cannot_supply_comparison_baseline():
    result = _result()
    result.debate["success"] = False
    assert trace_baseline(result, _context(), None)["action"] is None
