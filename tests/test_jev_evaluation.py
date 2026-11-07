"""Deterministic coverage of the Jev boundary, chronology and shadow guarantees."""

from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd
import pytest

from src.config import Config
from src.repositories.jev_repo import JevRepository
from src.services.jev_client import JevClient, JevClientError
from src.services.jev_evaluation_service import (
    JevEvaluationService, build_questions, completed_session, prepare_daily_state,
)
from src.storage import DatabaseManager


def bars(end="2026-07-24", count=40):
    return pd.DataFrame([
        {"date": timestamp.date(), "open": 100 + n, "close": 100 + n,
         "high": 102 + n, "low": 98 + n, "volume": 1000 + n}
        for n, timestamp in enumerate(pd.bdate_range(end=end, periods=count))
    ])


def response(questions):
    return {
        "model": "jev-test-resolved",
        "answers": {key: {"type": "choice", "choice": next(iter(q["criteria"])), "confidence": 0.8,
                          "probabilities": {option: 1.0 if index == 0 else 0.0
                                            for index, option in enumerate(q["criteria"])}}
                    for key, q in questions.items()},
        "usage": {"input_tokens": 42, "output_tokens": 12},
    }


def config():
    return Config(jev_enabled=True, jev_api_key="unit-secret")


@pytest.fixture
def repository(tmp_path):
    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'jev.db'}")
    yield JevRepository(db)
    DatabaseManager.reset_instance()


@pytest.mark.parametrize("symbol,market", [("600519", "cn"), ("hk00700", "hk"), ("AAPL", "us")])
def test_daily_state_uses_only_completed_bars_and_never_invents_prices(symbol, market):
    original = bars(end="2026-07-27")
    original.loc[len(original) - 1, "close"] = 99999
    state = prepare_daily_state(symbol, market, date(2026, 7, 24), original, "unit")
    assert state["as_of"] == "2026-07-24"
    assert state["metrics"]["last_close"] != 99999
    assert state["recent_bars"][-1]["date"] == "2026-07-24"
    assert state["metrics"]["trend"] == "close_above_ma5_above_ma20"
    assert state["price_plan"]["status"] == "unavailable"
    questions = build_questions(state)
    assert set(questions) == {f"{kind}_{n}" for kind in ("direction", "risk") for n in (1, 3, 5)}
    assert all("insufficient" in q["criteria"] for q in questions.values())


@pytest.mark.parametrize("problem", ["stale", "duplicate", "nan", "invalid_ohlc", "short"])
def test_bad_daily_data_is_rejected_before_inference(problem):
    frame = bars()
    if problem == "stale":
        frame = frame.iloc[:-1]
    elif problem == "duplicate":
        frame = pd.concat([frame, frame.tail(1)])
    elif problem == "nan":
        frame.loc[5, "volume"] = float("nan")
    elif problem == "invalid_ohlc":
        frame.loc[5, "low"] = 99999
    else:
        frame = frame.tail(10)
    with pytest.raises(ValueError):
        prepare_daily_state("600519", "cn", date(2026, 7, 24), frame, "unit")


def test_calendars_respect_close_dst_and_holidays():
    assert completed_session("us", now=datetime(2026, 7, 24, 19, tzinfo=timezone.utc)) == date(2026, 7, 23)
    assert completed_session("us", now=datetime(2026, 7, 24, 21, tzinfo=timezone.utc)) == date(2026, 7, 24)
    assert completed_session("hk", target=date(2026, 7, 1), now=datetime(2026, 7, 2, 12, tzinfo=timezone.utc)) == date(2026, 6, 30)
    with pytest.raises(ValueError, match="future_date"):
        completed_session("cn", date(2030, 1, 1), now=datetime(2026, 7, 24, tzinfo=timezone.utc))


def test_client_official_contract_and_rate_limit_retry():
    calls = []
    questions = build_questions({"price_plan": {"status": "unavailable"}})

    def handle(request):
        import json
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer unit-secret"
        assert request.url.path == "/v1/systemone"
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=response(questions))

    sleep = MagicMock()
    result = JevClient(config(), transport=httpx.MockTransport(handle), sleep=sleep).evaluate({"daily": True}, questions)
    assert len(calls) == 2
    assert calls[0]["questions"] == questions
    assert result["model"] == "jev-test-resolved"
    assert "unit-secret" not in str(result)
    sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize("problem", ["missing", "nan", "unknown_choice", "bad_sum", "non_max_choice"])
