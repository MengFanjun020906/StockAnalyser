# -*- coding: utf-8 -*-
"""Ledger gold-standard, T+1, fee and invariant tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.entities import Order
from src.backtest.schemas import FeePolicy, FeeRule
from src.backtest.ledger import Ledger
from src.backtest.schemas import OrderSide, OrderType
from tests.backtest_helpers import gold_fee_policy, zero_fee_policy


def _ledger(policy=None, initial="10000") -> Ledger:
    return Ledger(
        run_id="run-test",
        initial_cash=Decimal(initial),
        fee_policy=policy or gold_fee_policy(),
        next_session=lambda value: value + timedelta(days=1),
        calendar_version="test",
        count_trading_days=lambda start, end: (end - start).days,
    )


def _buy_order(quantity=100, price="10") -> Order:
    return Order(
        order_id="buy-1",
        run_id="run-test",
        plan_key="plan-1",
        symbol="600519",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=Decimal(price),
        active_from=datetime(2026, 1, 5, 9, 30),
        expires_on_session=date(2026, 1, 5),
        remaining=quantity,
    )


def test_fee_gold_standard_buy_sell():
    """100 shares @10 buy, 100 @11 sell: 1005 out, 1094.45 in, +89.45."""
    ledger = _ledger()
    buy = _buy_order()
    ledger.register_order(buy)
    ledger.reserve_cash(buy, Decimal("1005"), event_time=datetime(2026, 1, 5, 9, 30))
    ledger.apply_buy_fill(
        buy,
        quantity=100,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 35),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="full",
        reason="test_buy",
        fee_accumulator=ledger.fee_accumulator_for(buy, date(2026, 1, 5)),
    )
    assert ledger.cash == Decimal("8995.00")
    assert ledger.reserved_cash == Decimal("0.00")

    sell = Order(
        order_id="sell-1",
        run_id="run-test",
        plan_key="plan-1",
        symbol="600519",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        limit_price=Decimal("11"),
        active_from=datetime(2026, 1, 6, 9, 30),
        remaining=100,
    )
    ledger.register_order(sell)
    ledger.reserve_sell_quantity(sell, event_time=datetime(2026, 1, 6, 9, 30), session=date(2026, 1, 6))
    ledger.apply_sell_fill(
        sell,
        quantity=100,
        price=Decimal("11"),
        bar_end_at=datetime(2026, 1, 6, 9, 35),
        session=date(2026, 1, 6),
        slippage_cost=Decimal("0"),
        quality="full",
        reason="test_sell",
        fee_accumulator=ledger.fee_accumulator_for(sell, date(2026, 1, 6)),
        exit_reason="take_profit_1",
    )
    assert ledger.cash == Decimal("10089.45")
    assert ledger.realized_pnl == Decimal("89.45")
    assert ledger.fees_paid == Decimal("10.55")
    assert len(ledger.closed_trades) == 1
    assert ledger.closed_trades[0].realized_pnl == Decimal("89.45")
    ledger.assert_invariants()


def test_t_plus_one_blocks_same_day_sell_and_gap_stop():
    ledger = _ledger(policy=zero_fee_policy())
    buy = _buy_order()
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
    sell = Order(
        order_id="sell-t1",
        run_id="run-test",
        plan_key="plan-1",
        symbol="600519",
        side=OrderSide.SELL,
        order_type=OrderType.STOP_MARKET,
        quantity=100,
        limit_price=None,
        stop_price=Decimal("9"),
        active_from=datetime(2026, 1, 5, 10, 0),
        remaining=100,
    )
    with pytest.raises(Exception):
        ledger.reserve_sell_quantity(sell, event_time=datetime(2026, 1, 5, 10, 0), session=date(2026, 1, 5))

    ledger.reserve_sell_quantity(sell, event_time=datetime(2026, 1, 6, 9, 30), session=date(2026, 1, 6))
    assert ledger.available_sellable_quantity("600519", date(2026, 1, 6)) == 100
    ledger.apply_sell_fill(
        sell,
        quantity=100,
        price=Decimal("8"),
        bar_end_at=datetime(2026, 1, 6, 9, 35),
        session=date(2026, 1, 6),
        slippage_cost=Decimal("0"),
        quality="stop_market",
        reason="stop_loss_gap",
        fee_accumulator=ledger.fee_accumulator_for(sell, date(2026, 1, 6)),
        exit_reason="stop_loss",
    )
    assert ledger.closed_trades[0].exit_price == Decimal("8")
    assert ledger.cash == Decimal("9800.00")
    ledger.assert_invariants()


def test_minimum_commission_charged_once_per_order_day_across_partial_fills():
    ledger = _ledger(policy=gold_fee_policy())
    buy = _buy_order(quantity=200)
    ledger.register_order(buy)
    ledger.reserve_cash(buy, Decimal("2005"), event_time=datetime(2026, 1, 5, 9, 30))
    accumulator = ledger.fee_accumulator_for(buy, date(2026, 1, 5))
    ledger.apply_buy_fill(
        buy,
        quantity=100,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 35),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="partial_full",
        reason="partial",
        fee_accumulator=accumulator,
    )
    ledger.apply_buy_fill(
        buy,
        quantity=100,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 40),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="full",
        reason="partial",
        fee_accumulator=accumulator,
    )
    ledger.settle_fees(date(2026, 1, 5), event_time=datetime(2026, 1, 5, 15, 0))
    assert ledger.fees_paid == Decimal("5.00")
    ledger.assert_invariants()


def test_reserved_cash_cannot_exceed_balance():
    ledger = _ledger(policy=zero_fee_policy())
    first = _buy_order(quantity=100, price="100")
    second = Order(
        order_id="buy-2",
        run_id="run-test",
        plan_key="plan-2",
        symbol="600000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        limit_price=Decimal("100"),
        active_from=datetime(2026, 1, 5, 9, 30),
        remaining=100,
    )
    ledger.register_order(first)
    ledger.reserve_cash(first, Decimal("10000"), event_time=datetime(2026, 1, 5, 9, 30))
    with pytest.raises(Exception):
        ledger.reserve_cash(second, Decimal("10000"), event_time=datetime(2026, 1, 5, 9, 30))
    assert ledger.cash >= 0
    assert ledger.available_cash == 0
    ledger.assert_invariants()


def test_fee_refund_is_allocated_to_buy_lot_cost():
    policy = FeePolicy(
        version="test-refund",
        rules=[
            FeeRule(
                effective_from=date(2000, 1, 1),
                commission_bps=Decimal("10"),
                minimum_commission=Decimal("5"),
                transfer_fee_bps=Decimal("0"),
                stamp_duty_sell_bps=Decimal("0"),
                stamp_duty_buy_bps=Decimal("0"),
            )
        ],
    )
    ledger = _ledger(policy=policy)
    buy = _buy_order(quantity=200)
    ledger.register_order(buy)
    ledger.reserve_cash(buy, Decimal("2005"), event_time=datetime(2026, 1, 5, 9, 30))
    accumulator = ledger.fee_accumulator_for(buy, date(2026, 1, 5))
    ledger.apply_buy_fill(
        buy,
        quantity=10,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 35),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="partial_full",
        reason="partial",
        fee_accumulator=accumulator,
    )
    ledger.apply_buy_fill(
        buy,
        quantity=190,
        price=Decimal("10"),
        bar_end_at=datetime(2026, 1, 5, 9, 40),
        session=date(2026, 1, 5),
        slippage_cost=Decimal("0"),
        quality="full",
        reason="partial",
        fee_accumulator=accumulator,
    )
    ledger.settle_fees(date(2026, 1, 5), event_time=datetime(2026, 1, 5, 15, 0))
    assert ledger.fees_paid == Decimal("5.00")
    assert ledger.cash == Decimal("7995.00")
    position = ledger.positions["600519"]
    assert position.cost_total == Decimal("2005.00")
    ledger.assert_invariants()
