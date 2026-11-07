# -*- coding: utf-8 -*-
"""Signal lifecycle persistence and chart-data regression tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.repositories.signal_lifecycle_repo import SignalLifecycleRepository
from src.services.signal_lifecycle_service import SignalLifecycleService
from src.services.signal_market_data_service import SignalMarketDataService
from src.storage import DatabaseManager, SignalLifecycleEvent, StockDaily


@pytest.fixture()
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'signals.db'}")
    yield manager
    DatabaseManager.reset_instance()


def _plan(**overrides):
    payload = {
        "signal_id": "signal-1",
        "stock_code": "600519",
        "market": "cn",
        "signal_type": "entry",
        "setup_type": "agent_entry",
        "status": "active",
        "entry_low": 98.0,
        "entry_high": 100.0,
        "stop_price": 94.0,
        "trigger_rule": {"type": "price_entered_zone", "low": 98.0, "high": 100.0},
        "invalidation_rules": ["daily_close_below_94"],
        "created_as_of": datetime(2026, 8, 10),
        "last_checked_as_of": datetime(2026, 8, 10),
        "valid_trading_days": 5,
        "valid_until": date(2026, 8, 17),
        "source_trace_id": "trace-1",
    }
    payload.update(overrides)
    return payload


def test_entry_signal_upsert_is_idempotent_and_validates_transitions(db):
    repo = SignalLifecycleRepository(db)

    created = repo.upsert_entry_plan(_plan(), dedupe_key="agent-result:trace-1:600519:entry")
    duplicate = repo.upsert_entry_plan(_plan(), dedupe_key="agent-result:trace-1:600519:entry")

    assert duplicate["signal_id"] == created["signal_id"]
    assert duplicate["invalidation_rules"] == ["daily_close_below_94"]
    with db.get_session() as session:
        assert session.query(SignalLifecycleEvent).count() == 1

    triggered = repo.transition_entry_signal(
        created["signal_id"],
        to_status="price_triggered",
        event_type="entry_price_triggered",
        occurred_at=datetime(2026, 8, 11),
        as_of=datetime(2026, 8, 11),
        source="test",
        dedupe_key="signal-1:triggered:2026-08-11",
    )
    assert triggered["status"] == "price_triggered"

    with pytest.raises(ValueError, match="invalid entry signal transition"):
        repo.transition_entry_signal(
            created["signal_id"],
            to_status="active",
            event_type="bad_transition",
            occurred_at=datetime(2026, 8, 12),
            as_of=datetime(2026, 8, 12),
            source="test",
            dedupe_key="signal-1:bad:2026-08-12",
        )


def test_capture_requires_plan_eligible_single_stock_context_and_complete_plan(db):
    service = SignalLifecycleService(
        repository=SignalLifecycleRepository(db),
        market_data=SignalMarketDataService(db),
    )
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={
            "stock_name": "贵州茅台",
            "decision_type": "buy",
            "dashboard": {
                "battle_plan": {
                    "sniper_points": {
                        "ideal_buy": 1500,
                        "secondary_buy": 1470,
                        "stop_loss": 1420,
                    },
                    "invalidation_conditions": ["日线收盘跌破 1420"],
                }
            },
        },
    )

    generic_analysis_without_report_context = service.capture_agent_result(
        message="分析 600519 的基本面",
        session_id="trace-info",
        result=result,
        context={"stock_code": "600519"},
    )
    assert generic_analysis_without_report_context is not None

    incomplete_result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={"stock_name": "贵州茅台", "decision_type": "hold"},
    )
    assert service.capture_agent_result(
        message="分析 600519 的基本面",
        session_id="trace-info-no-zone",
        result=incomplete_result,
        context={"stock_code": "600519"},
    ) is None

    generic_analysis = service.capture_agent_result(
        message="分析 600519 的基本面",
        session_id="trace-default-entry-intent",
        result=result,
        context={
            "stock_code": "600519",
            "agent_user_context": {"report": {"intent": "entry_analysis"}},
        },
    )
    assert generic_analysis is not None

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="trace-entry",
        result=result,
        context={"stock_code": "600519", "stock_name": "贵州茅台"},
    )
    assert captured is not None
    assert captured["entry_low"] == 1470
    assert captured["entry_high"] == 1500
    assert captured["stop_price"] == 1420
    assert captured["setup_type"] == "agent_entry"
    assert captured["account_id"] is None


def test_dashboard_add_action_takes_priority_over_generic_buy_decision():
    assert SignalLifecycleService._dashboard_action({
        "decision_type": "buy",
        "operation_advice": "加仓",
    }) == "add"


def test_hard_rejection_dashboard_does_not_create_waiting_entry_plan(db):
    service = SignalLifecycleService(
        repository=SignalLifecycleRepository(db),
        market_data=SignalMarketDataService(db),
    )
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={
            "decision_type": "sell",
            "operation_advice": "卖出或回避",
            "sniper_points": {"ideal_buy": 100, "secondary_buy": 98, "stop_loss": 94},
        },
    )

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="trace-hard-reject",
        result=result,
        context={"stock_code": "600519"},
    )

    assert captured is None


def test_waiting_dashboard_with_complete_entry_zone_creates_watch_plan(db):
    service = SignalLifecycleService(
        repository=SignalLifecycleRepository(db),
        market_data=SignalMarketDataService(db),
    )
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={
            "stock_name": "贵州茅台",
            "decision_type": "hold",
            "operation_advice": "等待回调，不追价",
            "dashboard": {
                "battle_plan": {
                    "sniper_points": {
                        "ideal_buy": 1500,
                        "secondary_buy": 1470,
                        "stop_loss": 1420,
                    },
                    "invalidation_conditions": ["日线收盘跌破 1420"],
                }
            },
        },
    )

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="trace-wait-entry-zone",
        result=result,
        context={"stock_code": "600519", "stock_name": "贵州茅台"},
    )

    assert captured is not None
    assert captured["entry_low"] == 1470
    assert captured["entry_high"] == 1500
    assert captured["metadata"]["current_action"] == "wait"
    assert captured["metadata"]["trigger_action"] == "open"


def test_entry_revision_freezes_validity_and_rejects_target_drift(db):
    repo = SignalLifecycleRepository(db)
    created = repo.upsert_entry_plan(
        _plan(metadata={"evidence_fingerprint": "same"}),
        dedupe_key="create:frozen",
    )

    drifted = repo.upsert_entry_plan(
        _plan(
            signal_id="ignored-new-id",
            entry_high=101.0,
            valid_until=date(2026, 8, 24),
            metadata={"evidence_fingerprint": "same"},
        ),
        dedupe_key="revise:frozen:drift",
    )
    assert drifted["signal_id"] == created["signal_id"]
    assert drifted["entry_high"] == 100.0
    assert drifted["valid_until"] == date(2026, 8, 17)
    assert repo.list_signal_events(created["signal_id"])[-1]["event_type"] == "target_drift_rejected"

    checked = repo.upsert_entry_plan(
        _plan(
            signal_id="ignored-newer-id",
            valid_until=date(2026, 8, 24),
            metadata={"evidence_fingerprint": "new"},
        ),
        dedupe_key="revise:frozen:validity",
    )
    assert checked["valid_until"] == date(2026, 8, 17)
    assert checked["metadata"]["validity_change_rejected"]["frozen"] == "2026-08-17"


def test_blocked_new_analysis_invalidates_existing_signal(db):
    repo = SignalLifecycleRepository(db)
    repo.upsert_entry_plan(_plan(), dedupe_key="create:block-test")
    service = SignalLifecycleService(repository=repo, market_data=SignalMarketDataService(db))
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={
            "decision_type": "buy",
            "sniper_points": {"ideal_buy": 99, "secondary_buy": 98, "stop_loss": 94},
        },
    )

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="trace-blocked",
        result=result,
        context={"stock_code": "600519", "as_of": "2026-08-11"},
        risk_gate_payload={"risk_gate": {"status": "blocked", "allowed_action": "manual_review"}},
    )

    assert captured is not None
    assert captured["signal_id"] != "signal-1"
    assert captured["metadata"]["plan_mode"] == "risk_blocked"
    assert captured["metadata"]["execution_eligible"] is False
    assert repo.get_entry_signal("signal-1")["status"] == "invalidated"
    assert repo.list_signal_events("signal-1")[-1]["payload"]["reason"] == "risk_gate_blocked"


def test_evidence_quality_moves_signal_through_needs_review(db):
    repo = SignalLifecycleRepository(db)
    created = repo.upsert_entry_plan(
        _plan(status="needs_review", evidence_quality="partial", metadata={"evidence_fingerprint": "partial"}),
        dedupe_key="create:partial",
    )
    assert created["status"] == "needs_review"

    recovered = repo.upsert_entry_plan(
        _plan(signal_id="new-id", status="active", evidence_quality="ok", metadata={"evidence_fingerprint": "fresh"}),
        dedupe_key="check:fresh",
    )
    assert recovered["status"] == "active"
    assert repo.list_signal_events(created["signal_id"])[-1]["event_type"] == "signal_reactivated"


def test_stale_composite_snapshot_cannot_create_active_signal(db):
    service = SignalLifecycleService(
        repository=SignalLifecycleRepository(db),
        market_data=SignalMarketDataService(db),
    )
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        tool_calls_log=[],
        dashboard={
            "decision_type": "buy",
            "sniper_points": {"ideal_buy": 100, "secondary_buy": 98, "stop_loss": 94},
        },
    )

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="trace-stale",
        result=result,
        context={"stock_code": "600519", "as_of": "2026-08-11T18:00:00"},
        composite_payload={"snapshot": {"as_of": "2026-08-01", "data_quality": "ok"}},
    )

    assert captured["status"] == "needs_review"
    assert captured["evidence_quality"] == "partial"
    assert captured["metadata"]["evidence_snapshot"]["stale_snapshot"] is True


def test_chat_trade_plan_uses_local_composite_snapshot_at_capture_time(db):
    market_data = MagicMock()
    market_data.load_chart.return_value = {
        "status": "ok",
        "composite": {
            "status": "ok",
            "snapshot": {
                "symbol": "600519",
                "as_of": "2026-08-11",
                "data_quality": "sufficient",
                "chan": {
                    "signals": [{"name": "first_buy", "triggered": True}],
                },
            },
            "composite_verdict": {
                "score": 18,
                "scale_max": 20,
                "exit_warning": {"level": 0, "items": []},
            },
        },
    }
    service = SignalLifecycleService(
        repository=SignalLifecycleRepository(db),
        market_data=market_data,
    )
    result = SimpleNamespace(
        success=True,
        stock_selection=None,
        dashboard=None,
        tool_calls_log=[],
        trade_plan={
            "symbol": "600519",
            "action": "open",
            "entry_zone_low": 98.0,
            "entry_zone_high": 100.0,
            "stop_loss_price": 94.0,
            "invalidation_conditions": ["日线收盘跌破 94"],
        },
    )

    captured = service.capture_agent_result(
        message="分析 600519 是否适合买入",
        session_id="chat-local-composite",
        result=result,
        context={"stock_code": "600519", "market": "cn", "as_of": "2026-08-11T15:00:00"},
    )

    assert captured["status"] == "active"
    assert captured["setup_type"] == "chan_first_buy"
    assert captured["metadata"]["composite_verdict"]["score"] == 18
    market_data.load_chart.assert_called_once_with(
        "600519",
        market="cn",
        refresh_tail=False,
        as_of=date(2026, 8, 11),
        holding=False,
    )


def test_chart_loads_from_earliest_local_bar_and_builds_indicators(db):
    start = date(2025, 1, 2)
    with db.get_session() as session:
        for index in range(230):
            trade_date = start + timedelta(days=index)
            price = 100 + index * 0.1
            session.add(StockDaily(
                code="600519",
                date=trade_date,
                open=price - 0.2,
                high=price + 0.8,
                low=price - 0.8,
                close=price,
                volume=100000 + index * 100,
                amount=price * (100000 + index * 100),
                data_source="unit_test",
            ))
        session.commit()

    chart = SignalMarketDataService(db).load_chart(
        "600519",
        market="cn",
        refresh_tail=False,
        as_of=start + timedelta(days=229),
    )

    assert chart["status"] == "ok"
    assert chart["data_window"]["start"] == start.isoformat()
    assert chart["data_window"]["bar_count"] == 230
    assert chart["bars"][0]["guide_line"] is None
    assert chart["bars"][199]["guide_line"] is not None
    assert chart["bars"][-1]["ma57"] is not None
    assert chart["bars"][-1]["macd_histogram"] is not None
    assert chart["data_window"]["price_basis"] == "local_cash_price_unadjusted"


def test_entry_list_and_detail_share_read_only_effective_expiry_status(db):
    repo = SignalLifecycleRepository(db)
    expired_on = date.today() - timedelta(days=1)
    repo.upsert_entry_plan(
        _plan(
            created_as_of=datetime.combine(expired_on - timedelta(days=5), datetime.min.time()),
            last_checked_as_of=datetime.combine(expired_on - timedelta(days=5), datetime.min.time()),
            valid_until=expired_on,
        ),
        dedupe_key="create:read-only-expired",
    )
    market_data = MagicMock()
    market_data.load_chart.return_value = {"status": "ok", "bars": [], "data_window": {"end": expired_on.isoformat()}}
    service = SignalLifecycleService(repository=repo, market_data=market_data)

    listed = service.list_preview()["entry_signals"][0]
    detail = service.entry_chart("signal-1", refresh_tail=False)

    assert listed["status"] == "active"
    assert listed["effective_status"] == "expired"
    assert detail["subject"]["status"] == "active"
    assert detail["subject"]["effective_status"] == "expired"
    assert repo.get_entry_signal("signal-1")["status"] == "active"


def test_preview_list_fills_names_and_keeps_latest_row_per_stock():
    repo = MagicMock()
    repo.list_entry_signals.return_value = [
        {
            "signal_id": "entry-new",
            "stock_code": "600519",
            "stock_name": None,
            "market": "cn",
            "status": "active",
            "valid_until": date(2026, 8, 20),
        },
        {
            "signal_id": "entry-old",
            "stock_code": "600519",
            "stock_name": None,
            "market": "cn",
            "status": "active",
            "valid_until": date(2026, 8, 19),
        },
    ]
    repo.list_position_watches.return_value = [
        {
            "watch_id": "watch-new",
            "stock_code": "600330",
            "stock_name": None,
            "market": "cn",
            "status": "active",
            "risk_level": "low",
            "version": 2,
        },
        {
            "watch_id": "watch-old",
            "stock_code": "600330",
            "stock_name": None,
            "market": "cn",
            "status": "closed",
            "risk_level": "low",
            "version": 1,
        },
    ]
    repo.list_scheduler_states.return_value = []
    price_plans = MagicMock()
    price_plans.list_artifacts.return_value = [
        SimpleNamespace(
            plan_id="plan-new",
            symbol="600330",
            market="cn",
            generation_status=SimpleNamespace(value="observation"),
            revision=2,
            actual_as_of_session=date(2026, 8, 15),
            reason_codes=[],
            scenarios=[],
            trace_ref="trace-new",
        ),
        SimpleNamespace(
            plan_id="plan-old",
            symbol="600330",
            market="cn",
            generation_status=SimpleNamespace(value="observation"),
            revision=1,
            actual_as_of_session=date(2026, 8, 14),
            reason_codes=[],
            scenarios=[],
            trace_ref="trace-old",
        ),
    ]
    service = SignalLifecycleService(
        repository=repo,
        market_data=MagicMock(),
        price_plan_repository=price_plans,
    )

    payload = service.list_preview()

    assert [item["signal_id"] for item in payload["entry_signals"]] == ["entry-new"]
    assert payload["entry_signals"][0]["stock_name"] == "贵州茅台"
    assert [item["plan_id"] for item in payload["price_plans"]] == ["plan-new"]
    assert payload["price_plans"][0]["stock_name"] == "天通股份"
    assert [item["watch_id"] for item in payload["position_watches"]] == ["watch-new"]
    assert payload["position_watches"][0]["stock_name"] == "天通股份"
    assert payload["counts"]["entry"] == 1
    assert payload["counts"]["plans"] == 1
    assert payload["counts"]["positions"] == 1


def test_next_single_stock_request_replays_bars_and_marks_price_triggered(db):
    repo = SignalLifecycleRepository(db)
    repo.upsert_entry_plan(_plan(), dedupe_key="create:lazy-reconcile")
    market_data = MagicMock()
    market_data.load_chart.return_value = {
        "status": "ok",
        "bars": [
            {
                "date": "2026-08-11",
                "open": 101.0,
                "high": 101.5,
                "low": 99.0,
                "close": 100.0,
            }
        ],
        "data_window": {"end": "2026-08-11", "target_date": "2026-08-11"},
    }
    service = SignalLifecycleService(repository=repo, market_data=market_data)

    reconciled = service.reconcile_for_request(
        message="继续分析 600519 是否适合买入",
        context={"stock_code": "600519", "market": "cn"},
    )

    assert reconciled[0]["status"] == "price_triggered"
    assert repo.get_entry_signal("signal-1")["status"] == "price_triggered"
    event = repo.list_signal_events("signal-1")[-1]
    assert event["event_type"] == "entry_price_triggered"
    assert event["occurred_at"].date() == date(2026, 8, 11)


def test_reconcile_matches_each_disjoint_entry_zone_without_filling_the_gap(db):
    repo = SignalLifecycleRepository(db)
    repo.upsert_entry_plan(
        _plan(
            trigger_rule={
                "type": "price_entered_zone",
                "low": 98.0,
                "high": 100.0,
                "zones": [
                    {"label": "ideal", "low": 98.0, "high": 100.0},
                    {"label": "secondary", "low": 104.0, "high": 105.0},
                ],
            },
        ),
        dedupe_key="create:multi-zone-reconcile",
    )
    market_data = MagicMock()
    market_data.load_chart.return_value = {
        "status": "ok",
        "bars": [{
            "date": "2026-08-11",
            "open": 103.8,
            "high": 104.8,
            "low": 104.2,
            "close": 104.5,
        }],
        "data_window": {"end": "2026-08-11", "target_date": "2026-08-11"},
    }
    service = SignalLifecycleService(repository=repo, market_data=market_data)

    reconciled = service.reconcile_for_request(
        message="继续分析 600519",
        context={"stock_code": "600519", "market": "cn"},
    )

    assert reconciled[0]["status"] == "price_triggered"
    event = repo.list_signal_events("signal-1")[-1]
    assert event["payload"]["matched_zone"]["label"] == "secondary"


def test_position_notification_outbox_is_transactional_and_idempotent(db):
    repo = SignalLifecycleRepository(db)
    position = {
        "account_id": 1,
        "owner_id": None,
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "market": "cn",
        "quantity": 100,
        "average_cost": 1500,
    }
    notification = {
        "dedupe_key": "1:600519:1:reduce",
        "action": "reduce",
        "payload": {"stock_code": "600519", "action": "reduce"},
        "next_attempt_at": datetime(2026, 8, 10, 18),
    }
    repo.upsert_position_watch(
        position,
        as_of=datetime(2026, 8, 10, 18),
        risk_level="high",
        current_action="reduce",
        score=8,
        exit_warning_level=3,
        event_type="position_opened",
        event_payload={},
        dedupe_key="watch:1:2026-08-10",
        notification=notification,
    )

    claimed = repo.claim_notification_batch(now=datetime(2026, 8, 10, 18))
    assert len(claimed) == 1
    assert claimed[0]["status"] == "sending"
    assert repo.claim_notification_batch(now=datetime(2026, 8, 10, 18)) == []
    reclaimed = repo.claim_notification_batch(now=datetime(2026, 8, 10, 18, 11))
    assert len(reclaimed) == 1
    assert reclaimed[0]["attempts"] == 2
    failed = repo.complete_notification(reclaimed[0]["notification_id"], sent=False, error="test")
    assert failed["status"] == "failed"
    assert failed["attempts"] == 2

    # Retrying the same scheduler event cannot create another outbox row.
    repo.upsert_position_watch(
        position,
        as_of=datetime(2026, 8, 10, 18),
        risk_level="high",
        current_action="reduce",
        score=8,
        exit_warning_level=3,
        event_type="position_checked",
        event_payload={},
        dedupe_key="watch:1:2026-08-10",
        notification=notification,
    )
    assert repo.claim_notification_batch(now=datetime(2026, 8, 10, 18)) == []
