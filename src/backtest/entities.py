# -*- coding: utf-8 -*-
"""Hot-path entities used by the execution kernel and ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .schemas import ExitReason, OrderSide, OrderStatus, OrderType


@dataclass(frozen=True)
class Bar:
    symbol: str
    end_at: datetime
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    frequency: str = "5m"
    source: str = ""
    volume_unit: str = "shares"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "end_at": self.end_at.isoformat(),
            "session": self.session.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "amount": str(self.amount),
            "frequency": self.frequency,
            "source": self.source,
            "volume_unit": self.volume_unit,
        }


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    source: str = ""
    volume_unit: str = "shares"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session": self.session.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "amount": str(self.amount),
            "source": self.source,
            "volume_unit": self.volume_unit,
        }


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    action_type: str
    cash_per_share: Decimal = Decimal("0")
    stock_ratio: Decimal = Decimal("0")
    split_ratio: Decimal = Decimal("1")
    version: str = ""
    source: str = ""
    verified: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "action_type": self.action_type,
            "cash_per_share": str(self.cash_per_share),
            "stock_ratio": str(self.stock_ratio),
            "split_ratio": str(self.split_ratio),
            "version": self.version,
            "source": self.source,
            "verified": self.verified,
            "payload": self.payload,
        }


@dataclass
class Order:
    order_id: str
    run_id: str
    plan_key: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None
    stop_price: Decimal | None = None
    entry_floor: Decimal | None = None
    submitted_at: datetime | None = None
    active_from: datetime | None = None
    expires_on_session: date | None = None
    status: OrderStatus = OrderStatus.PENDING
    remaining: int = 0
    filled_quantity: int = 0
    avg_fill_price: Decimal = Decimal("0")
    reserved_cash: Decimal = Decimal("0")
    reserved_quantity: int = 0
    priority_rank: int = 999
    priority_available_at: datetime | None = None
    reason: str = ""
    tags: tuple[str, ...] = ()
    fee_accumulator: Any = None
    pre_registered_exit: bool = False
    exit_reason: str = ""
    created_seq: int = 0

    def __post_init__(self) -> None:
        if self.remaining == 0:
            self.remaining = self.quantity

    @property
    def is_open(self) -> bool:
        return self.status in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}

    @property
    def priority_key(self) -> tuple[Any, ...]:
        return (
            self.priority_rank,
            self.priority_available_at or self.submitted_at or datetime.min,
            self.symbol,
            self.plan_key,
            self.order_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "run_id": self.run_id,
            "plan_key": self.plan_key,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "remaining": self.remaining,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": str(self.avg_fill_price) if self.avg_fill_price else None,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "entry_floor": str(self.entry_floor) if self.entry_floor is not None else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "active_from": self.active_from.isoformat() if self.active_from else None,
            "expires_on_session": self.expires_on_session.isoformat() if self.expires_on_session else None,
            "status": self.status.value,
            "reserved_cash": str(self.reserved_cash),
            "reserved_quantity": self.reserved_quantity,
            "priority_rank": self.priority_rank,
            "reason": self.reason,
            "tags": list(self.tags),
            "pre_registered_exit": self.pre_registered_exit,
            "exit_reason": self.exit_reason,
        }


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    run_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    gross_amount: Decimal
    commission: Decimal
    transfer_fee: Decimal
    stamp_duty: Decimal
    slippage_cost: Decimal
    bar_end_at: datetime
    session: date
    quality: str
    reason: str
    event_seq: int

    @property
    def total_fees(self) -> Decimal:
        return self.commission + self.transfer_fee + self.stamp_duty

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": str(self.price),
            "gross_amount": str(self.gross_amount),
            "commission": str(self.commission),
            "transfer_fee": str(self.transfer_fee),
            "stamp_duty": str(self.stamp_duty),
            "slippage_cost": str(self.slippage_cost),
            "bar_end_at": self.bar_end_at.isoformat(),
            "session": self.session.isoformat(),
            "quality": self.quality,
            "reason": self.reason,
            "event_seq": self.event_seq,
        }


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    event_time: datetime
    event_type: str
    symbol: str
    cash_delta: Decimal = Decimal("0")
    reserved_delta: Decimal = Decimal("0")
    quantity_delta: int = 0
    cost_delta: Decimal = Decimal("0")
    realized_pnl_delta: Decimal = Decimal("0")
    fees_delta: Decimal = Decimal("0")
    order_id: str | None = None
    fill_id: str | None = None
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_time": self.event_time.isoformat(),
            "event_type": self.event_type,
            "symbol": self.symbol,
            "cash_delta": str(self.cash_delta),
            "reserved_delta": str(self.reserved_delta),
            "quantity_delta": self.quantity_delta,
            "cost_delta": str(self.cost_delta),
            "realized_pnl_delta": str(self.realized_pnl_delta),
            "fees_delta": str(self.fees_delta),
            "order_id": self.order_id,
            "fill_id": self.fill_id,
            "reason": self.reason,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class PositionLot:
    lot_id: str
    symbol: str
    acquired_session: date
    available_session: date
    quantity: int
    remaining: int
    cost_total: Decimal
    cost_per_share: Decimal
    source_order_id: str
    source_fill_id: str

    @property
    def sellable(self) -> bool:
        return self.remaining > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "symbol": self.symbol,
            "acquired_session": self.acquired_session.isoformat(),
            "available_session": self.available_session.isoformat(),
            "quantity": self.quantity,
            "remaining": self.remaining,
            "cost_total": str(self.cost_total),
            "cost_per_share": str(self.cost_per_share),
            "source_order_id": self.source_order_id,
            "source_fill_id": self.source_fill_id,
        }


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    sellable_quantity: int = 0
    cost_total: Decimal = Decimal("0")
    market_price: Decimal | None = None
    market_price_session: date | None = None
    stale: bool = False

    @property
    def avg_cost(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return self.cost_total / Decimal(self.quantity)

    @property
    def market_value(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return (self.market_price or Decimal("0")) * Decimal(self.quantity)

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return self.market_value - self.cost_total


@dataclass(frozen=True)
class EquityPoint:
    session: date
    timestamp: datetime
    cash: Decimal
    reserved_cash: Decimal
    market_value: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    stale: bool
    benchmark_value: Decimal | None
    granularity: str
    quality: str

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.isoformat(),
            "timestamp": self.timestamp.isoformat(),
            "cash": str(self.cash),
            "reserved_cash": str(self.reserved_cash),
            "available_cash": str(self.available_cash),
            "market_value": str(self.market_value),
            "equity": str(self.equity),
            "peak_equity": str(self.peak_equity),
            "drawdown": str(self.drawdown),
            "stale": self.stale,
            "benchmark_value": str(self.benchmark_value) if self.benchmark_value is not None else None,
            "granularity": self.granularity,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class ExitPlan:
    stop_loss: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    target_1_exit_ratio: Decimal
    max_hold_trading_days: int
    reason: str = ExitReason.MAX_HOLD.value


@dataclass(frozen=True)
class FillCandidate:
    fill_quantity: int
    price: Decimal
    quality: str
    reason: str
    reference_price: Decimal | None = None


def candidate_slippage(candidate: "FillCandidate") -> Decimal:
    if candidate.reference_price is None:
        return Decimal("0")
    return abs(candidate.price - candidate.reference_price) * Decimal(candidate.fill_quantity)


@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    entry_order_id: str
    exit_order_id: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    cost_basis: Decimal
    net_proceeds: Decimal
    realized_pnl: Decimal
    entry_session: date
    exit_session: date
    exit_reason: str
    holding_days: int = 0
    mfe_pct: float | None = None
    mae_pct: float | None = None

    @property
    def return_pct(self) -> float:
        if self.cost_basis <= 0:
            return 0.0
        return float(self.realized_pnl / self.cost_basis * Decimal("100"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "quantity": self.quantity,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "cost_basis": str(self.cost_basis),
            "net_proceeds": str(self.net_proceeds),
            "realized_pnl": str(self.realized_pnl),
            "return_pct": self.return_pct,
            "entry_session": self.entry_session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "holding_days": self.holding_days,
            "exit_reason": self.exit_reason,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
        }
