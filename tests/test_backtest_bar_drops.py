# -*- coding: utf-8 -*-
"""Bar-drop ledger tests: reason codes, aggregation and hook coverage."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.backtest.bar_drops import (
    ALERT_REASONS,
    REASON_CATEGORY,
    REASON_DESCRIPTION,
    REASON_PRIORITY,
    BarDropCategory,
    BarDropLedger,
    BarDropReason,
)
from src.backtest.entities import Bar, DailyBar, Order, OrderSide, OrderStatus, OrderType
from src.backtest.execution import ExecutionEngine
from src.backtest.rules import derive_rule
from src.backtest.schemas import Granularity
from src.backtest.snapshot import SnapshotBuilder
from tests.backtest_helpers import (
    CALENDAR,
    END,
    START,
    config,
    daily_bars,
    data_view,
    plan,
    rules_for,
    zero_fee_policy,
)


def _engine(data=None) -> ExecutionEngine:
    return ExecutionEngine(
        run_id="ledger-test",
        config=config(),
        plans=[],
        data=data or data_view(),
        calendar=CALENDAR,
        fee_policy=zero_fee_policy(),
    )


def _order(**overrides) -> Order:
    values = dict(
        order_id="order-1",
        run_id="ledger-test",
        plan_key="plan-1:s1",
        symbol="600519",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        limit_price=Decimal("10.9"),
        entry_floor=Decimal("10.8"),
        active_from=datetime(2026, 1, 5, 9, 30),
        expires_on_session=END,
        status=OrderStatus.PENDING,
    )
    values.update(overrides)
    return Order(**values)


def _bar(session, *, price="10.2", volume="1000000") -> Bar:
    value = Decimal(price)
    return Bar(
        symbol="600519",
        end_at=CALENDAR.session_close(session),
        session=session,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal(volume),
        amount=Decimal("0"),
        frequency="1d",
        source="test",
    )


def test_reason_catalog_is_complete_and_exclusive():
    reasons = set(BarDropReason)
    assert reasons == set(REASON_CATEGORY) == set(REASON_DESCRIPTION) == set(REASON_PRIORITY)
    assert len(set(REASON_PRIORITY.values())) == len(REASON_PRIORITY)
    assert BarDropCategory.DATA_ISSUE and BarDropCategory.RULE_EXCLUSION
    assert BarDropReason.IMPOSSIBLE_OHLC in ALERT_REASONS
    assert BarDropReason.LIMIT_UP_LOCKED not in ALERT_REASONS


def test_ledger_dedupes_and_keeps_highest_priority_reason():
    ledger = BarDropLedger()
    ledger.record(
        symbol="600519",
        session=START,
        timeframe="1d",
        reason=BarDropReason.MISSING_BAR,
        stage="coverage",
    )
    ledger.record(
        symbol="600519",
        session=START,
        timeframe="1d",
        reason=BarDropReason.IMPOSSIBLE_OHLC,
        stage="normalize",
        detail={"close": 0},
    )
    summary = ledger.summary()
    assert summary["total_bars"] == 1
    assert summary["total_events"] == 2
    assert summary["by_reason"] == {"impossible_ohlc": 1}
    assert summary["by_category"] == {"data_issue": 1}
    assert summary["samples"][0]["detail"] == {"close": 0}


def test_summary_aggregates_by_session_symbol_and_truncates_samples():
    ledger = BarDropLedger(max_entries=2)
    for index in range(5):
        ledger.record(
            symbol=f"60000{index}",
            session=START,
            timeframe="1d",
            reason=BarDropReason.MISSING_BAR,
            stage="coverage",
        )
    summary = ledger.summary(sample_limit=2, expected_symbol_sessions=10)
    assert summary["total_bars"] == 5
    assert summary["by_reason"] == {"missing_bar": 5}
    assert summary["by_session"] == {"2026-01-05": 5}
    assert len(summary["top_symbols"]) == 5
    assert len(summary["samples"]) == 2
    assert summary["truncated"] is True
    assert summary["drop_ratio_pct"] == 50.0


def test_snapshot_records_invalid_duplicate_and_missing_bars():
    keep = {"2026-01-05", "2026-01-07"}
    bars = [bar for bar in daily_bars() if bar.session.isoformat() in keep]
    invalid = DailyBar(
        symbol="600519",
        session=date(2026, 1, 8),
        open=Decimal("10"),
        high=Decimal("10.5"),
        low=Decimal("9.9"),
        close=Decimal("0"),
        volume=Decimal("1000"),
        amount=Decimal("0"),
        source="test",
    )
    duplicate = [
        DailyBar(
            symbol="600519",
            session=date(2026, 1, 9),
            open=Decimal("10"),
            high=Decimal("10.5"),
            low=Decimal("9.8"),
            close=Decimal(str(close)),
            volume=Decimal("1000"),
            amount=Decimal("0"),
            source="test",
        )
        for close in (10.1, 10.4)
    ]
    snapshot = SnapshotBuilder(
        run_id="ledger-snapshot",
        config=config(),
        plans=[plan()],
        calendar=CALENDAR,
        daily_bars=[*bars, invalid, *duplicate],
        minute_bars=[],
        rules=rules_for(),
    ).build()

    reasons = {entry.reason for entry in snapshot.bar_drops}
    assert BarDropReason.NON_POSITIVE_PRICE in reasons
    assert BarDropReason.DUPLICATE_BAR in reasons
    assert BarDropReason.MISSING_BAR in reasons
    assert "invalid_daily_bar_skipped" in snapshot.quality_flags


def test_snapshot_records_pre_listing_and_rule_suspension():
    bars = [bar for bar in daily_bars() if bar.session >= date(2026, 1, 7)]
    rules = []
    for rule in rules_for():
        if rule.session.isoformat() == "2026-01-06":
            rules.append(
                derive_rule(
                    "600519",
                    rule.session,
                    previous_close=rule.previous_close,
                    suspended=True,
                    st_status_verified=True,
                    listing_status_verified=True,
                    instrument_type_verified=True,
                )
            )
        else:
            rules.append(rule)
    snapshot = SnapshotBuilder(
        run_id="ledger-snapshot-coverage",
        config=config(),
        plans=[plan()],
        calendar=CALENDAR,
        daily_bars=bars,
        minute_bars=[],
        rules=rules,
    ).build()
    reasons = {entry.reason for entry in snapshot.bar_drops}
    assert BarDropReason.PRE_LISTING in reasons
    assert BarDropReason.RULE_SUSPENDED in reasons


def test_snapshot_records_partial_minute_session():
    from tests.backtest_helpers import full_session_minute_bars

    minute = full_session_minute_bars()[:-1]
    snapshot = SnapshotBuilder(
        run_id="ledger-snapshot-partial",
        config=config(granularity=Granularity.MINUTE_5),
        plans=[plan()],
        calendar=CALENDAR,
        daily_bars=daily_bars(),
        minute_bars=minute,
        rules=rules_for(),
    ).build()
    reasons = {entry.reason for entry in snapshot.bar_drops}
    assert BarDropReason.PARTIAL_SESSION in reasons


def test_execution_records_zero_volume_and_limit_up():
    engine = _engine()
    engine._match_one(_order(), _bar(START, volume="0"), CALENDAR.session_close(START), START, {"600519": 0}, is_buy=True)
    zero = engine._bar_drops.summary()
    assert zero["by_reason"] == {"zero_volume_suspended": 1}
    assert zero["by_category"] == {"rule_exclusion": 1}

    engine = _engine()
    rule = next(rule for rule in rules_for() if rule.session == START)
    limit_up = rule.limit_up
    bar = _bar(START, price=str(limit_up))
    engine._match_one(_order(), bar, CALENDAR.session_close(START), START, {"600519": 1000}, is_buy=True)
    summary = engine._bar_drops.summary()
    assert summary["by_reason"] == {"limit_up_locked": 1}
    assert summary["samples"][0]["detail"]["limit_up"] == str(limit_up)


def test_execution_records_no_bar_for_open_order():
    engine = _engine()
    engine.orders.append(_order())
    engine._match_orders({}, datetime(2026, 1, 6, 10, 0), date(2026, 1, 6))
    summary = engine._bar_drops.summary()
    assert summary["by_reason"] == {"no_bar_for_order": 1}


def test_execution_diagnostics_include_ledger_without_removing_existing_keys():
    engine = _engine()
    engine._match_one(_order(), _bar(START, volume="0"), CALENDAR.session_close(START), START, {"600519": 0}, is_buy=True)
    diagnostics = engine._build_diagnostics()
    assert "bar_drops" in diagnostics
    assert "bar_drop_counts" in diagnostics
    assert diagnostics["bar_drop_counts"] == {"zero_volume_suspended": 1}
    assert "fill_count" in diagnostics or "order_status_counts" in diagnostics
    assert diagnostics["bar_drops"]["samples"][0]["symbol"] == "600519"
