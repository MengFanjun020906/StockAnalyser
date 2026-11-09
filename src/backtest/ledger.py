# -*- coding: utf-8 -*-
"""Cash, reservation, lot and realized-PnL ledger.

Every state change emits an append-only ``LedgerEvent``.  The runner flushes
those events and the accompanying order/fill rows in one SQLite transaction per
timestamp, so a crash cannot leave half a fill without its cash/lot mutation.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Callable, Iterable

from .entities import (
    ClosedTrade,
    CorporateAction,
    EquityPoint,
    Fill,
    LedgerEvent,
    Order,
    Position,
    PositionLot,
)
from .fees import FeeAccumulator, FeeDelta, money
from .schemas import FeePolicy, OrderSide, OrderStatus


class LedgerError(RuntimeError):
    pass


class LedgerInvariantError(LedgerError):
    pass


class Ledger:
    """Deterministic single-account ledger for one experiment."""

    def __init__(
        self,
        *,
        run_id: str,
        initial_cash: Decimal,
        fee_policy: FeePolicy,
        next_session: Callable[[date], date],
        calendar_version: str,
        count_trading_days: Callable[[date, date], int] | None = None,
    ) -> None:
        self.run_id = run_id
        self.initial_cash = money(initial_cash)
        self.cash = self.initial_cash
        self.reserved_cash = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.fees_paid = Decimal("0")
        self.slippage_paid = Decimal("0")
        self.fee_policy = fee_policy
        self.next_session = next_session
        self.calendar_version = calendar_version
        self.count_trading_days = count_trading_days or (lambda start, end: max(0, (end - start).days))
        self.positions: dict[str, Position] = {}
        self.lots: dict[str, list[PositionLot]] = {}
        self.events: list[LedgerEvent] = []
        self.equity_points: list[EquityPoint] = []
        self.closed_trades: list[ClosedTrade] = []
        self._sequence = 0
        self._peak_equity = self.initial_cash
        self._fee_orders_by_session: dict[date, set[str]] = {}
        self._orders: dict[str, Order] = {}
        self._quality_flags: set[str] = set()

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def available_cash(self) -> Decimal:
        return money(self.cash - self.reserved_cash)

    @property
    def total_market_value(self) -> Decimal:
        return money(sum((position.market_value for position in self.positions.values()), Decimal("0")))

    @property
    def total_equity(self) -> Decimal:
        return money(self.cash + self.total_market_value)

    @property
    def quality_flags(self) -> list[str]:
        return sorted(self._quality_flags)

    def get_position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def sellable_quantity(self, symbol: str, session: date) -> int:
        return sum(
            lot.remaining
            for lot in self.lots.get(symbol, [])
            if lot.remaining > 0 and lot.available_session <= session
        )

    def reserved_sell_quantity(self, symbol: str, *, exclude_order_id: str | None = None) -> int:
        total = 0
        for order in self._orders.values():
            if order.symbol != symbol or not order.is_open or order.side is not OrderSide.SELL:
                continue
            if exclude_order_id and order.order_id == exclude_order_id:
                continue
            total += order.reserved_quantity
        return total

    def available_sellable_quantity(self, symbol: str, session: date, *, exclude_order_id: str | None = None) -> int:
        return max(
            0,
            self.sellable_quantity(symbol, session)
            - self.reserved_sell_quantity(symbol, exclude_order_id=exclude_order_id),
        )

    def register_order(self, order: Order) -> None:
        self._orders[order.order_id] = order

    # ------------------------------------------------------------------
    # Reservation
    # ------------------------------------------------------------------
    def reserve_cash(self, order: Order, amount: Decimal, *, event_time: datetime) -> None:
        amount = money(amount)
        if amount <= 0:
            raise LedgerError("reservation amount must be positive")
        if amount > self.available_cash:
            raise LedgerError("insufficient available cash for reservation")
        order.reserved_cash += amount
        self.reserved_cash = money(self.reserved_cash + amount)
        self._append_event(
            event_time,
            "cash_reserved",
            symbol=order.symbol,
            order_id=order.order_id,
            reserved_delta=amount,
            reason="order_entry_reservation",
        )
        self.assert_invariants()

    def release_order_cash(self, order: Order, *, event_time: datetime, reason: str) -> Decimal:
        released = order.reserved_cash
        if released <= 0:
            return Decimal("0")
        order.reserved_cash = Decimal("0")
        self.reserved_cash = money(self.reserved_cash - released)
        if self.reserved_cash < 0:
            raise LedgerInvariantError("reserved cash became negative")
        self._append_event(
            event_time,
            "cash_released",
            symbol=order.symbol,
            order_id=order.order_id,
            reserved_delta=-released,
            reason=reason,
        )
        self.assert_invariants()
        return released

    def reserve_sell_quantity(self, order: Order, *, event_time: datetime, session: date) -> None:
        if order.quantity <= 0:
            raise LedgerError("sell quantity must be positive")
        if order.reserved_quantity > 0:
            return
        available = self.available_sellable_quantity(
            order.symbol,
            session,
            exclude_order_id=order.order_id,
        )
        if order.quantity > available:
            raise LedgerError(
                f"sell quantity {order.quantity} exceeds available sellable quantity {available}"
            )
        order.reserved_quantity = order.quantity
        self._append_event(
            event_time,
            "quantity_reserved",
            symbol=order.symbol,
            order_id=order.order_id,
            payload={"quantity": order.quantity},
            reason="sell_order_reservation",
        )

    def release_order_quantity(self, order: Order, *, event_time: datetime, reason: str) -> None:
        if order.reserved_quantity <= 0:
            return
        released = order.reserved_quantity
        order.reserved_quantity = 0
        self._append_event(
            event_time,
            "quantity_released",
            symbol=order.symbol,
            order_id=order.order_id,
            payload={"quantity": released},
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------
    def apply_buy_fill(
        self,
        order: Order,
        *,
        quantity: int,
        price: Decimal,
        bar_end_at: datetime,
        session: date,
        slippage_cost: Decimal,
        quality: str,
        reason: str,
        fee_accumulator: FeeAccumulator,
    ) -> Fill:
        if quantity <= 0:
            raise LedgerError("fill quantity must be positive")
        if quantity > order.remaining:
            raise LedgerError("fill exceeds order remaining quantity")
        if price <= 0:
            raise LedgerError("fill price must be positive")
        gross = money(price * Decimal(quantity))
        fee_delta = fee_accumulator.on_fill(gross)
        self._touch_fee_order(session, order)
        total_cost = money(gross + fee_delta.total)
        remaining_before = order.remaining
        pro_rata_reserved = self._pro_rata_reserved(order, quantity, remaining_before)
        if total_cost >= pro_rata_reserved:
            reserved_consumed = pro_rata_reserved
            extra = money(total_cost - pro_rata_reserved)
            if extra > self.available_cash:
                raise LedgerInvariantError(
                    f"buy fill extra cost {extra} exceeds available cash {self.available_cash}"
                )
        else:
            reserved_consumed = total_cost
            extra = Decimal("0")
        order.reserved_cash = money(order.reserved_cash - reserved_consumed)
        self.reserved_cash = money(self.reserved_cash - reserved_consumed)
        self.cash = money(self.cash - total_cost)
        self.fees_paid = money(self.fees_paid + fee_delta.total)
        self.slippage_paid = money(self.slippage_paid + money(slippage_cost))
        self._update_order_after_fill(order, quantity, price)
        fill = self._new_fill(
            order,
            quantity=quantity,
            price=price,
            gross=gross,
            fees=fee_delta,
            slippage_cost=slippage_cost,
            bar_end_at=bar_end_at,
            session=session,
            quality=quality,
            reason=reason,
        )
        self._create_lot(order, fill, session=session, total_cost=total_cost)
        self._append_event(
            bar_end_at,
            "buy_fill",
            symbol=order.symbol,
            order_id=order.order_id,
            fill_id=fill.fill_id,
            cash_delta=-total_cost,
            reserved_delta=-reserved_consumed,
            quantity_delta=quantity,
            cost_delta=total_cost,
            fees_delta=fee_delta.total,
            reason=reason,
            payload={"price": str(price), "gross": str(gross), "extra_cash": str(extra)},
            idempotency_key=f"{order.order_id}:{fill.fill_id}:buy",
        )
        if order.remaining <= 0:
            self._complete_order(order, bar_end_at, reason="filled")
        self.assert_invariants()
        return fill

    def apply_sell_fill(
        self,
        order: Order,
        *,
        quantity: int,
        price: Decimal,
        bar_end_at: datetime,
        session: date,
        slippage_cost: Decimal,
        quality: str,
        reason: str,
        fee_accumulator: FeeAccumulator,
        exit_reason: str | None = None,
    ) -> Fill:
        if quantity <= 0:
            raise LedgerError("fill quantity must be positive")
        if quantity > order.remaining:
            raise LedgerError("fill exceeds order remaining quantity")
        sellable = self.sellable_quantity(order.symbol, session)
        reserved_by_others = self.reserved_sell_quantity(order.symbol, exclude_order_id=order.order_id)
        if quantity > sellable - reserved_by_others:
            raise LedgerInvariantError(
                f"sell fill {quantity} exceeds sellable quantity "
                f"{sellable - reserved_by_others} for {order.symbol}"
            )
        gross = money(price * Decimal(quantity))
        fee_delta = fee_accumulator.on_fill(gross)
        self._touch_fee_order(session, order)
        net_proceeds = money(gross - fee_delta.total)
        if net_proceeds < 0:
            raise LedgerInvariantError("sell proceeds after fees cannot be negative")
        consumed = self._consume_lots(order.symbol, quantity, session=session)
        cost_consumed = sum((item[2] for item in consumed), Decimal("0"))
        realized = money(net_proceeds - cost_consumed)
        self.cash = money(self.cash + net_proceeds)
        self.realized_pnl = money(self.realized_pnl + realized)
        self.fees_paid = money(self.fees_paid + fee_delta.total)
        self.slippage_paid = money(self.slippage_paid + money(slippage_cost))
        self._record_closed_trades(
            order,
            consumed,
            net_proceeds=net_proceeds,
            exit_price=price,
            exit_session=session,
            exit_reason=exit_reason or reason,
        )
        order.reserved_quantity = max(0, order.reserved_quantity - quantity)
        self._update_order_after_fill(order, quantity, price)
        fill = self._new_fill(
            order,
            quantity=quantity,
            price=price,
            gross=gross,
            fees=fee_delta,
            slippage_cost=slippage_cost,
            bar_end_at=bar_end_at,
            session=session,
            quality=quality,
            reason=reason,
        )
        self._append_event(
            bar_end_at,
            "sell_fill",
            symbol=order.symbol,
            order_id=order.order_id,
            fill_id=fill.fill_id,
            cash_delta=net_proceeds,
            reserved_delta=Decimal("0"),
            quantity_delta=-quantity,
            cost_delta=-cost_consumed,
            realized_pnl_delta=realized,
            fees_delta=fee_delta.total,
            reason=reason,
            payload={"price": str(price), "gross": str(gross), "cost_consumed": str(cost_consumed)},
            idempotency_key=f"{order.order_id}:{fill.fill_id}:sell",
        )
        if order.remaining <= 0:
            self.release_order_quantity(order, event_time=bar_end_at, reason="filled")
            self._complete_order(order, bar_end_at, reason="filled")
        self.assert_invariants()
        return fill

    def settle_fees(self, session: date, *, event_time: datetime) -> dict[str, Decimal]:
        """Settle minimum-commission / rounding adjustments for all touched orders."""
        touched = self._fee_orders_by_session.pop(session, set())
        adjustments: dict[str, Decimal] = {}
        for order_id in sorted(touched):
            order = self._orders.get(order_id)
            accumulator = getattr(order, "fee_accumulator", None) if order else None
            if order is None or accumulator is None or accumulator.fill_count == 0:
                continue
            delta = accumulator.settle_day()
            if delta.total == 0:
                continue
            self._apply_fee_adjustment(order, delta, event_time, session)
            adjustments[order_id] = delta.total
        self.assert_invariants()
        return adjustments

    def _apply_fee_adjustment(
        self,
        order: Order,
        delta: FeeDelta,
        event_time: datetime,
        session: date,
    ) -> None:
        total = money(delta.commission + delta.transfer_fee + delta.stamp_duty)
        if total == 0:
            return
        if total > 0:
            if total > self.available_cash:
                raise LedgerInvariantError(
                    f"fee adjustment {total} exceeds available cash for {order.order_id}"
                )
            self.cash = money(self.cash - total)
            if order.is_open and order.reserved_cash >= total:
                order.reserved_cash = money(order.reserved_cash - total)
                self.reserved_cash = money(self.reserved_cash - total)
        else:
            self.cash = money(self.cash - total)
        if order.side is OrderSide.BUY:
            self._adjust_buy_lot_costs(order, session, total)
        else:
            self.realized_pnl = money(self.realized_pnl - total)
            self._adjust_closed_trade_fees(order, session, total)
        self.fees_paid = money(self.fees_paid + total)
        self._append_event(
            event_time,
            "fee_adjustment",
            symbol=order.symbol,
            order_id=order.order_id,
            cash_delta=-total,
            reserved_delta=Decimal("-1") * total if total > 0 and order.is_open else Decimal("0"),
            fees_delta=total,
            reason="daily_fee_settlement",
            payload={
                "commission": str(delta.commission),
                "transfer_fee": str(delta.transfer_fee),
                "stamp_duty": str(delta.stamp_duty),
            },
            idempotency_key=f"{order.order_id}:{session.isoformat()}:fee",
        )

    def _adjust_buy_lot_costs(self, order: Order, session: date, total: Decimal) -> None:
        lots = [
            lot
            for lot in self.lots.get(order.symbol, [])
            if lot.source_order_id == order.order_id
            and lot.acquired_session == session
            and lot.remaining > 0
        ]
        if not lots:
            if total != 0:
                self._quality_flags.add("fee_adjustment_unallocated")
            return
        total_quantity = sum(lot.remaining for lot in lots)
        allocated = Decimal("0")
        for index, lot in enumerate(lots):
            if index == len(lots) - 1:
                share = money(total - allocated)
            else:
                share = money(total * Decimal(lot.remaining) / Decimal(total_quantity))
            allocated = money(allocated + share)
            lot.cost_total = money(lot.cost_total + share)
            lot.cost_per_share = (
                lot.cost_total / Decimal(lot.remaining) if lot.remaining > 0 else Decimal("0")
            )
        position = self.positions.get(order.symbol)
        if position is not None:
            position.cost_total = money(
                sum((lot.cost_total for lot in self.lots.get(order.symbol, [])), Decimal("0"))
            )

    def _adjust_closed_trade_fees(self, order: Order, session: date, total: Decimal) -> None:
        trades = [
            trade
            for trade in self.closed_trades
            if trade.exit_order_id == order.order_id and trade.exit_session == session
        ]
        if not trades:
            if total != 0:
                self._quality_flags.add("fee_adjustment_unallocated")
            return
        total_cost = sum((trade.cost_basis for trade in trades), Decimal("0"))
        allocated = Decimal("0")
        for index, trade in enumerate(trades):
            if index == len(trades) - 1:
                share = money(-total - allocated)
            elif total_cost > 0:
                share = money(-total * trade.cost_basis / total_cost)
            else:
                share = Decimal("0")
            allocated = money(allocated + share)
            trade.net_proceeds = money(trade.net_proceeds + share)
            trade.realized_pnl = money(trade.realized_pnl + share)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def expire_order(self, order: Order, *, event_time: datetime, reason: str) -> None:
        if not order.is_open:
            return
        order.status = OrderStatus.EXPIRED
        order.reason = reason
        self.release_order_cash(order, event_time=event_time, reason=reason)
        self.release_order_quantity(order, event_time=event_time, reason=reason)
        self._append_event(
            event_time,
            "order_expired",
            symbol=order.symbol,
            order_id=order.order_id,
            reason=reason,
            payload={"remaining": order.remaining},
        )

    def cancel_order(self, order: Order, *, event_time: datetime, reason: str) -> None:
        if not order.is_open:
            return
        order.status = OrderStatus.CANCELLED
        order.reason = reason
        self.release_order_cash(order, event_time=event_time, reason=reason)
        self.release_order_quantity(order, event_time=event_time, reason=reason)
        self._append_event(
            event_time,
            "order_cancelled",
            symbol=order.symbol,
            order_id=order.order_id,
            reason=reason,
            payload={"remaining": order.remaining},
        )

    # ------------------------------------------------------------------
    # Corporate actions / valuation
    # ------------------------------------------------------------------
    def apply_corporate_action(
        self,
        action: CorporateAction,
        *,
        event_time: datetime,
    ) -> dict[str, Decimal]:
        position = self.positions.get(action.symbol)
        if position is None or position.quantity <= 0:
            return {}
        result: dict[str, Decimal] = {}
        action_type = action.action_type
        if action_type in {"cash_dividend", "dividend"}:
            amount = money(Decimal(position.quantity) * action.cash_per_share)
            if amount > 0:
                self.cash = money(self.cash + amount)
                self._append_event(
                    event_time,
                    "cash_dividend",
                    symbol=action.symbol,
                    cash_delta=amount,
                    reason=action.action_type,
                    payload=action.to_dict(),
                )
                result["cash"] = amount
        elif action_type in {"stock_dividend", "split", "reverse_split"}:
            if action_type == "stock_dividend":
                ratio = Decimal("1") + action.stock_ratio
            elif action_type == "split":
                ratio = action.split_ratio
            else:
                ratio = Decimal("1") / action.split_ratio if action.split_ratio > 0 else Decimal("1")
            if ratio <= 0:
                return result
            self._adjust_lots_for_ratio(action.symbol, ratio, action.ex_date, event_time, action)
            result["quantity_ratio"] = ratio
        else:
            self._quality_flags.add(f"unsupported_corporate_action:{action_type}")
            self._append_event(
                event_time,
                "corporate_action_unsupported",
                symbol=action.symbol,
                reason=action_type,
                payload=action.to_dict(),
            )
        return result

    def _adjust_lots_for_ratio(
        self,
        symbol: str,
        ratio: Decimal,
        ex_date: date,
        event_time: datetime,
        action: CorporateAction,
    ) -> None:
        for lot in self.lots.get(symbol, []):
            if lot.remaining <= 0:
                continue
            original = lot.remaining
            new_quantity = int((Decimal(original) * ratio).to_integral_value(rounding=ROUND_FLOOR))
            if new_quantity <= 0:
                self._quality_flags.add("fractional_corporate_action_dropped")
                lot.remaining = 0
                lot.cost_total = Decimal("0")
                continue
            lot.quantity = new_quantity
            lot.remaining = new_quantity
            lot.cost_per_share = (
                lot.cost_total / Decimal(new_quantity) if new_quantity > 0 else Decimal("0")
            )
            lot.available_session = max(lot.available_session, ex_date)
            if new_quantity != original:
                self._quality_flags.add("fractional_corporate_action_dropped")
        position = self.positions.get(symbol)
        if position is not None:
            position.quantity = sum(lot.remaining for lot in self.lots.get(symbol, []))
            position.cost_total = sum((lot.cost_total for lot in self.lots.get(symbol, [])), Decimal("0"))
        self._append_event(
            event_time,
            "position_ratio_adjustment",
            symbol=symbol,
            quantity_delta=0,
            reason=action.action_type,
            payload={"ratio": str(ratio), "action": action.to_dict()},
        )

    def mark_price(self, symbol: str, price: Decimal | None, session: date, *, stale: bool) -> None:
        position = self.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return
        if price is not None and price > 0:
            position.market_price = price
            position.market_price_session = session
            position.stale = stale
        else:
            position.stale = True

    def close_session(self, session: date, timestamp: datetime, *, granularity: str, quality: str) -> EquityPoint:
        equity = self.total_equity
        if equity > self._peak_equity:
            self._peak_equity = equity
        drawdown = Decimal("0") if self._peak_equity <= 0 else equity / self._peak_equity - Decimal("1")
        stale = any(position.stale for position in self.positions.values() if position.quantity > 0)
        point = EquityPoint(
            session=session,
            timestamp=timestamp,
            cash=self.cash,
            reserved_cash=self.reserved_cash,
            market_value=self.total_market_value,
            equity=equity,
            peak_equity=self._peak_equity,
            drawdown=drawdown,
            stale=stale,
            benchmark_value=None,
            granularity=granularity,
            quality="stale" if stale else quality,
        )
        self.equity_points.append(point)
        return point

    def positions_snapshot(self, session: date) -> list[dict[str, object]]:
        result = []
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            if position.quantity <= 0:
                continue
            result.append(
                {
                    "symbol": symbol,
                    "quantity": position.quantity,
                    "sellable_quantity": self.available_sellable_quantity(symbol, session),
                    "avg_cost": str(position.avg_cost),
                    "cost_total": str(position.cost_total),
                    "market_price": str(position.market_price) if position.market_price is not None else None,
                    "market_value": str(position.market_value),
                    "unrealized_pnl": str(position.unrealized_pnl),
                    "stale": position.stale,
                    "market_price_session": position.market_price_session.isoformat()
                    if position.market_price_session
                    else None,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    def assert_invariants(self) -> None:
        if self.cash < 0:
            raise LedgerInvariantError(f"cash is negative: {self.cash}")
        if self.reserved_cash < 0:
            raise LedgerInvariantError(f"reserved cash is negative: {self.reserved_cash}")
        if self.reserved_cash > self.cash:
            raise LedgerInvariantError(
                f"reserved cash {self.reserved_cash} exceeds cash {self.cash}"
            )
        for symbol, position in self.positions.items():
            lots = self.lots.get(symbol, [])
            lot_quantity = sum(lot.remaining for lot in lots)
            lot_cost = sum((lot.cost_total for lot in lots), Decimal("0"))
            if position.quantity != lot_quantity:
                raise LedgerInvariantError(
                    f"position quantity mismatch for {symbol}: {position.quantity} != {lot_quantity}"
                )
            if abs(position.cost_total - lot_cost) > Decimal("0.01"):
                raise LedgerInvariantError(
                    f"position cost mismatch for {symbol}: {position.cost_total} != {lot_cost}"
                )
            sellable = sum(lot.remaining for lot in lots if lot.remaining > 0)
            if sellable > position.quantity:
                raise LedgerInvariantError(f"sellable exceeds position for {symbol}")
        if abs(self.total_equity - (self.cash + self.total_market_value)) > Decimal("0.01"):
            raise LedgerInvariantError("equity identity violated")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _append_event(
        self,
        event_time: datetime,
        event_type: str,
        *,
        symbol: str,
        cash_delta: Decimal = Decimal("0"),
        reserved_delta: Decimal = Decimal("0"),
        quantity_delta: int = 0,
        cost_delta: Decimal = Decimal("0"),
        realized_pnl_delta: Decimal = Decimal("0"),
        fees_delta: Decimal = Decimal("0"),
        order_id: str | None = None,
        fill_id: str | None = None,
        reason: str = "",
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> LedgerEvent:
        self._sequence += 1
        event = LedgerEvent(
            sequence=self._sequence,
            event_time=event_time,
            event_type=event_type,
            symbol=symbol,
            cash_delta=money(cash_delta),
            reserved_delta=money(reserved_delta),
            quantity_delta=quantity_delta,
            cost_delta=money(cost_delta),
            realized_pnl_delta=money(realized_pnl_delta),
            fees_delta=money(fees_delta),
            order_id=order_id,
            fill_id=fill_id,
            reason=reason,
            payload=payload or {},
            idempotency_key=idempotency_key
            or f"{self.run_id}:{self._sequence}:{event_type}:{symbol}",
        )
        self.events.append(event)
        return event

    def _pro_rata_reserved(self, order: Order, quantity: int, remaining_before: int) -> Decimal:
        if remaining_before <= 0:
            return Decimal("0")
        if quantity >= remaining_before:
            return order.reserved_cash
        return money(order.reserved_cash * Decimal(quantity) / Decimal(remaining_before))

    def _touch_fee_order(self, session: date, order: Order) -> None:
        self._fee_orders_by_session.setdefault(session, set()).add(order.order_id)

    def _update_order_after_fill(self, order: Order, quantity: int, price: Decimal) -> None:
        previous_value = order.avg_fill_price * Decimal(order.filled_quantity)
        order.filled_quantity += quantity
        order.remaining -= quantity
        order.avg_fill_price = (
            (previous_value + price * Decimal(quantity)) / Decimal(order.filled_quantity)
            if order.filled_quantity
            else Decimal("0")
        )
        order.status = (
            OrderStatus.FILLED if order.remaining <= 0 else OrderStatus.PARTIALLY_FILLED
        )

    def _complete_order(self, order: Order, event_time: datetime, *, reason: str) -> None:
        if order.reserved_cash > 0:
            self.release_order_cash(order, event_time=event_time, reason=reason)
        if order.reserved_quantity > 0:
            self.release_order_quantity(order, event_time=event_time, reason=reason)

    def _new_fill(
        self,
        order: Order,
        *,
        quantity: int,
        price: Decimal,
        gross: Decimal,
        fees: FeeDelta,
        slippage_cost: Decimal,
        bar_end_at: datetime,
        session: date,
        quality: str,
        reason: str,
    ) -> Fill:
        seed = f"{order.order_id}|{bar_end_at.isoformat()}|{quantity}|{price}|{self._sequence + 1}"
        fill_id = "fill-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            run_id=self.run_id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            gross_amount=gross,
            commission=fees.commission,
            transfer_fee=fees.transfer_fee,
            stamp_duty=fees.stamp_duty,
            slippage_cost=money(slippage_cost),
            bar_end_at=bar_end_at,
            session=session,
            quality=quality,
            reason=reason,
            event_seq=self._sequence + 1,
        )

    def _create_lot(self, order: Order, fill: Fill, *, session: date, total_cost: Decimal) -> None:
        available = self.next_session(session)
        lot = PositionLot(
            lot_id=f"lot-{fill.fill_id[-16:]}",
            symbol=order.symbol,
            acquired_session=session,
            available_session=available,
            quantity=fill.quantity,
            remaining=fill.quantity,
            cost_total=total_cost,
            cost_per_share=(total_cost / Decimal(fill.quantity)) if fill.quantity else Decimal("0"),
            source_order_id=order.order_id,
            source_fill_id=fill.fill_id,
        )
        self.lots.setdefault(order.symbol, []).append(lot)
        position = self.get_position(order.symbol)
        position.quantity += fill.quantity
        position.cost_total = money(position.cost_total + total_cost)
        position.market_price = fill.price
        position.market_price_session = session
        position.stale = False

    def _record_closed_trades(
        self,
        order: Order,
        consumed: list[tuple[PositionLot, int, Decimal]],
        *,
        net_proceeds: Decimal,
        exit_price: Decimal,
        exit_session: date,
        exit_reason: str,
    ) -> None:
        total_cost = sum((item[2] for item in consumed), Decimal("0"))
        allocated = Decimal("0")
        for index, (lot, take, cost) in enumerate(consumed):
            if index == len(consumed) - 1:
                trade_net = money(net_proceeds - allocated)
            elif total_cost > 0:
                trade_net = money(net_proceeds * cost / total_cost)
            else:
                trade_net = Decimal("0")
            allocated = money(allocated + trade_net)
            realized = money(trade_net - cost)
            seed = f"{lot.source_fill_id}|{order.order_id}|{take}|{exit_session.isoformat()}"
            trade = ClosedTrade(
                trade_id="trade-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20],
                symbol=order.symbol,
                entry_order_id=lot.source_order_id,
                exit_order_id=order.order_id,
                quantity=take,
                entry_price=lot.cost_per_share,
                exit_price=exit_price,
                cost_basis=cost,
                net_proceeds=trade_net,
                realized_pnl=realized,
                entry_session=lot.acquired_session,
                exit_session=exit_session,
                exit_reason=exit_reason,
                holding_days=self.count_trading_days(lot.acquired_session, exit_session),
            )
            self.closed_trades.append(trade)

    def _consume_lots(self, symbol: str, quantity: int, *, session: date) -> list[tuple[PositionLot, int, Decimal]]:
        remaining = quantity
        consumed: list[tuple[PositionLot, int, Decimal]] = []
        lots = self.lots.get(symbol, [])
        for lot in lots:
            if remaining <= 0:
                break
            if lot.remaining <= 0 or lot.available_session > session:
                continue
            take = min(lot.remaining, remaining)
            if take == lot.remaining:
                cost = lot.cost_total
                lot.cost_total = Decimal("0")
                lot.remaining = 0
            else:
                cost = (lot.cost_total * Decimal(take) / Decimal(lot.remaining)).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
                lot.cost_total = (lot.cost_total - cost).quantize(Decimal("0.000001"))
                lot.remaining -= take
            consumed.append((lot, take, cost))
            remaining -= take
        if remaining > 0:
            raise LedgerInvariantError(f"not enough sellable lots for {symbol}: {remaining} left")
        position = self.get_position(symbol)
        position.quantity -= quantity
        position.cost_total = money(position.cost_total - sum((item[2] for item in consumed), Decimal("0")))
        if position.quantity <= 0:
            position.quantity = 0
            position.cost_total = Decimal("0")
            position.market_price = None
        return consumed

    def fee_accumulator_for(self, order: Order, session: date) -> FeeAccumulator:
        accumulator = getattr(order, "fee_accumulator", None)
        if accumulator is None or accumulator.trade_date != session:
            order.fee_accumulator = FeeAccumulator(
                rule=self.fee_policy.rule_for(session),
                side=order.side,
                trade_date=session,
            )
        return order.fee_accumulator


def compute_turnover(fills: Iterable[Fill]) -> Decimal:
    return money(sum((fill.gross_amount for fill in fills), Decimal("0")))


__all__ = [
    "Ledger",
    "LedgerError",
    "LedgerInvariantError",
    "compute_turnover",
]
