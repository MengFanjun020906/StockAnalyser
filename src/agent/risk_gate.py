# -*- coding: utf-8 -*-
"""Deterministic A-share risk gate for structured Agent trade plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from src.schemas.agent_context import AccountContext, InvestorProfile, PositionContext
from src.schemas.agent_signal import (
    DataQuality,
    EXIT_ACTIONS,
    OPEN_ACTIONS,
    RiskGateCheck,
    RiskGateResult,
    TradeAction,
    TradePlan,
)


FAILED_DATA_QUALITY = {"failed", "insufficient"}
ACTIVE_ACTIONS = OPEN_ACTIONS | EXIT_ACTIONS


@dataclass(frozen=True)
class QuoteState:
    """Minimal quote state required by deterministic A-share constraints."""

    symbol: str
    last_price: Optional[float] = None
    pct_change: Optional[float] = None
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_st: bool = False
    is_delisting: bool = False
    is_ipo_special_period: bool = False
    market: str = "cn"


@dataclass(frozen=True)
class RiskGateConfig:
    """Static policy defaults for risk-gate evaluation."""

    min_active_trade_confidence: float = 0.75
    default_max_single_position_pct: float = 20.0
    default_max_total_equity_exposure_pct: float = 80.0
    require_stop_for_active_trade: bool = True
    block_open_on_limited_data: bool = True
    manual_review_for_special_status: bool = True


@dataclass(frozen=True)
class RiskGateInput:
    """Input bundle consumed by :class:`RiskGateEvaluator`."""

    plan: TradePlan
    quote: Optional[QuoteState] = None
    investor: Optional[InvestorProfile] = None
    account: Optional[AccountContext] = None
    position: Optional[PositionContext] = None
    data_quality: DataQuality = "unknown"
    failed_tools: Sequence[str] = ()
    l3_confidence: Optional[float] = None
    current_total_exposure_pct: Optional[float] = None


class RiskGateEvaluator:
    """Evaluate A-share hard constraints before simulation or execution."""

    def __init__(self, config: Optional[RiskGateConfig] = None) -> None:
        self.config = config or RiskGateConfig()

    def evaluate(self, gate_input: RiskGateInput) -> RiskGateResult:
        """Return deterministic gate result for one proposed trade plan."""
        checks: List[RiskGateCheck] = []
        checks.extend(self._check_special_status(gate_input))
        checks.extend(self._check_t_plus_one(gate_input))
        checks.extend(self._check_limit_constraints(gate_input))
        checks.extend(self._check_required_risk_controls(gate_input))
        checks.extend(self._check_confidence(gate_input))
        checks.extend(self._check_data_quality(gate_input))
        checks.extend(self._check_cash_and_exposure(gate_input))

        return self._build_result(gate_input.plan.action, checks)

    def _check_special_status(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        quote = gate_input.quote
        plan = gate_input.plan
        if not quote or plan.action not in OPEN_ACTIONS:
            return []

        reasons = []
        if quote.is_st:
            reasons.append("ST")
        if quote.is_delisting:
            reasons.append("退市整理")
        if quote.is_ipo_special_period:
            reasons.append("上市特殊交易期")
        if not reasons:
            return [
                self._pass(
                    "a_share_special_status",
                    "未命中 ST、退市整理或上市特殊交易期限制。",
                )
            ]

        return [
            self._fail(
                "a_share_special_status",
                f"{quote.symbol} 处于{','.join(reasons)}，开仓/加仓必须人工确认。",
                suggested_action="manual_review",
                details={"reasons": reasons},
                blocking=not self.config.manual_review_for_special_status,
            )
        ]

    def _check_t_plus_one(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        plan = gate_input.plan
        position = gate_input.position
        if plan.action not in EXIT_ACTIONS:
            return []
        if not position:
            return [
                self._fail(
                    "a_share_t_plus_one",
                    "缺少持仓上下文，无法确认是否为当日买入，卖出/减仓需人工确认。",
                    suggested_action="manual_review",
                    blocking=False,
                )
            ]
        if position.holding_days == 0:
            return [
                self._fail(
                    "a_share_t_plus_one",
                    "A 股 T+1 约束：当日买入持仓不能在当日卖出或减仓。",
                    suggested_action="manual_review",
                )
            ]
        return [self._pass("a_share_t_plus_one", "持仓不属于当日买入，T+1 检查通过。")]

    def _check_limit_constraints(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        quote = gate_input.quote
        plan = gate_input.plan
        if not quote:
            return []
        checks: List[RiskGateCheck] = []
        if plan.action in OPEN_ACTIONS and quote.is_limit_up:
            checks.append(
                self._fail(
                    "a_share_limit_up_no_chase",
                    "A 股涨停禁追：涨停状态不生成立即追买方案。",
                    suggested_action="wait",
                )
            )
        if plan.action in EXIT_ACTIONS and quote.is_limit_down:
            checks.append(
                self._fail(
                    "a_share_limit_down_no_fake_sell",
                    "A 股跌停时不能假定立即卖出成交，只能转为人工确认或后续条件计划。",
                    suggested_action="manual_review",
                    blocking=False,
                )
            )
        if not checks:
            checks.append(self._pass("a_share_price_limit", "未命中涨停追买或跌停卖出限制。"))
        return checks

    def _check_required_risk_controls(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        plan = gate_input.plan
        if plan.action not in ACTIVE_ACTIONS or not self.config.require_stop_for_active_trade:
            return []
        has_stop = plan.stop_loss_price is not None or plan.stop_loss_pct is not None
        if has_stop or plan.invalidation_conditions:
            return [self._pass("risk_control_required", "主动交易计划已包含止损或失效条件。")]
        return [
            self._fail(
                "risk_control_required",
                "每笔主动交易必须包含止损或失效条件。",
                suggested_action="manual_review",
            )
        ]

    def _check_confidence(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        plan = gate_input.plan
        if plan.action not in ACTIVE_ACTIONS or gate_input.l3_confidence is None:
            return []
        if gate_input.l3_confidence >= self.config.min_active_trade_confidence:
            return [self._pass("l3_confidence_gate", "L3 置信度达到主动交易门槛。")]
        return [
            self._fail(
                "l3_confidence_gate",
                f"L3 置信度 {gate_input.l3_confidence:.2f} 低于主动交易门槛 "
                f"{self.config.min_active_trade_confidence:.2f}。",
                suggested_action="wait",
            )
        ]

    def _check_data_quality(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        plan = gate_input.plan
        if not gate_input.failed_tools and gate_input.data_quality not in FAILED_DATA_QUALITY:
            return [self._pass("critical_data_quality", "关键数据质量未标记为失败或不足。")]

        details = {"data_quality": gate_input.data_quality, "failed_tools": list(gate_input.failed_tools)}
        if plan.action in ACTIVE_ACTIONS:
            suggested: TradeAction = "wait" if plan.action in OPEN_ACTIONS else "manual_review"
            return [
                self._fail(
                    "critical_data_quality",
                    "关键数据缺失或工具失败，主动交易动作必须降级。",
                    suggested_action=suggested,
                    details=details,
                    blocking=self.config.block_open_on_limited_data,
                )
            ]
        return [
            self._fail(
                "critical_data_quality",
                "关键数据缺失或工具失败，仅允许观察、等待或人工复核。",
                suggested_action="monitor",
                details=details,
                blocking=False,
            )
        ]

    def _check_cash_and_exposure(self, gate_input: RiskGateInput) -> List[RiskGateCheck]:
        plan = gate_input.plan
        if plan.action not in OPEN_ACTIONS:
            return []

        checks: List[RiskGateCheck] = []
        investor = gate_input.investor
        max_single = (
            investor.max_single_position_pct
            if investor and investor.max_single_position_pct is not None
            else self.config.default_max_single_position_pct
        )
        max_total = (
            investor.max_total_equity_exposure_pct
            if investor and investor.max_total_equity_exposure_pct is not None
            else self.config.default_max_total_equity_exposure_pct
        )

        if plan.target_position_pct is not None and plan.target_position_pct > max_single:
            checks.append(
                self._fail(
                    "single_position_limit",
                    f"目标单股仓位 {plan.target_position_pct:.2f}% 超过上限 {max_single:.2f}%。",
                    suggested_action="manual_review",
                    details={"target_position_pct": plan.target_position_pct, "max_single_position_pct": max_single},
                )
            )
        else:
            checks.append(self._pass("single_position_limit", "单股目标仓位未超过上限。"))

        projected_total = self._project_total_exposure(gate_input)
        if projected_total is not None and projected_total > max_total:
            checks.append(
                self._fail(
                    "total_exposure_limit",
                    f"预计总权益仓位 {projected_total:.2f}% 超过上限 {max_total:.2f}%。",
                    suggested_action="manual_review",
                    details={
                        "projected_total_exposure_pct": projected_total,
                        "max_total_equity_exposure_pct": max_total,
                    },
                )
            )
        elif projected_total is not None:
            checks.append(self._pass("total_exposure_limit", "预计总权益仓位未超过上限。"))

        if self._has_insufficient_cash(gate_input):
            checks.append(
                self._fail(
                    "cash_available",
                    "账户可用现金不足以支持计划仓位，需调整仓位或人工确认。",
                    suggested_action="manual_review",
                    blocking=False,
                )
            )
        elif (
            gate_input.account
            and gate_input.account.total_equity is not None
            and plan.target_position_pct is not None
        ):
            checks.append(self._pass("cash_available", "账户现金检查未发现明显不足。"))

        return checks

    def _project_total_exposure(self, gate_input: RiskGateInput) -> Optional[float]:
        current = gate_input.current_total_exposure_pct
        target = gate_input.plan.target_position_pct
        existing = gate_input.position.position_pct if gate_input.position else None
        if current is None or target is None:
            return None
        return max(0.0, current - float(existing or 0.0) + target)

    def _has_insufficient_cash(self, gate_input: RiskGateInput) -> bool:
        account = gate_input.account
        plan = gate_input.plan
        if not account or account.total_equity is None or account.available_cash is None:
            return False
        if plan.target_position_pct is None:
            return False
        current_position_pct = gate_input.position.position_pct if gate_input.position else 0.0
        additional_pct = max(0.0, plan.target_position_pct - float(current_position_pct or 0.0))
        required_cash = account.total_equity * additional_pct / 100.0
        return required_cash > account.available_cash

    def _build_result(self, original_action: TradeAction, checks: Sequence[RiskGateCheck]) -> RiskGateResult:
        blocking = [check for check in checks if not check.passed and check.severity == "blocking"]
        warnings = [check.message for check in checks if not check.passed and check.severity == "warning"]
        blocked_reasons = [check.message for check in blocking]
        suggested_action = self._select_suggested_action(checks, original_action)

        if blocking:
            status = "blocked"
            allowed_action = suggested_action
        elif any(not check.passed for check in checks):
            allowed_action = suggested_action
            status = "manual_review" if allowed_action == "manual_review" else "downgraded"
        else:
            status = "passed"
            allowed_action = original_action

        return RiskGateResult(
            status=status,
            original_action=original_action,
            allowed_action=allowed_action,
            checks=list(checks),
            blocked_reasons=blocked_reasons,
            warnings=warnings,
            required_manual_review=allowed_action == "manual_review",
        )

    def _select_suggested_action(
        self,
        checks: Iterable[RiskGateCheck],
        fallback_action: TradeAction,
    ) -> TradeAction:
        priority: List[TradeAction] = ["manual_review", "wait", "monitor", "reject"]
        suggestions = [check.suggested_action for check in checks if not check.passed and check.suggested_action]
        for action in priority:
            if action in suggestions:
                return action
        return fallback_action

    def _pass(self, rule_id: str, message: str) -> RiskGateCheck:
        return RiskGateCheck(rule_id=rule_id, passed=True, severity="info", message=message)

    def _fail(
        self,
        rule_id: str,
        message: str,
        *,
        suggested_action: TradeAction,
        details: Optional[dict] = None,
        blocking: bool = True,
    ) -> RiskGateCheck:
        return RiskGateCheck(
            rule_id=rule_id,
            passed=False,
            severity="blocking" if blocking else "warning",
            message=message,
            suggested_action=suggested_action,
            details=details or {},
        )


__all__ = [
    "QuoteState",
    "RiskGateConfig",
    "RiskGateEvaluator",
    "RiskGateInput",
]