def test_client_rejects_invalid_answers(problem):
    questions = build_questions({"price_plan": {"status": "unavailable"}})
    payload = response(questions)
    answer = payload["answers"]["direction_1"]
    if problem == "missing":
        del payload["answers"]["risk_5"]
    elif problem == "nan":
        answer["confidence"] = "NaN"
    elif problem == "unknown_choice":
        answer["choice"] = "execute"
    elif problem == "bad_sum":
        answer["probabilities"]["neutral"] = 0.5
    else:
        answer["choice"] = "bearish"
    client = JevClient(config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(JevClientError, match="invalid_response"):
        client.evaluate({}, questions)


def test_auth_error_does_not_echo_provider_response_or_key():
    client = JevClient(config(), transport=httpx.MockTransport(lambda _: httpx.Response(401, text="unit-secret")))
    with pytest.raises(JevClientError) as error:
        client.evaluate({}, {})
    assert str(error.value) == "http_401"
    assert "unit-secret" not in repr(config())


def test_success_is_cached_and_failure_can_recover(repository):
    client = MagicMock()
    client.evaluate.side_effect = JevClientError("timeout")
    service = JevEvaluationService(config=config(), repository=repository, client=client,
                                   history_loader=lambda *args, **kwargs: (bars(), "unit"))
    failed = service.evaluate("600519", as_of=date(2026, 7, 24))
    assert failed["status"] == "error"
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 20}
    recovered = service.evaluate("600519", as_of=date(2026, 7, 24))
    cached = service.evaluate("600519", as_of=date(2026, 7, 24))
    assert recovered["status"] == "ok"
    assert cached["cached"] is True
    assert client.evaluate.call_count == 2
    assert len(repository.list_recent()) == 2
    assert recovered["shadow_mode"] is True and recovered["execution_eligible"] is False


def test_disabled_and_insufficient_data_do_not_call_provider(repository):
    client = MagicMock()
    loader = MagicMock(return_value=(bars(count=2), "unit"))
    disabled = JevEvaluationService(config=Config(), repository=repository, client=client, history_loader=loader)
    assert disabled.evaluate("AAPL")["status"] == "disabled"
    loader.assert_not_called()
    enabled = JevEvaluationService(config=config(), repository=repository, client=client, history_loader=loader)
    assert enabled.evaluate("AAPL", as_of=date(2026, 7, 24))["status"] == "insufficient_data"
    client.evaluate.assert_not_called()


def test_agent_capture_preserves_decision_and_uses_position_cost(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 1}
    service = JevEvaluationService(config=config(), repository=repository, client=client,
                                   history_loader=lambda *args, **kwargs: (bars(), "unit"))
    result = SimpleNamespace(success=True, content="unchanged", stock_selection=None, price_plan=None,
                             trade_plan_result=None, deterministic_risk_gate={"allowed_action": "wait"})
    before = deepcopy(result.__dict__)
    observation = service.capture_agent_result(stock_code="AAPL", session_id="test", result=result, context={
        "as_of": "2026-07-24", "agent_user_context": {
            "report": {"target_symbols": ["AAPL"]},
            "positions": [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100}],
        },
    })
    assert observation["purpose"] == "position"
    assert observation["state"]["position"]["unrealized_price_return_pct"] == 39
    assert result.__dict__ == before
    result.stock_selection = {}
    assert service.capture_agent_result(stock_code="AAPL", session_id="test", result=result) is None


def test_outcomes_use_exchange_sessions_and_detect_price_basis_change(repository):
    frame = bars(end="2026-07-31", count=45)
    start = date(2026, 7, 24)
    reference = float(frame.loc[frame["date"] == start, "close"].iloc[0])
    service = JevEvaluationService(config=config(), repository=repository,
                                   history_loader=lambda *args, **kwargs: (frame, "unit"))
    evaluation = {"status": "ok", "stock_code": "AAPL", "market": "us", "as_of": start.isoformat(),
                  "state": {"metrics": {"last_close": reference}}}
    observations = service.outcomes(evaluation)
    assert [item["target_date"] for item in observations] == ["2026-07-27", "2026-07-29", "2026-07-31"]
    assert all(item["status"] == "observed" for item in observations)
    evaluation["state"]["metrics"]["last_close"] = reference / 2
    assert all(item["status"] == "price_basis_changed" for item in service.outcomes(evaluation))


@pytest.mark.parametrize("mismatch", [None, "symbol", "market", "date", "expired"])
def test_entry_questions_require_matching_live_deterministic_plan(mismatch, repository):
    from tests.test_signal_lifecycle_agent_api import _structured_price_plan

    artifact = _structured_price_plan("600519").price_plan
    payload = artifact.model_dump(mode="json")
    target = artifact.actual_as_of_session
    assert any(scenario.role != "observation" for scenario in artifact.scenarios)
    if mismatch == "symbol":
        payload["symbol"] = "000001"
    elif mismatch == "market":
        payload["market"] = "hk"
    elif mismatch == "date":
        payload["actual_as_of_session"] = "2020-01-01"
    elif mismatch == "expired":
        for scenario in payload["scenarios"]:
            scenario["confirmation_deadline"] = "2020-01-01"
    state = prepare_daily_state("600519", "cn", target, bars(end=target), "unit", price_plan=payload)
    assert (state["price_plan"]["status"] == "available") is (mismatch is None)
    assert ("entry_5" in build_questions(state)) is (mismatch is None)
    if mismatch is None:
        expected = {item["scenario_id"]: item["entry_reference"] for item in payload["scenarios"]}
        assert all(item["entry_reference"] == expected[item["scenario_id"]] for item in state["price_plan"]["scenarios"])


