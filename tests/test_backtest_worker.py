# -*- coding: utf-8 -*-
"""Worker lease, stale-run recovery, cancellation and fingerprint reuse tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

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
from src.storage import (
    BacktestCorporateActionRecord,
    BacktestRunRecord,
    MarketRuleSnapshotRecord,
)
from tests.backtest_helpers import CALENDAR, PRICES, SESSIONS, seed_db, temp_db


@pytest.fixture(autouse=True)
def _reset_database_singleton():
    yield
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 9),
        initial_cash=Decimal("100000"),
        granularity=Granularity.DAILY,
        price_basis="daily_declared_unadjusted",
        risk_profile=RiskProfile.BALANCED,
        source_pipeline=SourcePipeline.SINGLE_STOCK,
        corporate_action_policy=CorporateActionPolicy.STRICT,
        corporate_action_version="test.v1",
        fee_policy_ref="cn-a-share-zero-cost.v1",
    )


def _service(tmp_path):
    db = temp_db(tmp_path)
    seed_db(db)
    _seed_verified_rules_and_actions(db)
    service = BacktestRunService(
        db_manager=db,
        runs_root_override=tmp_path / "runs",
        engine_identity=EngineIdentity("test", "abc", False),
    )
    return db, service


def _seed_verified_rules_and_actions(db) -> None:
    from decimal import Decimal
    from src.agent.pricing.market_rules import resolve_cn_market_rules

    with db.get_session() as session:
        for session_date in SESSIONS:
            previous = CALENDAR.previous_session(session_date)
            previous_close = Decimal(str(PRICES[previous][3]))
            rules = resolve_cn_market_rules(
                "600519",
                session_date,
                previous_close=previous_close,
                st_status=False,
                st_status_verified=True,
                listing_status="listed",
                listing_status_verified=True,
                instrument_type="common_stock",
                instrument_type_verified=True,
                metadata_observed_on=date(2026, 1, 2),
            )
            session.add(
                MarketRuleSnapshotRecord(
                    snapshot_id=f"rule-{session_date.isoformat()}",
                    symbol="600519",
                    trade_date=session_date,
                    venue=rules.venue,
                    board=rules.board,
                    snapshot_json=rules.model_dump_json(),
                    created_at=datetime(2026, 1, 2, 20, 0),
                )
            )
        session.add(
            BacktestCorporateActionRecord(
                symbol="600519",
                ex_date=date(2026, 1, 6),
                action_type="cash_dividend",
                cash_per_share=0.0,
                stock_ratio=0.0,
                split_ratio=1.0,
                version="test.v1",
                source="test",
                verified=True,
                payload_json="{}",
                created_at=datetime(2026, 1, 2, 20, 0),
            )
        )
        session.commit()


def test_claim_heartbeat_and_finalize(tmp_path):
    db, service = _service(tmp_path)
    created = service.create_run(_config())
    run_id = created["run_id"]
    claimed = service.repo.claim_next(worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed["run_id"] == run_id
    assert claimed["status"] == "validating"
    assert service.repo.heartbeat(
        run_id=run_id,
        worker_id="w1",
        progress=0.5,
        phase="running",
        lease_seconds=60,
    )
    assert not service.repo.heartbeat(
        run_id=run_id,
        worker_id="other",
        progress=0.5,
        phase="running",
        lease_seconds=60,
    )
    service.repo.finalize(run_id=run_id, status="succeeded", progress=1.0)
    assert service.get_run(run_id)["status"] == "succeeded"


def test_stale_lease_is_reaped_to_interrupted(tmp_path):
    db, service = _service(tmp_path)
    run_id = service.create_run(_config())["run_id"]
    service.repo.claim_next(worker_id="w1", lease_seconds=60)
    with db.get_session() as session:
        session.execute(
            update(BacktestRunRecord)
            .where(BacktestRunRecord.run_id == run_id)
            .values(lease_expires_at=datetime.now() - timedelta(seconds=5))
        )
        session.commit()
    assert service.repo.reap_stale() == 1
    row = service.get_run(run_id)
    assert row["status"] == "interrupted"
    assert row["error_code"] == "lease_expired"


def test_cancel_queued_run_never_claimed(tmp_path):
    db, service = _service(tmp_path)
    run_id = service.create_run(_config())["run_id"]
    assert service.repo.request_cancel(run_id)
    assert service.get_run(run_id)["status"] == "cancelled"
    assert service.repo.claim_next(worker_id="w1", lease_seconds=60) is None


def test_second_identical_run_reuses_succeeded_result(tmp_path):
    db, service = _service(tmp_path)
    first = service.create_run(_config())
    worker = BacktestWorker(
        repository=service.repo,
        service=service,
        once=True,
        engine_identity=EngineIdentity("test", "abc", False),
    )
    worker.run_once()
    first_row = service.get_run(first["run_id"])
    assert first_row["status"] in {"succeeded", "partial"}
    assert first_row["experiment_fingerprint"]

    second = service.create_run(_config(), force=True)
    assert second["attempt"] == 2
    worker.run_once()
    second_row = service.get_run(second["run_id"])
    assert second_row["status"] in {"succeeded", "partial"}
    assert second_row["reused_from_run_id"] == first["run_id"]
    assert second_row["experiment_fingerprint"] == first_row["experiment_fingerprint"]
    assert second_row["result_available"] is True
    assert service.get_equity(second["run_id"])["total"] == 5
    assert service.get_run_detail(second["run_id"])["inputs"]


def test_two_workers_claim_different_runs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db, service = _service(tmp_path)
    first = service.create_run(_config())["run_id"]
    second = service.create_run(_config(), force=True)["run_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: service.repo.claim_next(worker_id=worker, lease_seconds=60), ["w1", "w2"]))
    claimed_ids = {claim["run_id"] for claim in claims if claim}
    assert claimed_ids == {first, second}
