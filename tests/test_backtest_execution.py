# -*- coding: utf-8 -*-
"""Matching, risk-policy, T+1 and ambiguity tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.backtest.data_view import InMemoryDataView
from src.backtest.entities import DailyBar
from src.backtest.execution import ExecutionEngine
from src.backtest.rules import derive_rule
from src.backtest.schemas import Granularity
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


def _engine(plans, data, cfg=None):
    return ExecutionEngine(
        run_id="run-test",
        config=cfg or config(),
        plans=list(plans),
        data=data,
        calendar=CALENDAR,
        fee_policy=zero_fee_policy(),
    )


def _daily_data(symbol: str, prices: dict[date, tuple]) -> InMemoryDataView:
    bars = [
        DailyBar(
            symbol=symbol,
            session=session,
            open=Decimal(str(values[0])),
            high=Decimal(str(values[1])),
            low=Decimal(str(values[2])),
            close=Decimal(str(values[3])),
            volume=Decimal(str(values[4])),
            amount=Decimal("0"),
            source="test",
        )
        for session, values in prices.items()
    ]
    rules = []
    for session in SESSIONS:
        prev = CALENDAR.previous_session(session)
        close = prices.get(prev, prices[min(prices)])[3]
        rules.append(
            derive_rule(
                symbol,
                session,
                previous_close=Decimal(str(close)),
                st_status_verified=True,
                listing_status_verified=True,
                instrument_type_verified=True,
            )
        )
    return InMemoryDataView.from_records(
        granularity="1d",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=[],
        daily=bars,
        rules=rules,
    )


def test_open_gap_below_entry_zone_does_not_auto_buy():
    prices = {
        CALENDAR.previous_session(START): (9.9, 10.0, 9.8, 9.9, 1_000_000),
        date(2026, 1, 5): (9.5, 9.8, 9.4, 9.6, 1_000_000),
        date(2026, 1, 6): (10.1, 10.4, 10.0, 10.2, 1_000_000),
        date(2026, 1, 7): (10.2, 10.4, 10.0, 10.3, 1_000_000),
        date(2026, 1, 8): (10.3, 10.5, 10.1, 10.4, 1_000_000),
        date(2026, 1, 9): (10.4, 10.6, 10.2, 10.5, 1_000_000),
    }
    p = plan(entry_low="10.0", entry_high="10.2", stop_loss="9.4", target_1="11.0", target_2=None)
    p = p.model_copy(update={"order_valid_trading_days": 2})
    result = _engine([p], _daily_data("600519", prices)).run()
    assert len(result.fills) == 1
    assert result.fills[0].session == date(2026, 1, 6)
    assert result.fills[0].price >= Decimal("10.1")
    assert result.plan_states["plan-1:s1"].status == "filled"


def test_one_price_limit_up_blocks_buy():
    prices = {
        CALENDAR.previous_session(START): (10.0, 10.1, 9.9, 10.0, 1_000_000),
        date(2026, 1, 5): (11.0, 11.0, 11.0, 11.0, 1_000_000),
        date(2026, 1, 6): (11.2, 11.4, 11.1, 11.3, 1_000_000),
        date(2026, 1, 7): (11.3, 11.5, 11.2, 11.4, 1_000_000),
        date(2026, 1, 8): (11.4, 11.6, 11.3, 11.5, 1_000_000),
        date(2026, 1, 9): (11.5, 11.7, 11.4, 11.6, 1_000_000),
    }
    p = plan(entry_low="10.5", entry_high="11.2", stop_loss="10.4", target_1="12.0", target_2=None)
    p = p.model_copy(update={"order_valid_trading_days": 1})
    result = _engine([p], _daily_data("600519", prices)).run()
    assert result.fills == []
    assert result.plan_states["plan-1:s1"].status == "not_triggered"


def test_stop_and_target_same_bar_uses_stop_first_and_marks_ambiguous():
    # Replace day 2 with a bar that hits both stop and target.
    prices = dict(PRICES)
    prices[date(2026, 1, 6)] = (10.5, 11.5, 9.5, 10.0, 1_000_000)
    cfg = config().model_copy(update={"slippage_bps": Decimal("0")})
    result = _engine([plan()], _daily_data("600519", prices), cfg).run()
    stop_fills = [fill for fill in result.fills if fill.reason in {"stop_loss_gap", "stop_loss_intraday"}]
    assert stop_fills
    assert stop_fills[0].price == Decimal("9.70")
    assert result.diagnostics["ambiguous_fills"] == 1
    assert result.diagnostics["ambiguous_fill_details"][0]["policy"] == "stop_first"


def test_max_concurrent_positions_rejects_second_plan():
    prices = dict(PRICES)
    cfg = config()
    cfg.risk_policy = cfg.risk_policy.model_copy(update={"max_concurrent_positions": 1})
    p1 = plan(plan_key="plan-1:s1", symbol="600519")
    p2 = plan(plan_key="plan-2:s1", symbol="600000")
    p2 = p2.model_copy(update={"rank": 2})
    data = InMemoryDataView.from_records(
        granularity="1d",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=[],
        daily=daily_bars("600519", prices) + daily_bars("600000", prices),
        rules=rules_for("600519", prices) + rules_for("600000", prices),
    )
    result = _engine([p1, p2], data, cfg).run()
    assert result.plan_states["plan-2:s1"].status == "max_positions_reached"
    assert any(item["reason"] == "max_positions_reached" for item in result.diagnostics["rejections"])


def test_risk_per_trade_caps_position_below_single_name_limit():
    prices = dict(PRICES)
    prices[date(2026, 1, 6)] = (10.3, 10.8, 10.2, 10.5, 1_000_000)
    p = plan(stop_loss="9.0", target_1="13.0", target_2=None)
    cfg = config()
    result = _engine([p], _daily_data("600519", prices), cfg).run()
    state = result.plan_states["plan-1:s1"]
    assert state.approved_position_pct < Decimal("10")
    assert state.cap_reason == "risk_per_trade"


def test_order_expires_without_touch():
    p = plan(entry_low="10.0", entry_high="10.2", stop_loss="9.4", target_1="11.0", target_2=None)
    p = p.model_copy(update={"order_valid_trading_days": 1})
    # Day 1 never touches the zone; order must expire at the end of day 1.
    prices = dict(PRICES)
    prices[date(2026, 1, 5)] = (10.5, 10.8, 10.3, 10.6, 1_000_000)
    result = _engine([p], _daily_data("600519", prices)).run()
    assert result.fills == []
    assert result.plan_states["plan-1:s1"].status == "not_triggered"
    assert any(order.status.value == "expired" for order in result.orders)


def test_volume_participation_partial_fills_share_bar_capacity():
    minute = full_session_minute_bars(volume=1000)[:2]
    data = InMemoryDataView.from_records(
        granularity="5m",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=minute,
        daily=daily_bars(),
        rules=rules_for(),
    )
    cfg = config(granularity=Granularity.MINUTE_5)
    cfg = cfg.model_copy(update={"participation_rate": Decimal("0.1")})
    result = _engine([plan()], data, cfg).run()
    entry_fills = [fill for fill in result.fills if fill.side.value == "buy"]
    assert len(entry_fills) == 2
    assert sum(fill.quantity for fill in entry_fills) == 200
    buy_orders = [order for order in result.orders if order.side.value == "buy"]
    assert buy_orders[0].filled_quantity == 200
    assert buy_orders[0].remaining == buy_orders[0].quantity - 200


def test_benchmark_values_are_normalized_to_initial_cash():
    benchmark = {session: Decimal("100") + Decimal(index * 2) for index, session in enumerate(SESSIONS)}
    data = InMemoryDataView.from_records(
        granularity="1d",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=[],
        daily=daily_bars(),
        rules=rules_for(),
        benchmark=benchmark,
    )
    result = _engine([plan()], data).run()
    assert result.ledger.equity_points[0].benchmark_value == Decimal("100000")
    assert result.ledger.equity_points[-1].benchmark_value == Decimal("108000")


def test_close_confirmation_only_creates_order_for_next_session():
    p = plan(requires_confirmation=True)
    p = p.model_copy(update={"scenario_json": {}})
    result = _engine([p], _daily_data("600519", PRICES)).run()
    entry_fills = [fill for fill in result.fills if fill.side.value == "buy"]
    assert entry_fills
    # The plan was confirmed at the first daily close; the entry order must not
    # fill on that same bar.
    assert entry_fills[0].session == date(2026, 1, 6)


def test_same_time_buy_orders_compete_for_cash_and_never_overspend():
    prices = dict(PRICES)
    cfg = config(initial_cash="10000")
    cfg.risk_policy = cfg.risk_policy.model_copy(
        update={
            "max_single_position_pct": Decimal("100"),
            "max_total_exposure_pct": Decimal("100"),
            "min_cash_pct": Decimal("0"),
            "risk_per_trade_pct": Decimal("100"),
            "max_concurrent_positions": 5,
        }
    )
    first = plan(plan_key="plan-1:s1", symbol="600519").model_copy(
        update={"target_position_pct": Decimal("100"), "rank": 1}
    )
    second = plan(plan_key="plan-2:s1", symbol="600000").model_copy(
        update={"target_position_pct": Decimal("100"), "rank": 2}
    )
    data = InMemoryDataView.from_records(
        granularity="1d",
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=[],
        daily=daily_bars("600519", prices) + daily_bars("600000", prices),
        rules=rules_for("600519", prices) + rules_for("600000", prices),
    )
    result = _engine([second, first], data, cfg).run()
    assert len([fill for fill in result.fills if fill.side.value == "buy"]) == 1
    assert result.plan_states["plan-2:s1"].status == "insufficient_cash"
    assert result.ledger.cash >= 0
    assert result.ledger.reserved_cash >= 0
