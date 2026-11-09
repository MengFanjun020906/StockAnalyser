# -*- coding: utf-8 -*-
"""Versioned A-share cash fee policies and deterministic fee accrual."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .schemas import FeePolicy, FeeRule, OrderSide


CENT = Decimal("0.01")
BPS = Decimal("10000")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FeeDelta:
    commission: Decimal = Decimal("0")
    transfer_fee: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.commission + self.transfer_fee + self.stamp_duty


@dataclass
class FeeAccumulator:
    """Accumulates one order/day so minimum commission is charged exactly once.

    The first fill of a day is charged ``max(notional*rate, minimum)``.  Later
    fills are charged the proportional rate.  ``settle_day`` trues the total up
    (or refunds an over-charge when the first fill was larger than the day's
    final target), which keeps partial fills across bars/days auditable.
    """

    rule: FeeRule
    side: OrderSide
    trade_date: date
    day_notional: Decimal = Decimal("0")
    commission_paid: Decimal = Decimal("0")
    transfer_paid: Decimal = Decimal("0")
    stamp_paid: Decimal = Decimal("0")
    fill_count: int = 0

    def on_fill(self, notional: Decimal) -> FeeDelta:
        if notional <= 0:
            return FeeDelta()
        commission_rate = self.rule.commission_bps / BPS
        transfer_rate = self.rule.transfer_fee_bps / BPS
        stamp_rate = (
            self.rule.stamp_duty_sell_bps if self.side is OrderSide.SELL else self.rule.stamp_duty_buy_bps
        ) / BPS
        raw_commission = notional * commission_rate
        if self.fill_count == 0:
            raw_commission = max(raw_commission, self.rule.minimum_commission)
        commission = money(raw_commission)
        transfer = money(notional * transfer_rate)
        stamp = money(notional * stamp_rate)
        self.fill_count += 1
        self.day_notional += notional
        self.commission_paid += commission
        self.transfer_paid += transfer
        self.stamp_paid += stamp
        return FeeDelta(commission=commission, transfer_fee=transfer, stamp_duty=stamp)

    def settle_day(self) -> FeeDelta:
        """Return the adjustment needed to reach exact daily fee targets."""
        if self.day_notional <= 0:
            return FeeDelta()
        commission_rate = self.rule.commission_bps / BPS
        transfer_rate = self.rule.transfer_fee_bps / BPS
        stamp_rate = (
            self.rule.stamp_duty_sell_bps if self.side is OrderSide.SELL else self.rule.stamp_duty_buy_bps
        ) / BPS
        target_commission = money(
            max(self.rule.minimum_commission, self.day_notional * commission_rate)
        )
        target_transfer = money(self.day_notional * transfer_rate)
        target_stamp = money(self.day_notional * stamp_rate)
        return FeeDelta(
            commission=target_commission - self.commission_paid,
            transfer_fee=target_transfer - self.transfer_paid,
            stamp_duty=target_stamp - self.stamp_paid,
        )


def estimate_max_buy_fees(rule: FeeRule, notional: Decimal, valid_days: int) -> Decimal:
    """Conservative reservation buffer for an open buy order."""
    rate = rule.commission_bps / BPS
    transfer = rule.transfer_fee_bps / BPS
    stamp = rule.stamp_duty_buy_bps / BPS
    days = Decimal(max(1, int(valid_days)))
    estimate = notional * (rate + transfer + stamp) + rule.minimum_commission * days
    return money(estimate)


def estimate_sell_fees(rule: FeeRule, notional: Decimal) -> Decimal:
    rate = rule.commission_bps / BPS
    transfer = rule.transfer_fee_bps / BPS
    stamp = rule.stamp_duty_sell_bps / BPS
    return money(max(notional * rate, rule.minimum_commission) + notional * (transfer + stamp))


# Pre-2023-08-28 stamp duty was 0.1%; after that date it is 0.05%.  Commission
# and transfer rates are experiment assumptions, not broker quotes.  They are
# versioned and displayed in the UI.
_FEE_RULES_2023 = [
    FeeRule(
        effective_from=date(1990, 12, 19),
        effective_to=date(2023, 8, 27),
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        transfer_fee_bps=Decimal("0.1"),
        stamp_duty_sell_bps=Decimal("10"),
        stamp_duty_buy_bps=Decimal("0"),
    ),
    FeeRule(
        effective_from=date(2023, 8, 28),
        effective_to=None,
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        transfer_fee_bps=Decimal("0.1"),
        stamp_duty_sell_bps=Decimal("5"),
        stamp_duty_buy_bps=Decimal("0"),
    ),
]

FEE_POLICIES: dict[str, FeePolicy] = {
    "cn-a-share-2023.v1": FeePolicy(
        version="cn-a-share-2023.v1",
        rules=_FEE_RULES_2023,
        description="A 股 2023-08-28 印花税调整；佣金 2.5bp 最低 5 元，过户费 0.1bp。",
    ),
    "cn-a-share-zero-cost.v1": FeePolicy(
        version="cn-a-share-zero-cost.v1",
        rules=[
            FeeRule(
                effective_from=date(1990, 12, 19),
                effective_to=None,
                commission_bps=Decimal("0"),
                minimum_commission=Decimal("0"),
                transfer_fee_bps=Decimal("0"),
                stamp_duty_sell_bps=Decimal("0"),
                stamp_duty_buy_bps=Decimal("0"),
            )
        ],
        description="零费用诊断策略，仅用于与主实验分开列示的敏感性对照。",
    ),
}

for _policy in FEE_POLICIES.values():
    _policy.validate_policy()


def get_fee_policy(ref: str) -> FeePolicy:
    key = str(ref or "").strip()
    policy = FEE_POLICIES.get(key)
    if policy is None:
        raise KeyError(f"unknown fee policy: {key}")
    return policy


__all__ = [
    "FEE_POLICIES",
    "FeeAccumulator",
    "FeeDelta",
    "estimate_max_buy_fees",
    "estimate_sell_fees",
    "get_fee_policy",
    "money",
]
