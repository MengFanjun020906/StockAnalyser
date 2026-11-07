# -*- coding: utf-8 -*-
"""Signal preview API contract tests."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.app import create_app


def test_signal_preview_list_endpoint(tmp_path):
    service = MagicMock()
    service.list_preview.return_value = {
        "entry_signals": [{"signal_id": "signal-1", "stock_code": "600519"}],
        "position_watches": [],
        "scheduler": [],
        "counts": {"entry": 1, "active_entry": 1, "positions": 0, "high_risk": 0},
    }
    client = TestClient(create_app(static_dir=tmp_path / "empty-static"))

    with patch("api.v1.endpoints.signal_preview.SignalLifecycleService", return_value=service):
        response = client.get("/api/v1/signal-preview")

    assert response.status_code == 200
    assert response.json()["entry_signals"][0]["signal_id"] == "signal-1"


def test_signal_preview_detail_returns_404(tmp_path):
    service = MagicMock()
    service.entry_chart.return_value = None
    client = TestClient(create_app(static_dir=tmp_path / "empty-static"))

    with patch("api.v1.endpoints.signal_preview.SignalLifecycleService", return_value=service):
        response = client.get("/api/v1/signal-preview/entries/missing")

    assert response.status_code == 404
    assert "not_found" in str(response.json())


def test_signal_preview_price_plan_detail_endpoint(tmp_path):
    service = MagicMock()
    service.price_plan_chart.return_value = {
        "kind": "price_plan",
        "subject": {"plan_id": "plan-1", "stock_code": "600519"},
        "price_plan": {"plan_id": "plan-1", "scenarios": []},
        "lifecycle_states": [],
        "events": [],
        "chart": {"bars": []},
    }
    client = TestClient(create_app(static_dir=tmp_path / "empty-static"))

    with patch("api.v1.endpoints.signal_preview.SignalLifecycleService", return_value=service):
        response = client.get("/api/v1/signal-preview/plans/plan-1?refresh_tail=false")

    assert response.status_code == 200
    assert response.json()["price_plan"]["plan_id"] == "plan-1"
    service.price_plan_chart.assert_called_once_with("plan-1", refresh_tail=False)


def test_signal_preview_evaluation_endpoint_is_read_only_summary(tmp_path):
    service = MagicMock()
    service.evaluate_all.return_value = {
        "sample_count": 1,
        "eligible_count": 1,
        "horizons": {},
        "by_setup_type": [],
        "review_policy": {"mode": "shadow_only"},
        "rows": [],
    }
    client = TestClient(create_app(static_dir=tmp_path / "empty-static"))

    with patch("api.v1.endpoints.signal_preview.SignalEvaluationService", return_value=service):
        response = client.get("/api/v1/signal-preview/evaluation?limit=100")

    assert response.status_code == 200
    assert response.json()["review_policy"]["mode"] == "shadow_only"
    service.evaluate_all.assert_called_once_with(limit=100, setup_type=None)
