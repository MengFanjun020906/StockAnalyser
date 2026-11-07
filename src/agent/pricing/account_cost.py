from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from .schemas import ZoneScenario


@dataclass(frozen=True)
class AccountCostEvaluation:
    status: str
    account_net_rr_1: Decimal | None
    quantity: int | None
    policy_ref: str | None
    reason_code: str | None


def evaluate_account_cost(
    scenario: ZoneScenario | None,
    *,
    target_position_pct: Decimal | None,
    lot_size: int,
    account: dict[str, Any] | None,
    minimum_rr: Decimal,
) -> AccountCostEvaluation:
    if account is None:
        return AccountCostEvaluation(
            status="account_absent",
            account_net_rr_1=None,
            quantity=None,
            policy_ref="reference-cost-policy.v1",
            reason_code="ACCOUNT_CONTEXT_ABSENT_REFERENCE_COST",
        )
    if scenario is None or scenario.stop_loss is None or scenario.target_1 is None:
        return _unavailable("ACCOUNT_NET_RR_UNAVAILABLE")
    required = (
        "total_equity",
        "available_cash",
        "buy_commission_bps",
        "sell_commission_bps",
        "minimum_commission",
        "transfer_fee_bps",
        "stamp_duty_bps",
        "cost_policy_version",
    )
    if target_position_pct is None or any(account.get(key) in (None, "") for key in required):
        return _unavailable("ACCOUNT_COST_POLICY_UNAVAILABLE")
    try:
        total_equity = Decimal(str(account["total_equity"]))
        available_cash = Decimal(str(account["available_cash"]))
        buy_commission = Decimal(str(account["buy_commission_bps"])) / Decimal("10000")
        sell_commission = Decimal(str(account["sell_commission_bps"])) / Decimal("10000")
        minimum_commission = Decimal(str(account["minimum_commission"]))
        transfer_fee = Decimal(str(account["transfer_fee_bps"])) / Decimal("10000")
        stamp_duty = Decimal(str(account["stamp_duty_bps"])) / Decimal("10000")
    except (TypeError, ValueError, ArithmeticError):
        return _unavailable("ACCOUNT_COST_POLICY_UNAVAILABLE")
    budget = min(
        available_cash,
        total_equity * target_position_pct / Decimal("100"),
    )
    lot_notional = scenario.entry_reference * Decimal(lot_size)
    lots = (budget / lot_notional).to_integral_value(rounding=ROUND_FLOOR)
    quantity = int(lots) * lot_size
    if quantity <= 0:
        return AccountCostEvaluation(
            status="unavailable",
            account_net_rr_1=None,
            quantity=0,
            policy_ref=str(account["cost_policy_version"]),
            reason_code="ACCOUNT_ORDER_SIZE_UNAVAILABLE",
        )
    quantity_decimal = Decimal(quantity)
    buy_notional = scenario.entry_reference * quantity_decimal
    buy_fee = max(buy_notional * buy_commission, minimum_commission)
    buy_fee += buy_notional * transfer_fee
    entry_cost = buy_notional + buy_fee
    stop_notional = scenario.stop_loss * quantity_decimal
    stop_fee = max(stop_notional * sell_commission, minimum_commission)
    stop_fee += stop_notional * (transfer_fee + stamp_duty)
    target_notional = scenario.target_1 * quantity_decimal
    target_fee = max(target_notional * sell_commission, minimum_commission)
    target_fee += target_notional * (transfer_fee + stamp_duty)
    net_risk = entry_cost - (stop_notional - stop_fee)
    net_reward = (target_notional - target_fee) - entry_cost
    if net_risk <= 0:
        return _unavailable("ACCOUNT_NET_RR_UNAVAILABLE")
    net_rr = net_reward / net_risk
    return AccountCostEvaluation(
        status="ok" if net_rr >= minimum_rr else "below_minimum",
        account_net_rr_1=net_rr.quantize(Decimal("0.000001")),
        quantity=quantity,
        policy_ref=str(account["cost_policy_version"]),
        reason_code=None if net_rr >= minimum_rr else "ACCOUNT_NET_RR_BELOW_MINIMUM",
    )


def _unavailable(reason: str) -> AccountCostEvaluation:
    return AccountCostEvaluation(
        status="unavailable",
        account_net_rr_1=None,
        quantity=None,
        policy_ref=None,
        reason_code=reason,
    )


__all__ = ["AccountCostEvaluation", "evaluate_account_cost"]
