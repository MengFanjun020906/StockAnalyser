# -*- coding: utf-8 -*-
"""Shared deterministic fixtures for backtest tests (no network, no production DB)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from src.agent.pricing.schemas import (
    AccountPlanDecision,
    PricePlanArtifact,
    RuleSpec,
    ZoneScenario,
)
from src.backtest.calendar import CnTradingCalendar
from src.backtest.data_view import InMemoryDataView
from src.backtest.entities import Bar, DailyBar
from src.backtest.rules import derive_rule
from src.backtest.schemas import (
    CorporateActionPolicy,
    ExperimentConfig,
    FeePolicy,
    FeeRule,
    Granularity,
    ReplayPlan,
    RiskProfile,
    SourcePipeline,
)
from src.storage import (
    AccountPlanDecisionRecord,
    Base,
    DatabaseManager,
    StockDaily,
    TradePlanArtifactRecord,
    TradePlanTraceLink,
)


CALENDAR = CnTradingCalendar()
START = date(2026, 1, 5)
END = date(2026, 1, 9)
SESSIONS = CALENDAR.sessions(START, END)
PREVIOUS = CALENDAR.previous_session(START)

PRICES = {
    PREVIOUS: (9.8, 10.0, 9.7, 9.9, 1_000_000),
    date(2026, 1, 5): (10.0, 10.6, 9.9, 10.3, 1_000_000),
    date(2026, 1, 6): (10.3, 11.5, 10.2, 11.2, 1_000_000),
    date(2026, 1, 7): (11.2, 12.3, 11.0, 12.0, 1_000_000),
    date(2026, 1, 8): (12.0, 12.2, 11.8, 12.1, 1_000_000),
    date(2026, 1, 9): (12.1, 12.4, 12.0, 12.3, 1_000_000),
}


def daily_bars(symbol: str = "600519", prices: dict[date, tuple] | None = None) -> list[DailyBar]:
    prices = prices or PRICES
    return [
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


def rules_for(symbol: str = "600519", prices: dict[date, tuple] | None = None) -> list:
    prices = prices or PRICES
    result = []
    for session in SESSIONS:
        previous = CALENDAR.previous_session(session)
        close = prices.get(previous, prices[PREVIOUS])[3]
        result.append(
            derive_rule(
                symbol,
                session,
                previous_close=Decimal(str(close)),
                st_status_verified=True,
                listing_status_verified=True,
                instrument_type_verified=True,
            )
        )
    return result


def data_view(
    *,
    granularity: str = "1d",
    daily: Iterable[DailyBar] | None = None,
    minute: Iterable[Bar] | None = None,
    rules: Iterable | None = None,
    actions=None,
    benchmark=None,
) -> InMemoryDataView:
    return InMemoryDataView.from_records(
        granularity=granularity,
        start_date=START,
        end_date=END,
        sessions=SESSIONS,
        bars=list(minute or []),
        daily=list(daily if daily is not None else daily_bars()),
        rules=list(rules if rules is not None else rules_for()),
        actions=actions or [],
        benchmark=benchmark or {},
    )


def full_session_minute_bars(
    symbol: str = "600519",
    *,
    date_: date = date(2026, 1, 5),
    price: float = 10.0,
    volume: int = 1_000_000,
) -> list[Bar]:
    bars = []
    for bar_time in CALENDAR.bar_times(date_, "5m"):
        bars.append(
            Bar(
                symbol=symbol,
                end_at=bar_time,
                session=date_,
                open=Decimal(str(price)),
                high=Decimal(str(price + 0.1)),
                low=Decimal(str(price - 0.1)),
                close=Decimal(str(price)),
                volume=Decimal(volume),
                amount=Decimal("0"),
                frequency="5m",
                source="test",
            )
        )
    return bars


def plan(
    *,
    symbol: str = "600519",
    plan_key: str = "plan-1:s1",
    entry_low: str = "10.0",
    entry_high: str = "10.2",
    stop_loss: str | None = "9.7",
    target_1: str | None = "11.0",
    target_2: str | None = "12.0",
    target_1_exit_ratio: str = "0.5",
    target_position_pct: str = "20",
    available_at: datetime = datetime(2026, 1, 2, 16, 0),
    requires_confirmation: bool = False,
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK,
    selection_trace_ref: str | None = None,
) -> ReplayPlan:
    return ReplayPlan(
        plan_key=plan_key,
        plan_id=plan_key.split(":", 1)[0],
        source_pipeline=source_pipeline,
        selection_trace_ref=selection_trace_ref,
        confirmation_trace_ref="trace-1",
        link_quality="explicit" if selection_trace_ref else "single_stock",
        source_revision=1,
        source_hash="sha256:source",
        symbol=symbol,
        scenario_id="s1",
        scenario_role="primary",
        available_at=available_at,
        valid_from=START,
        valid_until=END,
        entry_low=Decimal(entry_low),
        entry_high=Decimal(entry_high),
        entry_reference=Decimal(entry_high),
        stop_loss=Decimal(stop_loss) if stop_loss is not None else None,
        target_1=Decimal(target_1) if target_1 is not None else None,
        target_2=Decimal(target_2) if target_2 is not None else None,
        target_1_exit_ratio=Decimal(target_1_exit_ratio),
        order_valid_trading_days=2,
        max_hold_trading_days=10,
        target_position_pct=Decimal(target_position_pct),
        rank=1,
        requires_confirmation=requires_confirmation,
    )


def config(
    *,
    granularity: Granularity = Granularity.DAILY,
    initial_cash: str = "100000",
    risk_profile: RiskProfile = RiskProfile.BALANCED,
    source_pipeline: SourcePipeline = SourcePipeline.SINGLE_STOCK,
    fee_policy_ref: str = "cn-a-share-zero-cost.v1",
    corporate_action_policy: CorporateActionPolicy = CorporateActionPolicy.ASSUME_NONE,
    max_single: str = "100",
    max_total: str = "100",
    selection_links=None,
) -> ExperimentConfig:
    return ExperimentConfig(
        start_date=START,
        end_date=END,
        initial_cash=Decimal(initial_cash),
        granularity=granularity,
        price_basis="daily_declared_unadjusted" if granularity is Granularity.DAILY else "minute_unadjusted",
        fee_policy_ref=fee_policy_ref,
        risk_profile=risk_profile,
        source_pipeline=source_pipeline,
        missing_data_policy="strict",
        corporate_action_policy=corporate_action_policy,
        max_single_position_pct=Decimal(max_single),
        max_total_exposure_pct=Decimal(max_total),
        selection_links=selection_links or [],
    )


def zero_fee_policy() -> FeePolicy:
    return FeePolicy(
        version="test-zero",
        rules=[
            FeeRule(
                effective_from=date(2000, 1, 1),
                commission_bps=Decimal("0"),
                minimum_commission=Decimal("0"),
                transfer_fee_bps=Decimal("0"),
                stamp_duty_sell_bps=Decimal("0"),
                stamp_duty_buy_bps=Decimal("0"),
            )
        ],
    )


def gold_fee_policy() -> FeePolicy:
    return FeePolicy(
        version="test-gold",
        rules=[
            FeeRule(
                effective_from=date(2000, 1, 1),
                commission_bps=Decimal("0"),
                minimum_commission=Decimal("5"),
                transfer_fee_bps=Decimal("0"),
                stamp_duty_sell_bps=Decimal("5"),
                stamp_duty_buy_bps=Decimal("0"),
            )
        ],
    )


def make_artifact(
    *,
    plan_id: str = "plan-1",
    symbol: str = "600519",
    generation_status: str = "generated",
    selected_scenario: str = "s1",
    created_at: datetime = datetime(2026, 1, 2, 16, 0),
    valid_from: date = START,
    target_position_pct: str = "20",
    execution_status: str = "actionable",
) -> tuple[PricePlanArtifact, AccountPlanDecision]:
    scenario = ZoneScenario(
        scenario_id="s1",
        role="primary",
        base_rank=1,
        source_families=["price_structure"],
        anchor_refs=[],
        entry_low=Decimal("10.0"),
        entry_high=Decimal("10.2"),
        entry_reference=Decimal("10.2"),
        stop_loss=Decimal("9.7"),
        target_1=Decimal("11.0"),
        target_2=Decimal("12.0"),
        target_1_exit_ratio=Decimal("0.5"),
        reference_net_rr_1=Decimal("1.5"),
        reference_net_risk=Decimal("0.5"),
        reference_net_reward_1=Decimal("0.75"),
        reachability_band="near",
        price_relation="above",
        confirmation_rules=[
            RuleSpec(rule_type="recent_zone_touch", params={"bars": 2}, rule_version="v1")
        ],
        invalidation_rules=[
            RuleSpec(rule_type="close_at_or_below_stop", params={"stop_loss": 9.7}, rule_version="v1")
        ],
        confirmation_seed_bars=[],
        valid_from=valid_from,
        confirmation_deadline=END,
        confirmation_valid_trading_days=5,
        order_valid_trading_days=2,
        scenario_fingerprint="sha256:scenario",
    )
    artifact = PricePlanArtifact(
        plan_id=plan_id,
        revision=1,
        symbol=symbol,
        market="cn",
        side="long",
        timeframe="1d",
        requested_as_of=date(2026, 1, 2),
        actual_as_of_session=date(2026, 1, 2),
        generation_status=generation_status,
        scenarios=[scenario],
        observation_anchors=[],
        market_rules_snapshot_ref="rules-1",
        data_quality="sufficient",
        reason_codes=[],
        diagnostics={},
        algorithm_version="v1",
        params_version="params-v1",
        data_fingerprint="sha256:data",
        calculation_fingerprint=f"sha256:calc-{plan_id}",
        plan_fingerprint=f"sha256:plan-{plan_id}",
        created_at=created_at,
    )
    decision = AccountPlanDecision(
        decision_id=f"decision-{plan_id}",
        plan_id=plan_id,
        decision_scope="account",
        risk_profile="balanced",
        selected_scenario_id=selected_scenario,
        review_permission="allowed",
        confirmation_policy="none",
        target_position_pct=Decimal(target_position_pct),
        account_context_absent=True,
        execution_status=execution_status,
        reason_codes=[],
        input_fingerprint=f"sha256:input-{plan_id}",
        created_at=created_at + timedelta(minutes=5),
    )
    return artifact, decision


def seed_db(db: DatabaseManager, *, artifact=None, decision=None, prices=None) -> None:
    artifact = artifact or make_artifact()[0]
    decision = decision or make_artifact()[1]
    with db.get_session() as session:
        session.add(
            TradePlanArtifactRecord(
                plan_id=artifact.plan_id,
                revision=artifact.revision,
                symbol=artifact.symbol,
                market=artifact.market,
                timeframe=artifact.timeframe,
                requested_as_of=artifact.requested_as_of,
                actual_as_of_session=artifact.actual_as_of_session,
                generation_status=artifact.generation_status.value,
                valid_until=END,
                data_fingerprint=artifact.data_fingerprint,
                calculation_fingerprint=artifact.calculation_fingerprint,
                plan_fingerprint=artifact.plan_fingerprint,
                algorithm_version=artifact.algorithm_version,
                params_version=artifact.params_version,
                artifact_json=artifact.model_dump_json(),
                trace_ref="trace-1",
                created_at=artifact.created_at,
            )
        )
        session.add(
            AccountPlanDecisionRecord(
                decision_id=decision.decision_id,
                plan_id=decision.plan_id,
                selected_scenario_id=decision.selected_scenario_id,
                risk_profile=decision.risk_profile,
                execution_status=decision.execution_status,
                input_fingerprint=decision.input_fingerprint,
                artifact_json=decision.model_dump_json(),
                created_at=decision.created_at,
            )
        )
        session.add(
            TradePlanTraceLink(
                trace_ref="trace-1",
                plan_id=artifact.plan_id,
                linked_at=decision.created_at,
            )
        )
        for session_date, values in (prices or PRICES).items():
            session.add(
                StockDaily(
                    code=artifact.symbol,
                    date=session_date,
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=values[4],
                    amount=0,
                    data_source="test",
                )
            )
        session.commit()


def temp_db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(db._engine)
    return db
