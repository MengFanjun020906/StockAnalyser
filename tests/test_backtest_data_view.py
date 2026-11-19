# -*- coding: utf-8 -*-
"""Snapshot, coverage, as-of view and corporate-action tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from src.backtest.data_view import InMemoryDataView
from src.backtest.entities import Bar, CorporateAction, DailyBar, Order
from src.backtest.ledger import Ledger
from src.backtest.schemas import (
    Granularity,
    MissingDataPolicy,
    OrderSide,
    OrderType,
    PlanInputRecord,
    SourcePipeline,
)
from src.backtest.snapshot import SnapshotBuilder
from tests.backtest_helpers import (
    CALENDAR,
    END,
    PRICES,
    SESSIONS,
    START,
    config,
    daily_bars,
    full_session_minute_bars,
    plan,
    rules_for,
    zero_fee_policy,
)


def _inputs():
    return [
        PlanInputRecord(
            input_id="input-1",
            source_id="plan-1",
            plan_id="plan-1",
            scenario_id="s1",
            symbol="600519",
            included=True,
            source_pipeline=SourcePipeline.SINGLE_STOCK,
        )
    ]


def _mixed_minute_bar(symbol: str, session: date, close: str = "10.5") -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        end_at=datetime.combine(session, datetime.min.time()).replace(hour=15),
        session=session,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        amount=Decimal("10000"),
        frequency="5m",
    )


def _mixed_daily_bar(symbol: str, session: date, close: str = "20.5") -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        symbol=symbol,
        session=session,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("2000"),
        amount=Decimal("20000"),
    )


def test_daily_bars_at_is_not_shadowed_by_minute_bars_of_other_symbols():
    session = date(2026, 1, 5)
    timestamp = datetime(2026, 1, 5, 15, 0)
    view = InMemoryDataView.from_records(
        granularity="1d",
        start_date=session,
        end_date=session,
        sessions=[session],
        bars=[_mixed_minute_bar("000001", session)],
        daily=[_mixed_daily_bar("000002", session)],
        rules=[],
    )
    bars = view.bars_at(timestamp)
    assert set(bars) == {"000002"}
    assert bars["000002"].frequency == "1d"
    assert bars["000002"].close == Decimal("20.5")
    assert view.bar_for("000001", timestamp) is None


def test_minute_bars_at_still_returns_minute_bars_only():
    session = date(2026, 1, 5)
    timestamp = datetime(2026, 1, 5, 15, 0)
    view = InMemoryDataView.from_records(
        granularity="5m",
        start_date=session,
        end_date=session,
        sessions=[session],
        bars=[_mixed_minute_bar("000001", session)],
        daily=[_mixed_daily_bar("000002", session)],
        rules=[],
    )
    bars = view.bars_at(timestamp)
    assert set(bars) == {"000001"}
    assert bars["000001"].frequency == "5m"
    assert bars["000001"].close == Decimal("10.5")
    assert view.bar_for("000002", timestamp) is None


def test_data_view_only_exposes_bars_up_to_session():
    view = InMemoryDataView.from_records(
        granularity="1d",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=[],
        daily=daily_bars(),
        rules=rules_for(),
    )
    visible = view.daily_bars("600519", date(2026, 1, 6))
    assert [bar.session for bar in visible] == [
        d for d in PRICES if d <= date(2026, 1, 6)
    ]
    assert all(bar.session <= date(2026, 1, 6) for bar in visible)
    price, stale = view.last_price("600519", date(2026, 1, 9))
    assert price == Decimal("12.3")
    assert stale is False


def test_strict_5m_incomplete_blocks_precheck_and_daily_downgrade_is_explicit():
    minute = full_session_minute_bars(date_=START)
    strict_config = config(granularity=Granularity.MINUTE_5)
    strict_builder = SnapshotBuilder(
        run_id="strict",
        config=strict_config,
        plans=[plan()],
        calendar=CALENDAR,
        minute_bars=minute,
        daily_bars=daily_bars(),
        rules=rules_for(),
    )
    strict = strict_builder.build()
    precheck = strict.to_precheck(
        inputs=_inputs(),
        config_hash="sha256:config",
        generated_at=datetime(2026, 1, 2, 20, 0),
    )
    assert precheck.status == "blocked"
    assert "incomplete_5m_coverage" in precheck.blocking_reasons
    assert any(row.sessions_missing or row.sessions_partial for row in strict.coverage)

    downgrade_config = config(granularity=Granularity.MINUTE_5).model_copy(
        update={"missing_data_policy": MissingDataPolicy.DAILY_DOWNGRADE}
    )
    downgrade_builder = SnapshotBuilder(
        run_id="downgrade",
        config=downgrade_config,
        plans=[plan()],
        calendar=CALENDAR,
        minute_bars=minute,
        daily_bars=daily_bars(),
        rules=rules_for(),
    )
    downgrade = downgrade_builder.build()
    downgrade_precheck = downgrade.to_precheck(
        inputs=_inputs(),
        config_hash="sha256:config",
        generated_at=datetime(2026, 1, 2, 20, 0),
    )
    assert downgrade_precheck.status != "blocked"
    assert "mixed_granularity" in downgrade.quality_flags
    assert downgrade.downgraded_sessions
    assert all(row.sessions_missing == 0 and row.sessions_partial == 0 for row in downgrade.coverage)


def test_snapshot_content_hash_is_deterministic():
    cfg = config()
    kwargs = dict(
        config=cfg,
        plans=[plan()],
        calendar=CALENDAR,
        minute_bars=[],
        daily_bars=daily_bars(),
        rules=rules_for(),
        created_at=datetime(2026, 1, 2, 20, 0),
    )
    first = SnapshotBuilder(run_id="same", **kwargs).build()
    second = SnapshotBuilder(run_id="same", **kwargs).build()
    assert first.manifest.content_hash == second.manifest.content_hash
    assert first.manifest.content_hash.startswith("sha256:")


def test_corporate_action_cash_and_share_adjustments():
    ledger = Ledger(
        run_id="run-test",
        initial_cash=Decimal("10000"),
        fee_policy=zero_fee_policy(),
        next_session=lambda value: value + timedelta(days=1),
        calendar_version="test",
    )
    buy = Order(
        order_id="buy-1",
        run_id="run-test",
        plan_key="plan-1",
        symbol="600519",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        limit_price=Decimal("10"),
        active_from=datetime(2026, 1, 5, 9, 30),
        remaining=100,
    )
    ledger.register_order(buy)
    ledger.reserve_cash(buy, Decimal("1000"), event_time=datetime(2026, 1, 5, 9, 30))
    ledger.apply_buy_fill(
        buy,
        quantity=100,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 35),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="full",
        reason="buy",
        fee_accumulator=ledger.fee_accumulator_for(buy, date(2026, 1, 5)),
    )
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600519",
            ex_date=date(2026, 1, 6),
            action_type="cash_dividend",
            cash_per_share=Decimal("0.5"),
            version="test",
            source="test",
            verified=True,
        ),
        event_time=datetime(2026, 1, 6, 9, 30),
    )
    assert ledger.cash == Decimal("9050.00")
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600519",
            ex_date=date(2026, 1, 7),
            action_type="stock_dividend",
            stock_ratio=Decimal("0.5"),
            version="test",
            source="test",
            verified=True,
        ),
        event_time=datetime(2026, 1, 7, 9, 30),
    )
    position = ledger.positions["600519"]
    assert position.quantity == 150
    assert position.cost_total == Decimal("1000.00")
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600519",
            ex_date=date(2026, 1, 8),
            action_type="split",
            split_ratio=Decimal("2"),
            version="test",
            source="test",
            verified=True,
        ),
        event_time=datetime(2026, 1, 8, 9, 30),
    )
    assert ledger.positions["600519"].quantity == 300
    assert ledger.positions["600519"].cost_total == Decimal("1000.00")
    ledger.assert_invariants()
