# -*- coding: utf-8 -*-
"""P3 research API/worker contract test with a fake LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_database_manager
from api.v1.endpoints.backtest_runs import get_backtest_data_service, get_backtest_service
from src.backtest.calendar import CnTradingCalendar
from src.backtest.data_service import BacktestDataService
from src.backtest.runner import EngineIdentity
from src.backtest.schemas import (
    CorporateActionPolicy,
    ExperimentConfig,
    Granularity,
    ResearchConfig,
    RiskProfile,
    SourcePipeline,
)
from src.backtest.service import BacktestRunService
from src.backtest.worker import BacktestWorker
from src.storage import Base, DatabaseManager, StockDaily


CAL = CnTradingCalendar()


@dataclass
class FakeResponse:
    content: str
    model: str = "fake/research-model"
    usage: dict | None = None


class FakeAdapter:
    is_available = True

    def __init__(self):
        self.calls = 0

    def call_text(self, messages, **kwargs):
        self.calls += 1
        system = messages[0]["content"]
        if "选股研究 Agent" in system:
            body = {
                "selected": [{"symbol": "600519", "rank": 1, "confidence": 0.9, "thesis": "as-of"}],
                "no_trade_reason": None,
            }
        else:
            body = {"decision": "accept", "confidence": 0.9, "reason": "ok"}
        return FakeResponse(json.dumps(body), usage={"total_tokens": 5})


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        mode="historical_agent_research",
        source_pipeline=SourcePipeline.RESEARCH,
        start_date=date(2026, 1, 5),
        end_date=date(2026, 3, 31),
        initial_cash=Decimal("1000000"),
        granularity=Granularity.DAILY,
        price_basis="daily_declared_unadjusted",
        risk_profile=RiskProfile.BALANCED,
        corporate_action_policy=CorporateActionPolicy.ASSUME_NONE,
        fee_policy_ref="cn-a-share-zero-cost.v1",
        research=ResearchConfig(
            universe=["600519"],
            selection_top_n=1,
            max_confirmations_per_decision=1,
            max_llm_calls=10,
            min_history_bars=60,
            confirmation_mode="llm",
        ),
    )


def _seed_bars(db) -> None:
    sessions = CAL.sessions(date(2025, 8, 1), date(2026, 3, 31))
    price = 10.0
    with db.get_session() as session:
        for session_date in sessions:
            price *= 1.001
            session.add(
                StockDaily(
                    code="600519",
                    date=session_date,
                    open=price * 0.999,
                    high=price * 1.02,
                    low=price * 0.98,
                    close=price,
                    volume=1_000_000,
                    amount=0,
                    data_source="test",
                )
            )
        session.commit()


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    import src.auth as auth_module

    monkeypatch.setattr(auth_module, "_auth_enabled", False)
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(db._engine)
    _seed_bars(db)
    fake = FakeAdapter()
    service = BacktestRunService(
        db_manager=db,
        runs_root_override=tmp_path / "runs",
        engine_identity=EngineIdentity("test", "abc", False),
        research_llm_adapter=fake,
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
            yield client, service, db, fake
    finally:
        DatabaseManager.reset_instance()


def test_p3_research_precheck_create_and_worker(api_env):
    client, service, db, fake = api_env
    payload = {"config": _config().model_dump(mode="json"), "allow_incomplete": False}
    response = client.post("/api/v1/backtest/runs", json=payload)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "queued"
    assert run["source_pipeline"] == "research"
    run_id = run["run_id"]

    # The P3 precheck must not use no_included_plans.
    precheck = client.post(
        "/api/v1/backtest/prechecks",
        json={"config": _config().model_dump(mode="json")},
    )
    assert precheck.status_code == 202
    job = precheck.json()
    assert "no_included_plans" not in (job.get("result") or {}).get("blocking_reasons", [])
    assert (job.get("result") or {}).get("diagnostics", {}).get("research", {}).get("universe") == ["600519"]

    BacktestWorker(
        repository=service.repo,
        service=service,
        worker_kind="research",
        research_llm_adapter=fake,
        engine_identity=EngineIdentity("test", "abc", False),
        once=True,
    ).run_once()

    detail = client.get(f"/api/v1/backtest/runs/{run_id}").json()
    assert detail["status"] == "partial"
    assert detail["research"]["labels"]
    assert detail["research"]["llm_calls"] >= 2
    assert detail["counts"]["orders"] >= 1
    assert client.get(f"/api/v1/backtest/runs/{run_id}/orders").json()["total"] >= 1
    assert client.get(f"/api/v1/backtest/runs/{run_id}/equity").json()["total"] >= 1
