# -*- coding: utf-8 -*-
"""Deterministic fill rules for the paper-trading shadow account.

All prices come from the deterministic pricing engine scenarios; this module
only decides *whether* an engine level was reached on a daily bar and at what
price the order would have filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.backtest.fees import estimate_sell_fees, get_fee_policy, money

DEFAULT_FEE_POLICY = "cn-a-share-2023.v1"
LOT_SIZE = 100


@dataclass(frozen=True)
class Bar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    pct_chg: Optional[float] = None

    @property
    def tradable(self) -> bool:
        return self.open > 0 and self.volume > 0


@dataclass(frozen=True)
class PlanLevels:
    symbol: str
    plan_id: str
    scenario_id: str
    entry_low: float
    entry_high: float
    stop_loss: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    target_1_exit_ratio: float
    valid_until: Optional[str]


@dataclass(frozen=True)
class ExitInstruction:
    quantity: int
    price: float
    reason: str
    remaining_after: int
    target_1_done: bool


def limit_pct(code: str) -> float:
    """Daily price-limit ratio used by the local 6-digit A-share universe."""
    if code.startswith(("300", "301", "688")):
        return 20.0
    if code.startswith(("4", "8")):
        return 30.0
    return 10.0


def is_limit_locked(bar: Bar, code: str, direction: str) -> bool:
    """One-word limit board detection; non A-share codes are never locked.

    ``direction`` is ``"up"`` (cannot buy) or ``"down"`` (cannot sell).
    """
    if not (len(code) == 6 and code.isdigit()):
        return False
    if bar.high <= 0 or bar.low <= 0:
        return False
    one_word = abs(bar.high - bar.low) < 1e-6 and abs(bar.open - bar.high) < 1e-6
    if not one_word or bar.pct_chg is None:
        return False
    threshold = limit_pct(code) - 0.2
    if direction == "up":
        return float(bar.pct_chg) >= threshold
    return float(bar.pct_chg) <= -threshold


def entry_fill_price(bar: Bar, levels: PlanLevels, mode: str = "touch") -> Optional[float]:
    """Fill rule for a limit buy on the engine entry zone.

    ``touch`` (default): limit at the zone upper edge; any session low inside
    the zone fills, gaps below fill at the open.  ``close`` is the more
    conservative confirmation: the close itself must land inside the zone and
    the fill is the close.
    """
    if not bar.tradable:
        return None
    if mode == "close":
        if levels.entry_low <= bar.close <= levels.entry_high:
            return round(bar.close, 4)
        return None
    if bar.low > levels.entry_high:
        return None
    fill = min(levels.entry_high, bar.open)
    if fill < levels.entry_low:
        fill = bar.open
    return round(fill, 4)


def exit_instructions(position: Dict[str, Any], bar: Bar) -> List[ExitInstruction]:
    """Stop first, then target-1 partial, then target-2 liquidation."""
    available = int(position.get("available_quantity") or 0)
    quantity = int(position.get("quantity") or 0)
    if available <= 0 or quantity <= 0 or not bar.tradable:
        return []

    stop = _as_float(position.get("stop_loss"))
    target_1 = _as_float(position.get("target_1"))
    target_2 = _as_float(position.get("target_2"))
    ratio = _as_float(position.get("target_1_exit_ratio")) or 0.0
    target_1_done = bool(position.get("target_1_done"))

    if stop is not None and bar.low <= stop:
        fill = bar.open if bar.open <= stop else stop
        return [
            ExitInstruction(
                quantity=available,
                price=round(fill, 4),
                reason="stop_loss",
                remaining_after=quantity - available,
                target_1_done=target_1_done,
            )
        ]

    instructions: List[ExitInstruction] = []
    remaining = available
    if target_2 is not None and bar.high >= target_2:
        fill = max(target_2, bar.open) if bar.open >= target_2 else target_2
        instructions.append(
            ExitInstruction(
                quantity=remaining,
                price=round(min(fill, bar.high), 4),
                reason="target_2",
                remaining_after=quantity - remaining,
                target_1_done=True,
            )
        )
        return instructions

    if target_1 is not None and not target_1_done and bar.high >= target_1:
        sell_qty = int(quantity * ratio) // LOT_SIZE * LOT_SIZE
        if ratio > 0 and sell_qty <= 0 and quantity >= 2 * LOT_SIZE:
            sell_qty = LOT_SIZE
        sell_qty = min(sell_qty, remaining)
        if sell_qty > 0:
            fill = max(target_1, bar.open) if bar.open >= target_1 else target_1
            instructions.append(
                ExitInstruction(
                    quantity=sell_qty,
                    price=round(min(fill, bar.high), 4),
                    reason="target_1",
                    remaining_after=quantity - sell_qty,
                    target_1_done=True,
                )
            )
    return instructions


def buy_fees(notional: float, trade_date: date) -> float:
    rule = get_fee_policy(DEFAULT_FEE_POLICY).rule_for(trade_date)
    value = Decimal(str(max(notional, 0.0)))
    commission = max(value * rule.commission_bps / Decimal("10000"), rule.minimum_commission)
    transfer = value * rule.transfer_fee_bps / Decimal("10000")
    stamp = value * rule.stamp_duty_buy_bps / Decimal("10000")
    return float(money(commission + transfer + stamp))


def sell_fees(notional: float, trade_date: date) -> float:
    rule = get_fee_policy(DEFAULT_FEE_POLICY).rule_for(trade_date)
    return float(estimate_sell_fees(rule, Decimal(str(max(notional, 0.0)))))


def affordable_quantity(
    *,
    cash: float,
    budget: float,
    price: float,
    trade_date: date,
) -> int:
    """Max lot-aligned quantity fitting ``budget`` and available cash."""
    if price <= 0 or cash <= 0 or budget <= 0:
        return 0
    cap = min(float(cash), float(budget))
    lots = int(cap // (price * LOT_SIZE))
    while lots > 0:
        quantity = lots * LOT_SIZE
        notional = price * quantity
        if notional + buy_fees(notional, trade_date) <= cap + 1e-9:
            return quantity
        lots -= 1
    return 0


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "Bar",
    "DEFAULT_FEE_POLICY",
    "ExitInstruction",
    "LOT_SIZE",
    "PlanLevels",
    "affordable_quantity",
    "buy_fees",
    "entry_fill_price",
    "exit_instructions",
    "is_limit_locked",
    "limit_pct",
    "sell_fees",
]
