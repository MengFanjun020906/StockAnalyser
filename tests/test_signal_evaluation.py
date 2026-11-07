# -*- coding: utf-8 -*-
"""Deterministic shadow evaluation tests for entry-signal outcomes."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.repositories.signal_lifecycle_repo import SignalLifecycleRepository
from src.services.signal_evaluation_service import SignalEvaluationService
from src.storage import DatabaseManager, StockDaily


@pytest.fixture()
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'evaluation.db'}")
    with manager.get_session() as session:
        start = date(2026, 1, 2)
        for index in range(180):
            day = start + timedelta(days=index)
            close = 100.0 + index * 0.25
            session.add(StockDaily(
                code="600519",
                date=day,
                open=close - 0.2,
                high=close + 0.8,
                low=close - 0.8,
                close=close,
                volume=100000,
                amount=close * 100000,
                data_source="unit_test",
            ))
        session.commit()
    yield manager
    DatabaseManager.reset_instance()


def _plan(signal_id: str, *, created: date, valid_until: date):
    return {
        "signal_id": signal_id,
        "owner_id": None,
        "account_id": None,
        "stock_code": "600519",
        "market": "cn",
        "signal_type": "entry",
        "setup_type": "trend_pullback",
        "status": "active",
        "entry_low": 100.0,
        "entry_high": 101.0,
        "stop_price": 95.0,
        "trigger_rule": {"type": "price_entered_zone"},
        "invalidation_rules": ["daily_close_below_95"],
        "created_as_of": datetime.combine(created, datetime.min.time()),
        "last_checked_as_of": datetime.combine(created, datetime.min.time()),
        "valid_trading_days": 10,
        "valid_until": valid_until,
        "metadata": {"evidence_fingerprint": signal_id},
    }


def test_untriggered_expiry_is_opportunity_cost_only(db):
    repo = SignalLifecycleRepository(db)
    created = date(2026, 1, 2)
    signal = repo.upsert_entry_plan(
        _plan("untriggered", created=created, valid_until=date(2026, 1, 12)),
        dedupe_key="create:untriggered",
    )
    repo.transition_entry_signal(
        signal["signal_id"],
        to_status="expired",
        event_type="signal_expired",
        occurred_at=datetime(2026, 1, 12),
        as_of=datetime(2026, 1, 12),
        source="test",
        dedupe_key="expire:untriggered",
    )

    result = SignalEvaluationService(repository=repo).evaluate_signal(repo.get_entry_signal("untriggered"))

    assert result["triggered"] is False
    assert result["complete_horizons"] == []
    assert all(item["status"] == "not_triggered" for item in result["horizons"].values())
    assert all(item["signal_return"] is None for item in result["horizons"].values())
    assert all(item["execution_adjusted_return"] == 0.0 for item in result["horizons"].values())
    assert result["opportunity"]["status"] == "complete"
    assert result["opportunity"]["opportunity_cost"] > 0
    summary = SignalEvaluationService(repository=repo).evaluate_all()
    assert summary["horizons"]["5"]["execution_adjusted_return_pct"] == 0.0
    assert summary["horizons"]["5"]["signal_return_pct"] is None


def test_triggered_signal_has_simulated_return_and_no_fill_semantics(db):
    repo = SignalLifecycleRepository(db)
    created = date(2026, 1, 2)
    signal = repo.upsert_entry_plan(
        _plan("triggered", created=created, valid_until=date(2026, 1, 12)),
        dedupe_key="create:triggered",
    )
    repo.transition_entry_signal(
        signal["signal_id"],
        to_status="price_triggered",
        event_type="entry_price_triggered",
        occurred_at=datetime(2026, 1, 5),
        as_of=datetime(2026, 1, 5),
        source="test",
        dedupe_key="trigger:triggered",
        payload={"trigger_price": 101.0},
    )

    result = SignalEvaluationService(repository=repo).evaluate_signal(repo.get_entry_signal("triggered"))

    assert result["triggered"] is True
    assert result["simulated_entry_price"] == 101.0
    assert result["horizons"]["5"]["status"] == "complete"
    assert result["horizons"]["5"]["signal_return"] is not None
    assert result["opportunity"]["status"] == "not_applicable"


def test_observation_only_signal_is_visible_but_excluded_from_execution_metrics(db):
    repo = SignalLifecycleRepository(db)
    created = date(2026, 1, 2)
    plan = _plan("observation", created=created, valid_until=date(2026, 1, 12))
    plan["status"] = "proposed"
    plan["metadata"] = {
        "evidence_fingerprint": "observation",
        "plan_mode": "observation_only",
        "execution_eligible": False,
    }
    signal = repo.upsert_entry_plan(plan, dedupe_key="create:observation")
    repo.transition_entry_signal(
        signal["signal_id"],
        to_status="price_triggered",
        event_type="entry_price_triggered",
        occurred_at=datetime(2026, 1, 5),
        as_of=datetime(2026, 1, 5),
        source="test",
        dedupe_key="trigger:observation",
        payload={"trigger_price": 101.0},
    )

    service = SignalEvaluationService(repository=repo)
    result = service.evaluate_signal(repo.get_entry_signal("observation"))
    summary = service.evaluate_all()

    assert result["triggered"] is True
    assert result["execution_eligible"] is False
    assert result["sample_eligible"] is False
    assert summary["sample_count"] == 1
    assert summary["eligible_count"] == 0
    assert summary["horizons"]["5"]["signal_return_pct"] is None
