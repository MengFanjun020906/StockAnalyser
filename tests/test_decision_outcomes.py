# -*- coding: utf-8 -*-
"""MEM-01a: decision outcome pending records and T+5 settlement."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent import stock_selection
from src.config import Config
from src.services.decision_outcome_service import (
    HORIZON_DAYS,
    STATUS_DELISTED_EXIT,
    STATUS_EXPIRED_MISSING_EXIT,
    STATUS_PENDING,
    STATUS_SETTLED,
    STATUS_SKIPPED_MISSING_ENTRY,
    STATUS_SKIPPED_UNFILLABLE,
    build_decision_outcome_background_tasks,
    record_pending_records,
    settle_due_records,
)
from src.storage import DatabaseManager, DecisionOutcomeRecord, StockDaily

SESSION_START = date(2024, 1, 1)
HORIZON = HORIZON_DAYS


def _day(offset: int) -> date:
    return SESSION_START + timedelta(days=offset)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(Config, "_instance", None, raising=False)
    DatabaseManager.reset_instance()
    manager = DatabaseManager.get_instance()
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()
        Config._instance = None


def _add_bar(session, code, day, *, open_=10.0, close=10.0, high=None, low=None, volume=1000.0, pct_chg=0.0):
    session.add(
        StockDaily(
            code=code,
            date=day,
            open=open_,
            high=open_ if high is None else high,
            low=open_ if low is None else low,
            close=close,
            volume=volume,
            amount=volume * close if close is not None else None,
            pct_chg=pct_chg,
            data_source="test",
        )
    )


def _seed_calendar(session, last_offset: int, code: str = "600999"):
    """Insert filler bars so ``stock_daily`` defines a trading calendar."""
    for offset in range(last_offset + 1):
        _add_bar(session, code, _day(offset))
    session.commit()


def _record(db, run_id, code, mode="immediate_open", **extra):
    item = {"code": code, "execution_mode": mode, "action": extra.pop("action", "buy")}
    item.update(extra)
    return record_pending_records(
        run_id=run_id,
        decision_as_of=_day(0),
        items=[item],
        enabled=True,
        db=db,
    )


def _row(db, run_id, code):
    with db.get_session() as session:
        return (
            session.query(DecisionOutcomeRecord)
            .filter(DecisionOutcomeRecord.run_id == run_id, DecisionOutcomeRecord.code == code)
            .one()
        )


def test_record_is_idempotent_and_counts_skipped_actions(db):
    items = [
        {"code": "600001", "execution_mode": "immediate_open", "action": "buy"},
        {"code": "600002", "execution_mode": "conditional_open", "action": "wait"},
        {"code": "600003", "execution_mode": "strong_watch", "action": "watch"},
        {"code": "600004", "execution_mode": "plain_wait", "action": "avoid"},
    ]
    first = record_pending_records(run_id="run-1", decision_as_of=_day(0), items=items, db=db)
    assert first == {"enabled": True, "inserted": 2, "updated": 0, "skipped": 0, "skipped_actions": 2}

    second = record_pending_records(run_id="run-1", decision_as_of=_day(0), items=items, db=db)
    assert second == {"enabled": True, "inserted": 0, "updated": 2, "skipped": 0, "skipped_actions": 2}

    with db.get_session() as session:
        rows = session.query(DecisionOutcomeRecord).order_by(DecisionOutcomeRecord.code).all()
    assert [row.code for row in rows] == ["600001", "600002"]
    assert all(row.status == STATUS_PENDING for row in rows)
    assert all(row.horizon_days == HORIZON for row in rows)


def test_settles_at_t_plus_5_close_with_20bp_cost(db):
    with db.get_session() as session:
        _seed_calendar(session, 6)
        _add_bar(session, "600001", _day(1), open_=10.0, close=10.5, high=10.8, low=9.9)
        _add_bar(session, "600001", _day(5), open_=11.9, close=12.0, high=12.1, low=11.7)
        session.commit()

    _record(db, "run-settle", "600001")
    summary = settle_due_records(as_of=_day(6), db=db)

    assert summary["checked"] == 1
    assert summary["settled"] == 1
    row = _row(db, "run-settle", "600001")
    assert row.status == STATUS_SETTLED
    assert row.reason_code is None
    assert row.entry_session == _day(1)
    assert row.exit_session == _day(5)
    assert row.entry_price == pytest.approx(10.0)
    assert row.exit_price == pytest.approx(12.0)
    assert row.trade_return == pytest.approx(0.198, abs=1e-6)


def test_settled_rows_are_never_overwritten(db):
    with db.get_session() as session:
        _seed_calendar(session, 6)
        _add_bar(session, "600001", _day(1), open_=10.0, close=10.5, high=10.8, low=9.9)
        _add_bar(session, "600001", _day(5), open_=11.9, close=12.0, high=12.1, low=11.7)
        session.commit()

    _record(db, "run-frozen", "600001", ideal_entry_zone="原始区间")
    settle_due_records(as_of=_day(6), db=db)

    result = record_pending_records(
        run_id="run-frozen",
        decision_as_of=_day(0),
        items=[{"code": "600001", "execution_mode": "immediate_open", "action": "buy", "ideal_entry_zone": "改写区间"}],
        db=db,
    )
    assert result["skipped"] == 1
    assert result["updated"] == 0
    assert result["inserted"] == 0

    row = _row(db, "run-frozen", "600001")
    assert row.status == STATUS_SETTLED
    assert row.entry_zone_text == "原始区间"
    assert row.trade_return == pytest.approx(0.198, abs=1e-6)


def test_pending_row_refreshes_reference_fields_on_rerun(db):
    _record(db, "run-refresh", "600001", ideal_entry_zone="旧区间")
    _record(db, "run-refresh", "600001", plan_mode="pricing_agent", entry_condition="新条件")

    row = _row(db, "run-refresh", "600001")
    assert row.status == STATUS_PENDING
    assert row.entry_zone_text == "新条件"
    assert row.plan_mode == "pricing_agent"


def test_skipped_missing_entry_when_no_bar_within_grace(db):
    with db.get_session() as session:
        _seed_calendar(session, 8)
        session.commit()

    _record(db, "run-missing", "600001")
    summary = settle_due_records(as_of=_day(8), db=db)

    row = _row(db, "run-missing", "600001")
    assert row.status == STATUS_SKIPPED_MISSING_ENTRY
    assert row.reason_code == "entry_bar_missing"
    assert row.trade_return is None
    assert summary["skipped"] == 1


def test_skipped_unfillable_on_one_word_limit_up(db):
    with db.get_session() as session:
        _seed_calendar(session, 6)
        _add_bar(session, "600001", _day(1), open_=11.0, close=11.0, high=11.0, low=11.0, pct_chg=10.0)
        session.commit()

    _record(db, "run-limit", "600001")
    settle_due_records(as_of=_day(6), db=db)

    row = _row(db, "run-limit", "600001")
    assert row.status == STATUS_SKIPPED_UNFILLABLE
    assert row.reason_code == "entry_limit_locked"
    assert row.entry_reference == pytest.approx(11.0)
    assert row.trade_return is None


def test_delisted_exit_uses_last_tradable_close(db):
    with db.get_session() as session:
        _seed_calendar(session, 16)
        _add_bar(session, "600001", _day(1), open_=10.0, close=10.2, high=10.5, low=9.8)
        session.commit()

    _record(db, "run-delist", "600001")
    settle_due_records(as_of=_day(16), db=db)

    row = _row(db, "run-delist", "600001")
    assert row.status == STATUS_DELISTED_EXIT
    assert row.reason_code == "delisted_before_exit"
    assert row.exit_price == pytest.approx(10.2)
    assert row.trade_return == pytest.approx(0.02 - 0.002, abs=1e-6)


def test_expired_missing_exit_when_exit_bar_has_no_close(db):
    with db.get_session() as session:
        _seed_calendar(session, 16)
        _add_bar(session, "600001", _day(1), open_=10.0, close=10.2, high=10.5, low=9.8)
        _add_bar(session, "600001", _day(5), open_=11.0, close=None, high=11.5, low=10.8)
        session.commit()

    _record(db, "run-expire", "600001")
    settle_due_records(as_of=_day(16), db=db)

    row = _row(db, "run-expire", "600001")
    assert row.status == STATUS_EXPIRED_MISSING_EXIT
    assert row.reason_code == "no_bar_at_exit"
    assert row.exit_price is None
    assert row.trade_return is None


def test_disabled_switch_writes_nothing(db):
    item = [{"code": "600001", "execution_mode": "immediate_open", "action": "buy"}]
    result = record_pending_records(run_id="run-off", decision_as_of=_day(0), items=item, enabled=False, db=db)
    assert result["enabled"] is False
    assert result["inserted"] == 0
    with db.get_session() as session:
        assert session.query(DecisionOutcomeRecord).count() == 0

    assert build_decision_outcome_background_tasks(
        SimpleNamespace(agent_decision_outcome_enabled=False)
    ) == []
    tasks = build_decision_outcome_background_tasks(SimpleNamespace(agent_decision_outcome_enabled=True))
    assert len(tasks) == 1 and tasks[0]["interval_seconds"] == 3600

    # 关闭时 hook 只读配置即返回，不触及 ctx，也不写表。
    with patch.object(stock_selection.Config, "get_instance", return_value=SimpleNamespace(agent_decision_outcome_enabled=False)):
        stock_selection._record_decision_outcomes(None, {})
    with db.get_session() as session:
        assert session.query(DecisionOutcomeRecord).count() == 0

    # 关闭时最终报告与「完全不调用 hook」逐字段一致。
    ctx = stock_selection.SelectionRunContext(run_id="run-off-report", user_message="test")
    with patch.object(stock_selection.Config, "get_instance", return_value=SimpleNamespace(agent_decision_outcome_enabled=False)):
        report_disabled = stock_selection._build_final_report_json(ctx)
    with patch.object(stock_selection, "_record_decision_outcomes", lambda *_args, **_kwargs: None):
        report_baseline = stock_selection._build_final_report_json(ctx)
    assert report_disabled == report_baseline


def test_write_failure_does_not_break_report_build(db):
    ctx = stock_selection.SelectionRunContext(run_id="run-boom", user_message="test")
    enabled_cfg = SimpleNamespace(agent_decision_outcome_enabled=True)
    with patch.object(stock_selection.Config, "get_instance", return_value=enabled_cfg):
        with patch(
            "src.services.decision_outcome_service.record_pending_records",
            side_effect=RuntimeError("db down"),
        ):
            report = stock_selection._build_final_report_json(ctx)

    assert isinstance(report, dict)
    assert report["selection_context"]["run_id"] == "run-boom"
