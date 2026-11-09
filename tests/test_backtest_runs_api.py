# -*- coding: utf-8 -*-
"""API contract tests for /api/v1/backtest/runs and data jobs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_database_manager
from api.v1.endpoints.backtest_runs import get_backtest_data_service, get_backtest_service
from src.backtest.data_service import BacktestDataService
from src.backtest.runner import EngineIdentity
from src.backtest.schemas import (
    CorporateActionPolicy,
    ExperimentConfig,
    Granularity,
    RiskProfile,
    SourcePipeline,
)
from src.backtest.service import BacktestRunService
from src.backtest.worker import BacktestWorker
from tests.backtest_helpers import seed_db, temp_db


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    import src.auth as auth_module

    monkeypatch.setattr(auth_module, "_auth_enabled", False)
    db = temp_db(tmp_path)
    seed_db(db)
    service = BacktestRunService(
        db_manager=db,
        runs_root_override=tmp_path / "runs",
        engine_identity=EngineIdentity("test", "abc", False),
    )
    data_service = BacktestDataService(
        db_manager=db,
        repository=service.repo,
        run_service=service,
        inline=True,
    )
    app = create_app()
    app.dependency_overrides[get_backtest_service] = lambda: service
    app.dependency_overrides[get_backtest_data_service] = lambda: data_service
    app.dependency_overrides[get_database_manager] = lambda: db
    try:
        with TestClient(app) as client:
            yield client, service, data_service, db
    finally:
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()


def _config(**overrides) -> ExperimentConfig:
    payload = dict(
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 9),
        initial_cash=Decimal("100000"),
        granularity=Granularity.DAILY,
        price_basis="daily_declared_unadjusted",
        risk_profile=RiskProfile.BALANCED,
        source_pipeline=SourcePipeline.SINGLE_STOCK,
        corporate_action_policy=CorporateActionPolicy.ASSUME_NONE,
        fee_policy_ref="cn-a-share-zero-cost.v1",
    )
    payload.update(overrides)
    return ExperimentConfig(**payload)


def _payload(*, allow_incomplete: bool = False, **config_overrides):
    return {
        "config": _config(**config_overrides).model_dump(mode="json"),
        "allow_incomplete": allow_incomplete,
    }


def test_create_list_detail_and_precheck(api_env):
    client, service, _, _ = api_env
    response = client.post("/api/v1/backtest/runs", json=_payload())
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "queued"
    assert run["precheck_status"] == "warning"
    run_id = run["run_id"]

    listing = client.get("/api/v1/backtest/runs").json()
    assert listing["total"] == 1
    assert listing["items"][0]["run_id"] == run_id

    detail = client.get(f"/api/v1/backtest/runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["inputs"][0]["included"] is True
    assert body["result_available"] is False

    job = client.post("/api/v1/backtest/prechecks", json={"config": _config().model_dump(mode="json")})
    assert job.status_code == 202
    job_body = job.json()
    assert job_body["status"] == "succeeded"
    assert job_body["result"]["status"] == "warning"


def test_worker_completes_run_and_detail_exports(api_env):
    client, service, _, _ = api_env
    run_id = client.post("/api/v1/backtest/runs", json=_payload()).json()["run_id"]
    BacktestWorker(
        repository=service.repo,
        service=service,
        once=True,
        engine_identity=EngineIdentity("test", "abc", False),
    ).run_once()

    detail = client.get(f"/api/v1/backtest/runs/{run_id}").json()
    assert detail["status"] in {"succeeded", "partial"}
    assert detail["metrics"]["total_return_pct"] > 0
    assert detail["counts"]["fills"] == 3
    assert detail["result_available"] is True

    assert client.get(f"/api/v1/backtest/runs/{run_id}/orders").json()["total"] == 3
    assert client.get(f"/api/v1/backtest/runs/{run_id}/fills").json()["total"] == 3
    equity = client.get(f"/api/v1/backtest/runs/{run_id}/equity").json()
    assert equity["total"] == 5
    assert equity["items"][0]["equity"] > 0
    assert client.get(f"/api/v1/backtest/runs/{run_id}/trades").json()["total"] == 2
    assert client.get(f"/api/v1/backtest/runs/{run_id}/diagnostics").status_code == 200

    csv_response = client.get(f"/api/v1/backtest/runs/{run_id}/export?kind=fills")
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]
    assert "fill_id" in csv_response.text
    metrics_response = client.get(f"/api/v1/backtest/runs/{run_id}/export?kind=metrics")
    assert metrics_response.status_code == 200
    assert metrics_response.json()["total_return_pct"] > 0


def test_cancel_queued_run(api_env):
    client, _, _, _ = api_env
    run_id = client.post("/api/v1/backtest/runs", json=_payload(allow_incomplete=True)).json()["run_id"]
    response = client.post(f"/api/v1/backtest/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_no_included_plans_is_422_with_audit(api_env):
    client, _, _, _ = api_env
    config = _config(plan_ids=["plan-does-not-exist"])
    response = client.post(
        "/api/v1/backtest/runs",
        json={"config": config.model_dump(mode="json"), "allow_incomplete": False},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "no_included_plans"
    assert body["detail"]["inputs"][0]["exclusion_reason"] == "plan_not_found"


def test_strict_5m_incomplete_is_blocked_at_create(api_env):
    client, _, _, _ = api_env
    config = _config(granularity=Granularity.MINUTE_5)
    response = client.post(
        "/api/v1/backtest/runs",
        json={"config": config.model_dump(mode="json"), "allow_incomplete": False},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "precheck_blocked"
    assert "incomplete_5m_coverage" in body["detail"]["blocking_reasons"]


def test_candidates_risk_and_fee_policies(api_env):
    client, _, _, _ = api_env
    candidates = client.get("/api/v1/backtest/candidates")
    assert candidates.status_code == 200
    assert candidates.json()["total"] == 1
    assert candidates.json()["items"][0]["plan_id"] == "plan-1"

    risk = client.get("/api/v1/backtest/risk-policies")
    assert risk.status_code == 200
    profiles = {item["profile"]: item for item in risk.json()}
    assert profiles["conservative"]["max_single_position_pct"] == 10.0
    assert profiles["aggressive"]["max_single_position_pct"] == 40.0
    assert profiles["balanced"]["max_concurrent_positions"] == 5

    fees = client.get("/api/v1/backtest/fee-policies")
    assert fees.status_code == 200
    assert any(item["version"] == "cn-a-share-zero-cost.v1" for item in fees.json())


def test_invalid_date_range_is_422(api_env):
    client, _, _, _ = api_env
    config = _config().model_copy(
        update={"start_date": date(2026, 1, 9), "end_date": date(2026, 1, 5)}
    )
    response = client.post(
        "/api/v1/backtest/runs",
        json={"config": config.model_dump(mode="json"), "allow_incomplete": False},
    )
    assert response.status_code == 422


def test_benchmark_endpoint_is_display_only(api_env):
    client, service, _, _ = api_env

    def fake_benchmarks(run_id: str):
        return {
            "run_id": run_id,
            "initial_equity": "100000",
            "series": [
                {
                    "symbol": "000001.SH",
                    "name": "上证指数",
                    "status": "ok",
                    "points": [{"session": "2026-01-05", "value": 100000.0}],
                }
            ],
            "warnings": [],
        }

    service.get_benchmarks = fake_benchmarks
    response = client.get("/api/v1/backtest/runs/bt-test-20260105/benchmarks")
    assert response.status_code == 200, response.text
    assert response.json()["series"][0]["symbol"] == "000001.SH"