def test_current_evidence_is_symbol_scoped_and_timestamp_does_not_bypass_cache(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 1}
    service = JevEvaluationService(config=config(), repository=repository, client=client,
                                   history_loader=lambda *args, **kwargs: (bars(), "unit"))
    calls = [{"tool": "get_capital_flow", "success": True, "arguments": {"stock_code": code},
              "result_preview": preview} for code, preview in [("600519", "net inflow"), ("000001", "unrelated evidence")]]
    with patch("src.services.jev_evaluation_service.completed_session", return_value=date(2026, 7, 24)):
        first = service.evaluate("600519", tool_calls=calls)
        second = service.evaluate("600519", tool_calls=calls)
    assert first["evaluation_mode"] == "forward_shadow"
    assert first["state"]["agent_evidence"]["capital"][0]["summary"] == "net inflow"
    assert "unrelated evidence" not in str(first["state"])
    assert "capital" not in first["state"]["missing_evidence"]
    assert second["cached"] is True
    client.evaluate.assert_called_once()


def test_historical_replay_excludes_current_agent_evidence(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 1}
    service = JevEvaluationService(config=config(), repository=repository, client=client,
                                   history_loader=lambda *args, **kwargs: (bars(), "unit"))
    with patch("src.services.jev_evaluation_service.completed_session", side_effect=[date(2026, 7, 27), date(2026, 7, 24)]):
        result = service.evaluate("600519", as_of=date(2026, 7, 24), tool_calls=[{
            "tool": "get_capital_flow", "success": True, "arguments": {"stock_code": "600519"},
            "result_preview": "future evidence",
        }])
    assert result["evaluation_mode"] == "historical_replay"
    assert "agent_evidence" not in result["state"]
    assert "future evidence" not in str(client.evaluate.call_args)


def test_explicit_missing_history_refresh_is_persisted(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 1}
    service = JevEvaluationService(config=config(), repository=repository, client=client,
                                   history_loader=lambda *args, **kwargs: (None, "none"))
    with patch("src.services.jev_evaluation_service.refresh_history_tail", return_value=(bars(), "provider", 40)) as refresh:
        missing = service.evaluate("600519", as_of=date(2026, 7, 24))
        refresh.assert_not_called()
        assert missing["reason"] == "insufficient_history"
        result = service.evaluate("600519", as_of=date(2026, 7, 24), fallback_to_network=True)
    assert result["status"] == "ok"
    assert refresh.call_args.kwargs["persist"] is True
    assert result["state"]["history_source"] == "provider"


def test_identical_network_and_cached_bars_share_one_paid_evaluation(repository):
    client = MagicMock()
    client.evaluate.side_effect = lambda state, questions: {**response(questions), "latency_ms": 1}
    loader = MagicMock(side_effect=[(None, "none"), (bars(), "db_cache")])
    service = JevEvaluationService(config=config(), repository=repository, client=client, history_loader=loader)
    with patch("src.services.jev_evaluation_service.refresh_history_tail", return_value=(bars(), "provider", 40)):
        first = service.evaluate("600519", as_of=date(2026, 7, 24), fallback_to_network=True)
        second = service.evaluate("600519", as_of=date(2026, 7, 24))
    assert first["evaluation_id"] == second["evaluation_id"]
    assert second["cached"] is True
    assert second["state"]["history_source"] == "provider"
    client.evaluate.assert_called_once()


@pytest.mark.parametrize("provider_fails", [False, True])
def test_position_scan_keeps_original_action_and_notification(provider_fails):
    from src.services.position_watch_scheduler import PositionWatchSchedulerService
    from tests.test_position_watch_scheduler import _position, _chart

    repository, market_data = MagicMock(), MagicMock()
    repository.list_position_watches.return_value = []
    market_data.load_chart.return_value = _chart(warning_level=3)
    service = PositionWatchSchedulerService(repository=repository, portfolio_service=MagicMock(),
                                            market_data=market_data, l1_runner=MagicMock())
    with patch.object(service, "_positions_for_market", return_value=[_position()]), \
            patch.object(service, "_active_watch", return_value=None), \
            patch.object(service, "_dispatch_notifications"), \
            patch("src.services.position_watch_scheduler.get_config", return_value=config()) as settings, \
            patch("src.services.jev_evaluation_service.JevEvaluationService") as jev:
        settings.return_value.jev_enabled = False
        service.scan_market("cn", as_of=date(2026, 8, 10))
        baseline = deepcopy(repository.upsert_position_watch.call_args.kwargs)
        settings.return_value.jev_enabled = True
        jev.return_value.evaluate.return_value = {"status": "ok", "answers": {"risk_5": {"choice": "normal"}}}
        if provider_fails:
            jev.return_value.evaluate.side_effect = RuntimeError("offline")
        service.scan_market("cn", as_of=date(2026, 8, 10))
        observed = repository.upsert_position_watch.call_args.kwargs
    shadow = observed["event_payload"].pop("jev_evaluation")
    assert shadow["status"] == ("error" if provider_fails else "ok")
    assert observed == baseline
    assert observed["current_action"] == "reduce"
