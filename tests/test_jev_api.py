"""Jev API contracts and shadow integration boundaries."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints import jev


def client():
    app = FastAPI()
    app.include_router(jev.router, prefix="/api/v1/jev")
    return TestClient(app)


def test_api_never_accepts_client_supplied_prices_or_execution_permissions():
    with patch.object(jev, "JevEvaluationService") as service:
        response = client().post("/api/v1/jev/evaluate", json={
            "stock_code": "AAPL", "price_plan": {"entry": 10}, "execution_eligible": True,
        })
    assert response.status_code == 422
    service.assert_not_called()


def test_api_uses_server_plan_and_explicit_network_opt_in():
    service = MagicMock()
    service.evaluate.return_value = {"status": "ok", "shadow_mode": True}
    with patch.object(jev, "JevEvaluationService", return_value=service), patch.object(jev, "PricePlanRepository") as plans:
        plans.return_value.get_latest_artifact.return_value = None
        result = client().post("/api/v1/jev/evaluate", json={"stock_code": "hk00700"})
    assert result.status_code == 200
    assert service.evaluate.call_args.kwargs["fallback_to_network"] is False
    assert service.evaluate.call_args.kwargs["price_plan"] is None


def test_history_omits_large_input_but_detail_exposes_audit_and_outcomes():
    service = MagicMock()
    record = {"evaluation_id": "jev-unit", "status": "ok", "state": {"metrics": {}}, "questions": {}}
    service.list_recent.return_value = [record]
    service.repo.get.return_value = record
    service.outcomes.return_value = [{"horizon": 1, "status": "pending"}]
    with patch.object(jev, "JevEvaluationService", return_value=service):
        history = client().get("/api/v1/jev/evaluations").json()
        detail = client().get("/api/v1/jev/evaluations/jev-unit").json()
    assert "state" not in history["items"][0]
    assert detail["state"] == record["state"]
    assert detail["outcomes"][0]["status"] == "pending"


def test_single_stock_capture_keeps_signal_result_when_jev_fails():
    from api.v1.endpoints.agent import _capture_signal_lifecycle

    result = SimpleNamespace(success=True, deterministic_risk_gate={"allowed_action": "wait"})
    with patch("src.services.signal_lifecycle_service.SignalLifecycleService") as signals, \
            patch("src.services.jev_evaluation_service.JevEvaluationService") as evaluations:
        evaluations.return_value.capture_agent_result.side_effect = RuntimeError("unavailable")
        _capture_signal_lifecycle(message="AAPL", session_id="jev-test", result=result,
                                  context={"stock_code": "AAPL"})
    signals.return_value.capture_agent_result.assert_called_once()
    evaluations.return_value.capture_agent_result.assert_called_once()
    assert result.deterministic_risk_gate == {"allowed_action": "wait"}


def test_invalid_date_is_rejected_before_portfolio_snapshot_or_plan_lookup():
    with patch.object(jev, "PricePlanRepository") as plans, \
            patch("src.services.portfolio_service.PortfolioService") as portfolio:
        response = client().post("/api/v1/jev/evaluate", json={
            "stock_code": "AAPL", "as_of": "2099-01-01", "account_id": 1,
        })
    assert response.status_code == 422
    plans.assert_not_called()
    portfolio.assert_not_called()
