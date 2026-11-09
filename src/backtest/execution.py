# -*- coding: utf-8 -*-
"""Deterministic multi-asset execution kernel for recorded plan replay.

The engine advances one shared logical clock.  It never calls a provider,
never calls an LLM, never reads the wall clock and never touches Portfolio.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Protocol

from src.agent.pricing.confirmation import evaluate_confirmation_bar
from src.agent.pricing.params import get_price_plan_params
from src.agent.pricing.schemas import ZoneScenario

from .calendar import CnTradingCalendar
from .entities import Bar, DailyBar, FillCandidate, Order, candidate_slippage
from .fees import estimate_max_buy_fees
from .ledger import Ledger
from .rules import MarketRule
from .schemas import (
    ExitReason,
    ExperimentConfig,
    FeePolicy,
    OrderSide,
    OrderType,
    ReplayPlan,
    risk_policy_for,
)


class BacktestCancelled(RuntimeError):
    """Raised when a cooperative cancel request is observed."""


class MarketDataView(Protocol):
    granularity: str
    quality_flags: list[str]

    def bars_at(self, timestamp: datetime) -> dict[str, Bar]: ...

    def bar_for(self, symbol: str, timestamp: datetime) -> Bar | None: ...

    def daily_bars(self, symbol: str, up_to_session: date) -> list[DailyBar]: ...

    def previous_close(self, symbol: str, session: date) -> Decimal | None: ...

    def last_price(self, symbol: str, session: date) -> tuple[Decimal | None, bool]: ...

    def rule(self, symbol: str, session: date) -> MarketRule: ...

    def corporate_actions(self, session: date) -> list[Any]: ...

    def benchmark_close(self, session: date) -> Decimal | None: ...


@dataclass
class PlanState:
    plan: ReplayPlan
    status: str = "pending"
    accepted: bool = False
    confirmed_session: date | None = None
    activation_session: date | None = None
    entry_order_id: str | None = None
    entry_filled_quantity: int = 0
    entry_avg_price: Decimal = Decimal("0")
    entry_session: date | None = None
    target_1_done: bool = False
    target_2_done: bool = False
    stop_triggered: bool = False
    timeout_triggered: bool = False
    requested_position_pct: Decimal = Decimal("0")
    approved_position_pct: Decimal = Decimal("0")
    cap_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ExitInstruction:
    symbol: str
    plan_key: str
    stop_loss: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    target_1_exit_ratio: Decimal
    entry_session: date
    exit_active_from: date
    max_hold_trading_days: int
    target_1_done: bool = False
    target_2_done: bool = False
    stop_triggered: bool = False
    timeout_triggered: bool = False
    stop_order_id: str | None = None
    target_order_id: str | None = None
    timeout_order_id: str | None = None
    entry_quantity: int = 0


@dataclass
class ExecutionResult:
    orders: list[Order]
    fills: list[Any]
    ledger: Ledger
    plan_states: dict[str, PlanState]
    diagnostics: dict[str, Any]
    quality_flags: list[str]
    status: str = "succeeded"
    last_clock: datetime | None = None


class ExecutionEngine:
    def __init__(
        self,
        *,
        run_id: str,
        config: ExperimentConfig,
        plans: list[ReplayPlan],
        data: MarketDataView,
        calendar: CnTradingCalendar,
        fee_policy: FeePolicy,
        progress_callback: Callable[[int, int, str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
        plan_provider: Callable[[date, dict[str, Any]], list[ReplayPlan]] | None = None,
        decision_dates: Iterable[date] | None = None,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.plans = sorted(plans, key=lambda item: (item.available_at, item.rank, item.plan_key))
        self.data = data
        self.calendar = calendar
        self.fee_policy = fee_policy
        self.progress_callback = progress_callback
        self.cancel_callback = cancel_callback
        self.plan_provider = plan_provider
        self._decision_dates = set(decision_dates or [])
        self._provider_called: set[date] = set()
        self.ledger = Ledger(
            run_id=run_id,
            initial_cash=config.initial_cash,
            fee_policy=fee_policy,
            next_session=calendar.next_session,
            calendar_version=calendar.version,
            count_trading_days=lambda start, end: max(
                0, len(calendar.sessions(start, end)) - 1
            ),
        )
        self.orders: list[Order] = []
        self.fills: list[Any] = []
        self.plan_states: dict[str, PlanState] = {
            plan.plan_key: PlanState(plan=plan) for plan in self.plans
        }
        self._orders_by_id: dict[str, Order] = {}
        self._order_seq = 0
        self._exit_instructions: dict[str, ExitInstruction] = {}
        self._ambiguous_fills: list[dict[str, Any]] = []
        self._rejections: list[dict[str, Any]] = []
        self._last_clock: datetime | None = None
        self._sessions: list[date] = []
        self._current_session: date | None = None
        self._processed_sessions = 0
        self._stale_sessions = 0
        self._missing_bar_days = 0
        self._downgraded_days = 0
        self._fill_count_by_plan: dict[str, int] = {}
        self._quality_flags: set[str] = set(self.data.quality_flags)
        self._rules_unverified = False
        self._benchmark_base: Decimal | None = None
        self._cancel_check_counter = 0
        self.risk_policy = config.risk_policy or risk_policy_for(config.risk_profile)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> ExecutionResult:
        self._sessions = self.calendar.sessions(self.config.start_date, self.config.end_date)
        if not self._sessions:
            raise ValueError("experiment range contains no trading sessions")
        total = len(self._sessions)
        for index, session in enumerate(self._sessions):
            self._check_cancel()
            self._current_session = session
            self._last_clock = self.calendar.session_open(session)
            self._apply_corporate_actions(session)
            self._call_plan_provider(session)
            self._accept_available_plans(self.calendar.session_open(session), session)
            bar_times = self.calendar.bar_times(session, self.config.granularity.value)
            for bar_time in bar_times:
                self._check_cancel()
                self._last_clock = bar_time
                bars = self.data.bars_at(bar_time)
                if not bars:
                    continue
                if self.config.granularity.value == "5m" and any(
                    bar.frequency == "1d" for bar in bars.values()
                ):
                    self._downgraded_days += 1
                    self._quality_flags.add("mixed_granularity")
                self._prepare_exit_orders(bars, bar_time, session)
                self._match_orders(bars, bar_time, session)
                self._accept_available_plans(bar_time, session)
            self._end_of_session(session)
            self._processed_sessions = index + 1
            if self.progress_callback:
                self.progress_callback(index + 1, total, "running")
        self._finalize_end_policy()
        diagnostics = self._build_diagnostics()
        status = "partial" if self._is_partial() else "succeeded"
        return ExecutionResult(
            orders=self.orders,
            fills=self.fills,
            ledger=self.ledger,
            plan_states=self.plan_states,
            diagnostics=diagnostics,
            quality_flags=sorted(self._quality_flags),
            status=status,
            last_clock=self._last_clock,
        )

    def cancel(self) -> ExecutionResult:
        event_time = self._last_clock or datetime.combine(self.config.start_date, datetime.min.time())
        for order in self.orders:
            if order.is_open:
                self.ledger.cancel_order(order, event_time=event_time, reason="run_cancelled")
        diagnostics = self._build_diagnostics()
        return ExecutionResult(
            orders=self.orders,
            fills=self.fills,
            ledger=self.ledger,
            plan_states=self.plan_states,
            diagnostics=diagnostics,
            quality_flags=sorted(self._quality_flags | {"cancelled"}),
            status="cancelled",
            last_clock=self._last_clock,
        )

    def _call_plan_provider(self, session: date) -> None:
        if self.plan_provider is None or session not in self._decision_dates:
            return
        if session in self._provider_called:
            return
        self._provider_called.add(session)
        new_plans = self.plan_provider(session, self._portfolio_snapshot(session)) or []
        known = {plan.plan_key for plan in self.plans}
        for plan in new_plans:
            if plan.plan_key in known:
                continue
            self.plans.append(plan)
            self.plan_states[plan.plan_key] = PlanState(plan=plan)
            known.add(plan.plan_key)
            self._quality_flags.update(plan.quality_flags)
        self.plans.sort(key=lambda item: (item.available_at, item.rank, item.plan_key))

    def _portfolio_snapshot(self, session: date) -> dict[str, Any]:
        equity = self.ledger.total_equity
        market_value = self.ledger.total_market_value
        positions = []
        for symbol in sorted(self.ledger.positions):
            position = self.ledger.positions[symbol]
            if position.quantity <= 0:
                continue
            price = position.market_price
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": position.quantity,
                    "sellable_quantity": self.ledger.available_sellable_quantity(symbol, session),
                    "average_cost": str(position.avg_cost),
                    "market_price": str(price) if price is not None else None,
                    "market_value": str(position.market_value),
                    "stale": position.stale,
                }
            )
        return {
            "session": session.isoformat(),
            "cash": str(self.ledger.cash),
            "reserved_cash": str(self.ledger.reserved_cash),
            "available_cash": str(self.ledger.available_cash),
            "market_value": str(market_value),
            "equity": str(equity),
            "exposure_pct": float(market_value / equity * Decimal("100")) if equity > 0 else 0.0,
            "positions": positions,
            "risk_policy": self.config.risk_policy.model_dump(mode="json") if self.config.risk_policy else {},
        }

    # ------------------------------------------------------------------
    # Plan acceptance / confirmation
    # ------------------------------------------------------------------
    def _accept_available_plans(self, clock: datetime, session: date) -> None:
        for plan in self.plans:
            state = self.plan_states[plan.plan_key]
            if state.accepted:
                continue
            if plan.available_at > clock:
                continue
            activation = max(plan.valid_from, self.calendar.first_session_after_datetime(plan.available_at))
            if activation > self.config.end_date:
                state.status = "outside_window"
                state.accepted = True
                continue
            if activation > plan.valid_until:
                state.status = "not_triggered"
                state.accepted = True
                continue
            state.activation_session = activation
            if activation > session:
                continue
            if self._position_exists(plan.symbol):
                state.status = "already_position"
                state.accepted = True
                state.notes.append("position_already_open")
                continue
            if plan.requires_confirmation:
                state.status = "waiting_confirmation"
                state.accepted = True
                continue
            if self._supersede_competing_entry(plan, clock, session):
                state.status = "superseded"
                state.accepted = True
                continue
            state.accepted = True
            self._create_entry_order(plan, state, clock=clock, session=session)
        self._accept_confirmations(session, at_close=False)

    def _accept_confirmations(self, session: date, *, at_close: bool) -> None:
        if not at_close:
            return
        for plan in self.plans:
            state = self.plan_states[plan.plan_key]
            if not state.accepted or not plan.requires_confirmation:
                continue
            if state.status not in {"waiting_confirmation"}:
                continue
            if state.activation_session is None or session < state.activation_session:
                continue
            if session > (plan.confirmation_deadline or plan.valid_until):
                state.status = "not_triggered"
                state.notes.append("confirmation_deadline_passed")
                continue
            daily = self.data.daily_bars(plan.symbol, session)
            if not daily or daily[-1].session != session:
                self._missing_bar_days += 1
                state.notes.append(f"missing_daily_bar:{session.isoformat()}")
                continue
            if self._position_exists(plan.symbol):
                state.status = "already_position"
                continue
            outcome = _evaluate_confirmation(plan, daily, session)
            if outcome == "invalidated":
                state.status = "invalidated"
                state.notes.append("confirmation_invalidated")
            elif outcome == "ambiguous":
                state.status = "ambiguous"
                state.notes.append("confirmation_ambiguous")
            elif outcome == "confirmed":
                state.confirmed_session = session
                state.status = "confirmed"
                if self._supersede_competing_entry(plan, self.calendar.session_close(session), session):
                    state.status = "superseded"
                    continue
                next_session = self.calendar.next_session(session)
                self._create_entry_order(
                    plan,
                    state,
                    clock=self.calendar.session_close(session),
                    session=next_session,
                    valid_session=next_session,
                )

    def _supersede_competing_entry(self, plan: ReplayPlan, clock: datetime, session: date) -> bool:
        for other in self.plans:
            if other.plan_key == plan.plan_key or other.symbol != plan.symbol:
                continue
            other_state = self.plan_states[other.plan_key]
            if other_state.status == "already_position":
                return True
            if other_state.entry_order_id:
                order = self._orders_by_id.get(other_state.entry_order_id)
                if order and order.is_open:
                    self.ledger.cancel_order(order, event_time=clock, reason="superseded_by_new_plan")
                    other_state.status = "superseded"
                    other_state.notes.append("cancelled_unfilled_entry")
            elif other_state.status in {"waiting_confirmation", "confirmed", "pending"}:
                if other.available_at <= plan.available_at:
                    other_state.status = "superseded"
                    other_state.notes.append("superseded_pending_plan")
        return False

    def _create_entry_order(
        self,
        plan: ReplayPlan,
        state: PlanState,
        *,
        clock: datetime,
        session: date,
        valid_session: date | None = None,
    ) -> None:
        policy = self.risk_policy
        if policy.require_stop_loss and plan.stop_loss is None:
            state.status = "missing_stop_loss"
            state.cap_reason = "missing_stop_loss"
            self._rejections.append({"plan_key": plan.plan_key, "reason": "missing_stop_loss"})
            return
        if self._active_position_count() >= policy.max_concurrent_positions:
            state.status = "max_positions_reached"
            state.cap_reason = "max_concurrent_positions"
            self._rejections.append({"plan_key": plan.plan_key, "reason": "max_positions_reached"})
            return
        rule = self.data.rule(plan.symbol, session)
        if not rule.supported:
            state.status = "unsupported"
            state.notes.append("unsupported_market_rule")
            self._rejections.append({"plan_key": plan.plan_key, "reason": "unsupported_market_rule"})
            return
        if rule.suspended:
            state.status = "suspended"
            state.notes.append("suspended_on_activation")
            return
        self._rules_unverified = self._rules_unverified or not rule.verified
        limit_price = plan.entry_high
        if rule.limit_up is not None:
            limit_price = min(limit_price, rule.limit_up)
        if limit_price < plan.entry_low:
            state.status = "not_triggered"
            state.notes.append("entry_zone_above_limit_up")
            return
        equity = self.ledger.total_equity
        requested_pct = plan.target_position_pct
        effective_single_cap = min(policy.max_single_position_pct, self.config.max_single_position_pct)
        target_pct = min(requested_pct, effective_single_cap)
        cap_reason = "plan_target" if target_pct == plan.target_position_pct else "single_position_cap"
        risk_cap_pct = None
        if plan.stop_loss is not None and plan.entry_high > plan.stop_loss:
            stop_distance = (plan.entry_high - plan.stop_loss) / plan.entry_high
            if stop_distance > 0:
                risk_notional = equity * policy.risk_per_trade_pct / Decimal("100") / stop_distance
                risk_cap_pct = risk_notional / equity * Decimal("100") if equity > 0 else Decimal("0")
                if risk_cap_pct < target_pct:
                    target_pct = risk_cap_pct
                    cap_reason = "risk_per_trade"
        target_notional = equity * target_pct / Decimal("100")
        exposure_cap = self._exposure_capacity(equity)
        available = self.ledger.available_cash
        budget = min(target_notional, exposure_cap, available)
        if budget <= 0:
            state.status = "insufficient_cash"
            state.cap_reason = "exposure_or_cash_cap"
            self._rejections.append({"plan_key": plan.plan_key, "reason": "insufficient_cash"})
            return
        state.requested_position_pct = requested_pct
        state.approved_position_pct = min(target_pct, budget / equity * Decimal("100")) if equity > 0 else Decimal("0")
        state.cap_reason = cap_reason
        lot = rule.buy_lot_size
        quantity = self._size_buy_order(
            budget=budget,
            limit_price=limit_price,
            lot=lot,
            valid_days=plan.order_valid_trading_days,
            session=session,
        )
        if quantity <= 0:
            state.status = "insufficient_cash"
            self._rejections.append({"plan_key": plan.plan_key, "reason": "below_one_lot"})
            return
        fee_buffer = estimate_max_buy_fees(
            self.fee_policy.rule_for(session),
            limit_price * Decimal(quantity),
            plan.order_valid_trading_days,
        )
        reservation = _money(limit_price * Decimal(quantity) + fee_buffer)
        while reservation > self.ledger.available_cash and quantity > lot:
            quantity -= lot
            reservation = _money(
                limit_price * Decimal(quantity)
                + estimate_max_buy_fees(
                    self.fee_policy.rule_for(session),
                    limit_price * Decimal(quantity),
                    plan.order_valid_trading_days,
                )
            )
        if quantity <= 0 or reservation > self.ledger.available_cash:
            state.status = "insufficient_cash"
            self._rejections.append({"plan_key": plan.plan_key, "reason": "insufficient_cash_after_fees"})
            return
        active_from = self._entry_active_from(clock, valid_session or session)
        order = self._new_order(
            plan=plan,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
            entry_floor=plan.entry_low,
            active_from=active_from,
            expires_on_session=self._add_sessions(valid_session or session, plan.order_valid_trading_days - 1),
            priority_rank=plan.rank,
        )
        try:
            self.ledger.reserve_cash(order, reservation, event_time=clock)
        except Exception as exc:
            state.status = "insufficient_cash"
            self._rejections.append({"plan_key": plan.plan_key, "reason": str(exc)})
            return
        state.entry_order_id = order.order_id
        state.status = "order_active"
        self.ledger.register_order(order)

    def _position_exists(self, symbol: str) -> bool:
        position = self.ledger.positions.get(symbol)
        return position is not None and position.quantity > 0

    def _exposure_capacity(self, equity: Decimal) -> Decimal:
        effective_pct = min(
            self.risk_policy.effective_total_exposure_pct,
            self.config.max_total_exposure_pct,
        )
        total_cap = equity * effective_pct / Decimal("100")
        current = self.ledger.total_market_value
        pending = sum(
            (
                order.limit_price * Decimal(order.remaining)
                for order in self.orders
                if order.is_open and order.side is OrderSide.BUY and order.limit_price is not None
            ),
            Decimal("0"),
        )
        return max(Decimal("0"), total_cap - current - pending)

    def _active_position_count(self) -> int:
        symbols = {
            symbol
            for symbol, position in self.ledger.positions.items()
            if position.quantity > 0
        }
        symbols.update(
            order.symbol
            for order in self.orders
            if order.is_open and order.side is OrderSide.BUY
        )
        return len(symbols)

    def _size_buy_order(
        self,
        *,
        budget: Decimal,
        limit_price: Decimal,
        lot: int,
        valid_days: int,
        session: date,
    ) -> int:
        if limit_price <= 0 or lot <= 0:
            return 0
        fee_rate = self.fee_policy.rule_for(session)
        approximate_fee = limit_price * Decimal(lot) * (
            fee_rate.commission_bps + fee_rate.transfer_fee_bps + fee_rate.stamp_duty_buy_bps
        ) / Decimal("10000")
        per_lot = limit_price * Decimal(lot) + max(approximate_fee, fee_rate.minimum_commission)
        lots = int((budget / per_lot).to_integral_value(rounding=ROUND_FLOOR))
        return max(0, lots * lot)

    # ------------------------------------------------------------------
    # Existing order matching
    # ------------------------------------------------------------------
    def _prepare_exit_orders(self, bars: dict[str, Bar], clock: datetime, session: date) -> None:
        for symbol in sorted(bars):
            instruction = self._exit_instructions.get(symbol)
            if instruction is None:
                continue
            position = self.ledger.positions.get(symbol)
            if position is None or position.quantity <= 0:
                self._exit_instructions.pop(symbol, None)
                continue
            if session < instruction.exit_active_from:
                continue
            bar = bars[symbol]
            if _is_suspended(bar):
                continue
            stop_order = self._orders_by_id.get(instruction.stop_order_id or "")
            target_order = self._orders_by_id.get(instruction.target_order_id or "")
            timeout_order = self._orders_by_id.get(instruction.timeout_order_id or "")
            stop_trigger = (
                not instruction.stop_triggered
                and instruction.stop_loss is not None
                and (bar.open <= instruction.stop_loss or bar.low <= instruction.stop_loss)
                and (stop_order is None or not stop_order.is_open)
            )
            target_trigger, target_price, target_kind = self._target_trigger(instruction, bar)
            if stop_trigger:
                for other in (target_order, timeout_order):
                    if other is not None and other.is_open:
                        self.ledger.cancel_order(other, event_time=clock, reason="oco_stop_first")
                if target_trigger:
                    self._ambiguous_fills.append(
                        {
                            "symbol": symbol,
                            "session": session.isoformat(),
                            "bar_end_at": clock.isoformat(),
                            "reason": "stop_and_target_same_bar",
                            "stop_price": str(instruction.stop_loss),
                            "target_price": str(target_price),
                            "policy": "stop_first",
                        }
                    )
                instruction.stop_triggered = True
                if stop_order is None or not stop_order.is_open:
                    self._create_exit_order(
                        symbol=symbol,
                        plan_key=instruction.plan_key,
                        side=OrderSide.SELL,
                        order_type=OrderType.STOP_MARKET,
                        quantity=position.quantity,
                        price=instruction.stop_loss,
                        clock=clock,
                        session=session,
                        exit_reason=ExitReason.STOP_LOSS.value,
                    )
                continue
            if instruction.stop_triggered:
                continue
            if any(order is not None and order.is_open for order in (stop_order, target_order, timeout_order)):
                continue
            if target_trigger and target_kind:
                quantity = instruction.target_1_exit_ratio
                if target_kind == "target_1":
                    qty = int(
                        (Decimal(position.quantity) * quantity).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    if qty <= 0:
                        qty = position.quantity
                else:
                    qty = position.quantity
                self._create_exit_order(
                    symbol=symbol,
                    plan_key=instruction.plan_key,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=max(0, min(qty, position.quantity)),
                    price=target_price,
                    clock=clock,
                    session=session,
                    exit_reason=(
                        ExitReason.TAKE_PROFIT_1.value
                        if target_kind == "target_1"
                        else ExitReason.TAKE_PROFIT_2.value
                    ),
                )

    def _target_trigger(
        self, instruction: ExitInstruction, bar: Bar
    ) -> tuple[bool, Decimal | None, str | None]:
        if instruction.target_1 is None:
            instruction.target_1_done = True
        if not instruction.target_1_done and instruction.target_1 is not None:
            if bar.open >= instruction.target_1 or bar.high >= instruction.target_1:
                return True, instruction.target_1, "target_1"
            return False, None, None
        if not instruction.target_2_done and instruction.target_2 is not None:
            if bar.open >= instruction.target_2 or bar.high >= instruction.target_2:
                return True, instruction.target_2, "target_2"
        return False, None, None

    def _match_orders(self, bars: dict[str, Bar], clock: datetime, session: date) -> None:
        open_orders = [
            order
            for order in self.orders
            if order.is_open
            and order.active_from is not None
            and order.active_from <= clock
            and (order.expires_on_session is None or order.expires_on_session >= session)
        ]
        sells = [order for order in open_orders if order.side is OrderSide.SELL]
        buys = [order for order in open_orders if order.side is OrderSide.BUY]
        sells.sort(key=lambda order: (0 if order.order_type is OrderType.STOP_MARKET else 1, order.priority_key))
        buys.sort(key=lambda order: order.priority_key)
        capacities: dict[str, int] = {}
        for symbol, bar in bars.items():
            capacities[symbol] = max(0, int(bar.volume * self.config.participation_rate))
        for order in sells:
            bar = bars.get(order.symbol)
            if bar is None:
                continue
            self._match_one(order, bar, clock, session, capacities, is_buy=False)
        for order in buys:
            bar = bars.get(order.symbol)
            if bar is None:
                continue
            self._match_one(order, bar, clock, session, capacities, is_buy=True)

    def _match_one(
        self,
        order: Order,
        bar: Bar,
        clock: datetime,
        session: date,
        capacities: dict[str, int],
        *,
        is_buy: bool,
    ) -> None:
        if capacities.get(order.symbol, 0) <= 0:
            return
        rule = self.data.rule(order.symbol, session)
        self._rules_unverified = self._rules_unverified or not rule.verified
        candidate = _match_order(
            order,
            bar,
            rule=rule,
            capacity=capacities[order.symbol],
            slippage_bps=self.config.slippage_bps,
            is_buy=is_buy,
        )
        if candidate is None or candidate.fill_quantity <= 0:
            return
        if is_buy:
            candidate = self._apply_buy_cap(order, candidate, rule, session)
        else:
            candidate = self._apply_sell_cap(order, candidate, session)
        if candidate is not None and candidate.fill_quantity < order.remaining:
            candidate = FillCandidate(
                candidate.fill_quantity,
                candidate.price,
                "partial_" + candidate.quality,
                candidate.reason,
                candidate.reference_price,
            )
        if candidate is None or candidate.fill_quantity <= 0:
            return
        fee_accumulator = self.ledger.fee_accumulator_for(order, session)
        if is_buy:
            fill = self.ledger.apply_buy_fill(
                order,
                quantity=candidate.fill_quantity,
                price=candidate.price,
                bar_end_at=clock,
                session=session,
                slippage_cost=candidate_slippage(candidate),
                quality=candidate.quality,
                reason=candidate.reason,
                fee_accumulator=fee_accumulator,
            )
            self._on_entry_fill(order, fill, session, candidate.price)
        else:
            fill = self.ledger.apply_sell_fill(
                order,
                quantity=candidate.fill_quantity,
                price=candidate.price,
                bar_end_at=clock,
                session=session,
                slippage_cost=candidate_slippage(candidate),
                quality=candidate.quality,
                reason=candidate.reason,
                fee_accumulator=fee_accumulator,
                exit_reason=order.exit_reason or candidate.reason,
            )
            self._on_exit_fill(order, fill, session)
        self.fills.append(fill)
        capacities[order.symbol] = max(0, capacities.get(order.symbol, 0) - candidate.fill_quantity)
        if order.is_open is False:
            self.ledger.release_order_cash(order, event_time=clock, reason="filled")
            self.ledger.release_order_quantity(order, event_time=clock, reason="filled")

    def _apply_buy_cap(
        self, order: Order, candidate: FillCandidate, rule: MarketRule, session: date
    ) -> FillCandidate | None:
        qty = candidate.fill_quantity
        lot = max(1, rule.buy_lot_size)
        if order.remaining >= lot:
            qty = (qty // lot) * lot
        if qty <= 0:
            return None
        if candidate.price > (order.limit_price or Decimal("Infinity")):
            return None
        return FillCandidate(qty, candidate.price, candidate.quality, candidate.reason, candidate.reference_price)

    def _apply_sell_cap(self, order: Order, candidate: FillCandidate, session: date) -> FillCandidate | None:
        qty = min(candidate.fill_quantity, order.remaining)
        if qty <= 0:
            return None
        return FillCandidate(qty, candidate.price, candidate.quality, candidate.reason, candidate.reference_price)

    def _on_entry_fill(self, order: Order, fill: Any, session: date, price: Decimal) -> None:
        state = self.plan_states.get(order.plan_key)
        if state is None:
            return
        state.entry_filled_quantity += fill.quantity
        state.entry_avg_price = order.avg_fill_price
        state.entry_session = state.entry_session or session
        state.status = "filled" if not order.is_open else "partially_filled"
        self._fill_count_by_plan[order.plan_key] = self._fill_count_by_plan.get(order.plan_key, 0) + 1
        instruction = self._exit_instructions.get(order.symbol)
        if instruction is None:
            instruction = ExitInstruction(
                symbol=order.symbol,
                plan_key=order.plan_key,
                stop_loss=state.plan.stop_loss,
                target_1=state.plan.target_1,
                target_2=state.plan.target_2,
                target_1_exit_ratio=state.plan.target_1_exit_ratio,
                entry_session=session,
                exit_active_from=self.calendar.next_session(session),
                max_hold_trading_days=state.plan.max_hold_trading_days,
                entry_quantity=fill.quantity,
            )
            self._exit_instructions[order.symbol] = instruction
        else:
            instruction.entry_quantity += fill.quantity
        if state.plan.stop_loss is None:
            self._quality_flags.add("plan_without_stop_loss")

    def _on_exit_fill(self, order: Order, fill: Any, session: date) -> None:
        instruction = self._exit_instructions.get(order.symbol)
        if instruction is None:
            return
        position = self.ledger.positions.get(order.symbol)
        position_quantity = position.quantity if position else 0
        if order.exit_reason == ExitReason.STOP_LOSS.value:
            instruction.stop_triggered = True
            self._cancel_target_orders(order.symbol, order.order_id, session)
        elif order.exit_reason == ExitReason.TAKE_PROFIT_1.value:
            if not order.is_open:
                instruction.target_1_done = True
                instruction.target_order_id = None
                if instruction.target_2 is not None and position_quantity > 0:
                    instruction.target_2_done = False
        elif order.exit_reason == ExitReason.TAKE_PROFIT_2.value:
            if not order.is_open:
                instruction.target_2_done = True
                instruction.target_order_id = None
        if position_quantity <= 0:
            state = self.plan_states.get(instruction.plan_key)
            if state is not None and state.status not in {"stopped", "closed"}:
                state.status = "closed"
            self._exit_instructions.pop(order.symbol, None)

    def _cancel_target_orders(self, symbol: str, keep_order_id: str, session: date) -> None:
        instruction = self._exit_instructions.get(symbol)
        if instruction is None:
            return
        if instruction.target_order_id and instruction.target_order_id != keep_order_id:
            order = self._orders_by_id.get(instruction.target_order_id)
            if order and order.is_open:
                self.ledger.cancel_order(
                    order,
                    event_time=self._last_clock or self.calendar.session_close(session),
                    reason="oco_stop_filled",
                )
            instruction.target_order_id = None

    def _create_exit_order(
        self,
        *,
        symbol: str,
        plan_key: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Decimal | None,
        clock: datetime,
        session: date,
        exit_reason: str,
    ) -> Order | None:
        if quantity <= 0:
            return None
        plan = self._plan_for_key(plan_key)
        order = self._new_order(
            plan=plan,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=price if order_type is OrderType.LIMIT else None,
            stop_price=price if order_type is OrderType.STOP_MARKET else None,
            active_from=clock,
            expires_on_session=None,
            priority_rank=1,
            pre_registered_exit=True,
            exit_reason=exit_reason,
        )
        try:
            self.ledger.reserve_sell_quantity(order, event_time=clock, session=session)
        except Exception:
            return None
        instruction = self._exit_instructions.get(symbol)
        if instruction is not None:
            if exit_reason == ExitReason.STOP_LOSS.value:
                instruction.stop_order_id = order.order_id
            elif exit_reason == ExitReason.TAKE_PROFIT_1.value:
                instruction.target_order_id = order.order_id
            elif exit_reason == ExitReason.TAKE_PROFIT_2.value:
                instruction.target_order_id = order.order_id
        self.ledger.register_order(order)
        return order

    # ------------------------------------------------------------------
    # End of session
    # ------------------------------------------------------------------
    def _end_of_session(self, session: date) -> None:
        self._accept_confirmations(session, at_close=True)
        clock = self.calendar.session_close(session)
        for order in self.orders:
            if not order.is_open:
                continue
            if order.expires_on_session is not None and order.expires_on_session <= session:
                self.ledger.expire_order(order, event_time=clock, reason="order_window_expired")
                state = self.plan_states.get(order.plan_key)
                if state is not None and order.side is OrderSide.BUY:
                    state.status = "not_triggered" if state.entry_filled_quantity == 0 else "partially_filled_expired"
        self._create_timeout_orders(session, clock)
        self.ledger.settle_fees(session, event_time=clock)
        stale = 0
        for symbol in sorted(self.ledger.positions):
            position = self.ledger.positions[symbol]
            if position.quantity <= 0:
                continue
            price, is_stale = self.data.last_price(symbol, session)
            self.ledger.mark_price(symbol, price, session, stale=is_stale)
            if is_stale:
                stale += 1
        if stale:
            self._stale_sessions += 1
            self._quality_flags.add("stale_valuation")
        point = self.ledger.close_session(
            session,
            clock,
            granularity=self.config.granularity.value,
            quality="partial" if stale else "complete",
        )
        benchmark = self.data.benchmark_close(session)
        if benchmark is not None and benchmark > 0:
            if self._benchmark_base is None:
                self._benchmark_base = benchmark
            normalized = self.config.initial_cash * benchmark / self._benchmark_base
            self.ledger.equity_points[-1] = replace(point, benchmark_value=normalized)

    def _create_timeout_orders(self, session: date, clock: datetime) -> None:
        for symbol in sorted(self._exit_instructions):
            instruction = self._exit_instructions[symbol]
            position = self.ledger.positions.get(symbol)
            if position is None or position.quantity <= 0:
                continue
            if instruction.timeout_triggered:
                continue
            holding = max(0, len(self.calendar.sessions(instruction.entry_session, session)) - 1)
            if holding < instruction.max_hold_trading_days:
                continue
            instruction.timeout_triggered = True
            order = self._create_exit_order(
                symbol=symbol,
                plan_key=instruction.plan_key,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
                price=None,
                clock=clock,
                session=session,
                exit_reason=ExitReason.MAX_HOLD.value,
            )
            if order is not None:
                instruction.timeout_order_id = order.order_id
                state = self.plan_states.get(instruction.plan_key)
                if state is not None:
                    state.timeout_triggered = True

    def _finalize_end_policy(self) -> None:
        last_session = self._sessions[-1] if self._sessions else self.config.end_date
        clock = self.calendar.session_close(last_session)
        if self.config.end_policy.value == "liquidate":
            for symbol in sorted(self.ledger.positions):
                position = self.ledger.positions[symbol]
                if position.quantity <= 0:
                    continue
                instruction = self._exit_instructions.get(symbol)
                if instruction is None:
                    continue
                sellable = self.ledger.available_sellable_quantity(symbol, last_session)
                if sellable <= 0:
                    self._quality_flags.add("liquidation_blocked")
                    continue
                order = self._create_exit_order(
                    symbol=symbol,
                    plan_key=instruction.plan_key,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=sellable,
                    price=None,
                    clock=clock,
                    session=last_session,
                    exit_reason=ExitReason.END_OF_WINDOW.value,
                )
                if order is not None:
                    bar = self.data.bar_for(symbol, clock)
                    if bar is not None:
                        capacities = {symbol: max(0, int(bar.volume * self.config.participation_rate))}
                        self._match_one(order, bar, clock, last_session, capacities, is_buy=False)
        for order in self.orders:
            if order.is_open:
                self.ledger.cancel_order(
                    order, event_time=clock, reason="end_of_experiment_cancel_open_orders"
                )
        self._exit_instructions.clear()

    # ------------------------------------------------------------------
    # Corporate actions / diagnostics
    # ------------------------------------------------------------------
    def _apply_corporate_actions(self, session: date) -> None:
        for action in self.data.corporate_actions(session):
            self.ledger.apply_corporate_action(action, event_time=self.calendar.session_open(session))

    def _build_diagnostics(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        for state in self.plan_states.values():
            status_counts[state.status] = status_counts.get(state.status, 0) + 1
        filled_plans = len([key for key, count in self._fill_count_by_plan.items() if count > 0])
        included = len(self.plans)
        open_positions = len([p for p in self.ledger.positions.values() if p.quantity > 0])
        order_status_counts: dict[str, int] = {}
        for order in self.orders:
            order_status_counts[order.status.value] = order_status_counts.get(order.status.value, 0) + 1
        return {
            "plans_included": included,
            "plan_status_counts": dict(sorted(status_counts.items())),
            "orders_created": len(self.orders),
            "order_status_counts": dict(sorted(order_status_counts.items())),
            "fills": len(self.fills),
            "filled_plans": filled_plans,
            "fill_rate_pct": (filled_plans / included * 100.0) if included else None,
            "open_positions": open_positions,
            "ambiguous_fills": len(self._ambiguous_fills),
            "ambiguous_fill_details": self._ambiguous_fills[:200],
            "rejections": self._rejections[:500],
            "not_triggered": status_counts.get("not_triggered", 0),
            "gate_blocked": 0,
            "insufficient_cash": status_counts.get("insufficient_cash", 0),
            "partial_fills": sum(1 for fill in self.fills if fill.quality.startswith("partial")),
            "stale_sessions": self._stale_sessions,
            "missing_bar_days": self._missing_bar_days,
            "downgraded_days": self._downgraded_days,
            "rules_unverified": self._rules_unverified,
            "quality_flags": sorted(self._quality_flags),
        }

    def _is_partial(self) -> bool:
        return bool(
            self._ambiguous_fills
            or self._stale_sessions
            or self._missing_bar_days
            or self._downgraded_days
            or self._rules_unverified
            or self.config.corporate_action_policy.value == "strict"
            and "corporate_action_assumed_none" in self._quality_flags
        )

    def _new_order(
        self,
        *,
        plan: ReplayPlan,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        limit_price: Decimal | None,
        stop_price: Decimal | None = None,
        entry_floor: Decimal | None = None,
        active_from: datetime,
        expires_on_session: date | None,
        priority_rank: int,
        pre_registered_exit: bool = False,
        exit_reason: str = "",
    ) -> Order:
        self._order_seq += 1
        seed = f"{self.run_id}|{plan.plan_key}|{side.value}|{order_type.value}|{self._order_seq}"
        order = Order(
            order_id="ord-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:22],
            run_id=self.run_id,
            plan_key=plan.plan_key,
            symbol=plan.symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            entry_floor=entry_floor,
            submitted_at=active_from,
            active_from=active_from,
            expires_on_session=expires_on_session,
            remaining=quantity,
            priority_rank=priority_rank,
            priority_available_at=plan.available_at,
            pre_registered_exit=pre_registered_exit,
            exit_reason=exit_reason,
            created_seq=self._order_seq,
        )
        self.orders.append(order)
        self._orders_by_id[order.order_id] = order
        return order

    def _add_sessions(self, session: date, count: int) -> date:
        sessions = self.calendar.sessions(session, session + timedelta(days=90))
        if not sessions:
            return session
        index = min(max(0, count), len(sessions) - 1)
        return sessions[index]

    def _entry_active_from(self, clock: datetime, session: date) -> datetime:
        session_open = self.calendar.session_open(session)
        if clock >= self.calendar.session_close(session):
            return self.calendar.session_open(self.calendar.next_session(session))
        if clock <= session_open:
            return session_open
        bar_times = self.calendar.bar_times(session, self.config.granularity.value)
        for bar_time in bar_times:
            if bar_time > clock:
                return bar_time
        return session_open

    def _plan_for_key(self, plan_key: str) -> ReplayPlan:
        state = self.plan_states.get(plan_key)
        if state is not None:
            return state.plan
        raise KeyError(plan_key)

    def _check_cancel(self) -> None:
        self._cancel_check_counter += 1
        # Cross-process cancellation needs a DB read; sample it instead of
        # querying on every 5-minute bar while keeping the first check immediate.
        if self._cancel_check_counter % 8 != 1:
            return
        if self.cancel_callback and self.cancel_callback():
            raise BacktestCancelled("cancel requested")


# ----------------------------------------------------------------------
# Matching primitives
# ----------------------------------------------------------------------
def _match_order(
    order: Order,
    bar: Bar,
    *,
    rule: MarketRule,
    capacity: int,
    slippage_bps: Decimal,
    is_buy: bool,
) -> FillCandidate | None:
    if _is_suspended(bar):
        return None
    if is_buy and _is_one_price_limit_up(bar, rule):
        return None
    if not is_buy and _is_one_price_limit_down(bar, rule):
        return None
    if order.order_type is OrderType.LIMIT:
        if order.limit_price is None:
            return None
        if is_buy:
            return _buy_limit_candidate(order, bar, rule, capacity, slippage_bps)
        return _sell_limit_candidate(order, bar, rule, capacity, slippage_bps)
    if order.order_type is OrderType.STOP_MARKET:
        if order.stop_price is None:
            return None
        return _sell_stop_candidate(order, bar, rule, capacity, slippage_bps)
    if order.order_type is OrderType.MARKET:
        if is_buy:
            return _buy_market_candidate(order, bar, rule, capacity, slippage_bps)
        return _sell_market_candidate(order, bar, rule, capacity, slippage_bps)
    return None


def _buy_limit_candidate(
    order: Order, bar: Bar, rule: MarketRule, capacity: int, slippage_bps: Decimal
) -> FillCandidate | None:
    limit = order.limit_price
    if limit is None:
        return None
    if order.entry_floor is not None and bar.open < order.entry_floor:
        return None
    if bar.open <= limit:
        raw = bar.open
    elif bar.low <= limit:
        raw = limit
    else:
        return None
    price = _tick_ceil(raw * (Decimal("1") + slippage_bps / Decimal("10000")), rule.tick_size)
    if price > limit:
        price = limit
    quality = "full"
    if _touches_limit_up(bar, rule):
        quality = "limit_up_liquidity_assumption"
    quantity = min(order.remaining, max(0, capacity))
    if quantity <= 0:
        return None
    if quality != "full":
        return FillCandidate(quantity, price, quality, "buy_limit_limit_up", raw)
    return FillCandidate(quantity, price, "full", "buy_limit", raw)


def _sell_limit_candidate(
    order: Order, bar: Bar, rule: MarketRule, capacity: int, slippage_bps: Decimal
) -> FillCandidate | None:
    limit = order.limit_price
    if limit is None:
        return None
    if bar.open >= limit:
        raw = bar.open
    elif bar.high >= limit:
        raw = limit
    else:
        return None
    price = _tick_floor(raw * (Decimal("1") - slippage_bps / Decimal("10000")), rule.tick_size)
    if price < limit:
        price = limit
    quality = "full"
    if _touches_limit_down(bar, rule):
        quality = "limit_down_liquidity_assumption"
    quantity = min(order.remaining, max(0, capacity))
    if quantity <= 0:
        return None
    return FillCandidate(quantity, price, quality, "sell_limit", raw)


def _sell_stop_candidate(
    order: Order, bar: Bar, rule: MarketRule, capacity: int, slippage_bps: Decimal
) -> FillCandidate | None:
    stop = order.stop_price
    if stop is None:
        return None
    if bar.open <= stop:
        raw = bar.open
    elif bar.low <= stop:
        raw = stop
    else:
        return None
    price = _tick_floor(raw * (Decimal("1") - slippage_bps / Decimal("10000")), rule.tick_size)
    if price <= 0:
        return None
    quantity = min(order.remaining, max(0, capacity))
    if quantity <= 0:
        return None
    return FillCandidate(quantity, price, "stop_market", "stop_loss_gap" if bar.open <= stop else "stop_loss_intraday", raw)


def _buy_market_candidate(
    order: Order, bar: Bar, rule: MarketRule, capacity: int, slippage_bps: Decimal
) -> FillCandidate | None:
    protection = order.limit_price
    if protection is None:
        return None
    if bar.open > protection:
        return None
    price = _tick_ceil(bar.open * (Decimal("1") + slippage_bps / Decimal("10000")), rule.tick_size)
    if price > protection:
        price = protection
    quantity = min(order.remaining, max(0, capacity))
    return FillCandidate(quantity, price, "market_protected", "market_buy", bar.open) if quantity > 0 else None


def _sell_market_candidate(
    order: Order, bar: Bar, rule: MarketRule, capacity: int, slippage_bps: Decimal
) -> FillCandidate | None:
    price = _tick_floor(bar.open * (Decimal("1") - slippage_bps / Decimal("10000")), rule.tick_size)
    if price <= 0:
        return None
    quantity = min(order.remaining, max(0, capacity))
    return FillCandidate(quantity, price, "market", "market_sell", bar.open) if quantity > 0 else None


def _tick_ceil(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _tick_floor(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _is_suspended(bar: Bar) -> bool:
    return bar.volume <= 0


def _is_one_price(bar: Bar) -> bool:
    return bar.open == bar.high == bar.low == bar.close


def _touches_limit_up(bar: Bar, rule: MarketRule) -> bool:
    return rule.limit_up is not None and bar.high >= rule.limit_up


def _touches_limit_down(bar: Bar, rule: MarketRule) -> bool:
    return rule.limit_down is not None and bar.low <= rule.limit_down


def _is_one_price_limit_up(bar: Bar, rule: MarketRule) -> bool:
    return _is_one_price(bar) and rule.limit_up is not None and bar.close >= rule.limit_up


def _is_one_price_limit_down(bar: Bar, rule: MarketRule) -> bool:
    return _is_one_price(bar) and rule.limit_down is not None and bar.close <= rule.limit_down


def _evaluate_confirmation(plan: ReplayPlan, daily: list[DailyBar], session: date) -> str:
    scenario_payload = plan.scenario_json
    if not scenario_payload:
        return "confirmed"
    try:
        scenario = ZoneScenario.model_validate(scenario_payload)
    except Exception:
        return "not_triggered"
    params = get_price_plan_params("price-plan-params.v1")
    if params is None:
        return "not_triggered"
    eligible = [
        {
            "date": bar.session,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in daily
        if bar.session >= plan.valid_from and bar.session <= session
    ]
    if not eligible:
        return "not_triggered"
    window = eligible[: scenario.confirmation_valid_trading_days]
    touch_index: int | None = None
    for index, _bar in enumerate(window):
        evaluation = evaluate_confirmation_bar(scenario, eligible, index, params=params)
        if evaluation.touched and touch_index is None:
            touch_index = index
        confirmed = (
            touch_index is not None
            and index - touch_index < params.confirmation_after_touch_bars
            and evaluation.pattern_confirmed
        )
        if evaluation.invalidated and confirmed:
            return "ambiguous"
        if evaluation.invalidated:
            return "invalidated"
        if confirmed:
            return "confirmed"
    if len(eligible) >= scenario.confirmation_valid_trading_days:
        return "not_triggered"
    return "pending"


__all__ = [
    "BacktestCancelled",
    "ExecutionEngine",
    "ExecutionResult",
    "ExitInstruction",
    "MarketDataView",
    "PlanState",
]
