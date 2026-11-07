from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.agent.pricing.backtest import evaluate_price_plan_backtest
from src.agent.pricing.fingerprint import fingerprint
from src.agent.pricing.lifecycle import evaluate_scenario_lifecycle
from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.schemas import (
    GenerationStatus,
    MarketRuleSnapshot,
    PricePlanArtifact,
    PriceRelation,
    ReachabilityBand,
    RuleSpec,
    ScenarioRole,
    ZoneScenario,
)


def _scenario() -> ZoneScenario:
    seed_bars = [
        {
            "trade_date": date(2026, 8, day),
            "open": 102,
            "high": 103,
            "low": 101,
            "close": 102,
            "volume": 1000,
        }
        for day in range(3, 8)
    ]
    return ZoneScenario(
        scenario_id="scenario-test",
        role=ScenarioRole.PRIMARY,
        base_rank=1,
        source_families=["trend", "price_structure"],
        anchor_refs=["a1", "a2"],
        entry_low=Decimal("98"),
        entry_high=Decimal("100"),
        entry_reference=Decimal("100"),
        stop_loss=Decimal("95"),
        target_1=Decimal("105"),
        target_2=Decimal("110"),
        target_1_exit_ratio=Decimal("0.5"),
        reference_net_rr_1=Decimal("1.2"),
        reference_net_risk=Decimal("5"),
        reference_net_reward_1=Decimal("6"),
        reachability_band=ReachabilityBand.NEAR,
        price_relation=PriceRelation.ABOVE,
        confirmation_rules=[
            RuleSpec(rule_type="closed_bar_reversal_pattern", params={}, rule_version="v1")
        ],
        invalidation_rules=[
            RuleSpec(
                rule_type="close_at_or_below_stop",
                params={"stop_loss": Decimal("95")},
                rule_version="v1",
            )
        ],
        confirmation_seed_bars=seed_bars,
        valid_from=date(2026, 8, 11),
        confirmation_deadline=date(2026, 8, 17),
        confirmation_valid_trading_days=5,
        order_valid_trading_days=2,
        scenario_fingerprint=fingerprint({"scenario": "test"}),
    )


def _artifact(scenario: ZoneScenario) -> PricePlanArtifact:
    return PricePlanArtifact(
        plan_id="plan-test",
        revision=1,
        symbol="600001",
        requested_as_of=date(2026, 8, 10),
        actual_as_of_session=date(2026, 8, 10),
        generation_status=GenerationStatus.GENERATED,
        scenarios=[scenario],
        observation_anchors=[],
        market_rules_snapshot_ref="rules-test",
        data_quality="sufficient",
        reason_codes=[],
        algorithm_version="deterministic-price-plan.v1",
        params_version="price-plan-params.v1",
        data_fingerprint=fingerprint({"data": "test"}),
        calculation_fingerprint=fingerprint({"calculation": "test"}),
        plan_fingerprint=fingerprint({"plan": "test"}),
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        trace_ref="trace-test",
    )


def _rules() -> MarketRuleSnapshot:
    return MarketRuleSnapshot(
        snapshot_id="rules-test",
        venue="sse",
        board="main",
        trade_date=date(2026, 8, 10),
        tick_size=Decimal("0.01"),
        buy_lot_size=100,
        price_limit_ratio=Decimal("0.10"),
        previous_close=Decimal("100"),
        limit_up=Decimal("110"),
        limit_down=Decimal("90"),
        t_plus_one=True,
        st_status_verified=True,
        listing_status_verified=True,
        instrument_type="common_stock",
        instrument_type_verified=True,
    )


def test_structured_backtest_confirms_next_day_fill_and_two_targets() -> None:
    scenario = _scenario()
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},
        {"date": "2026-08-13", "open": 101, "high": 106, "low": 100, "close": 105, "volume": 1200},
        {"date": "2026-08-14", "open": 106, "high": 111, "low": 104, "close": 110, "volume": 1200},
    ]

    evaluation = evaluate_price_plan_backtest(_artifact(scenario), bars, market_rules=_rules())
    item = evaluation["scenario_evaluations"][0]

    assert evaluation["evaluation_status"] == "evaluated"
    assert evaluation["outcome"] == "WIN"
    assert item["touched_at"] == "2026-08-11"
    assert item["confirmed_at"] == "2026-08-11"
    assert item["filled_at"] == "2026-08-12"
    assert item["entry_price"] == 99.05
    assert item["exit_reason"] == "target_2"
    assert item["mae_pct"] <= 0
    assert item["mfe_pct"] > 10


def test_structured_backtest_uses_stop_first_on_ambiguous_exit_bar() -> None:
    scenario = _scenario().model_copy(update={"target_1_exit_ratio": Decimal("1")})
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},
        {"date": "2026-08-13", "open": 100, "high": 106, "low": 94, "close": 101, "volume": 1200},
    ]

    evaluation = evaluate_price_plan_backtest(_artifact(scenario), bars, market_rules=_rules())
    item = evaluation["scenario_evaluations"][0]

    assert evaluation["outcome"] == "LOSS"
    assert item["exit_reason"] == "stop_loss"
    assert item["ambiguous_bar"] is True


def test_structured_backtest_refuses_missing_historical_market_rules() -> None:
    evaluation = evaluate_price_plan_backtest(_artifact(_scenario()), [], market_rules=None)

    assert evaluation["evaluation_status"] == "unevaluable"
    assert evaluation["outcome"] is None
    assert evaluation["unevaluable_reason"] == "missing_market_rule_snapshot"


def test_structured_backtest_refuses_unknown_artifact_params_version() -> None:
    artifact = _artifact(_scenario()).model_copy(update={"params_version": "missing.v99"})

    evaluation = evaluate_price_plan_backtest(artifact, [], market_rules=_rules())

    assert evaluation["evaluation_status"] == "unevaluable"
    assert evaluation["unevaluable_reason"] == "unknown_params_version"


def test_structured_backtest_does_not_fill_one_price_limit_up() -> None:
    scenario = _scenario().model_copy(update={"entry_low": Decimal("108"), "entry_high": Decimal("110"), "entry_reference": Decimal("110"), "target_1": Decimal("115"), "target_2": Decimal("120")})
    bars = [
        {"date": "2026-08-11", "open": 109, "high": 110, "low": 107, "close": 110, "volume": 700},
        {"date": "2026-08-12", "open": 121, "high": 121, "low": 121, "close": 121, "volume": 1000},
        {"date": "2026-08-13", "open": 122, "high": 123, "low": 121, "close": 122, "volume": 1200},
    ]

    evaluation = evaluate_price_plan_backtest(_artifact(scenario), bars, market_rules=_rules())

    assert evaluation["scenario_evaluations"][0]["filled_at"] is None
    assert evaluation["scenario_evaluations"][0]["outcome"] == "NOT_FILLED"


def test_structured_backtest_selects_scenario_before_observing_outcome() -> None:
    first = _scenario().model_copy(
        update={
            "scenario_id": "scenario-first",
            "base_rank": 1,
            "entry_low": Decimal("80"),
            "entry_high": Decimal("82"),
            "entry_reference": Decimal("82"),
            "stop_loss": Decimal("75"),
            "target_1": Decimal("88"),
            "target_2": Decimal("92"),
        }
    )
    second = _scenario().model_copy(
        update={"scenario_id": "scenario-second", "base_rank": 2}
    )
    artifact = _artifact(first).model_copy(update={"scenarios": [first, second]})
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},
        {"date": "2026-08-13", "open": 101, "high": 106, "low": 100, "close": 105, "volume": 1200},
        {"date": "2026-08-14", "open": 106, "high": 111, "low": 104, "close": 110, "volume": 1200},
        {"date": "2026-08-17", "open": 109, "high": 110, "low": 108, "close": 109, "volume": 1200},
    ]

    neutral = evaluate_price_plan_backtest(artifact, bars, market_rules=_rules())
    account_selected = evaluate_price_plan_backtest(
        artifact,
        bars,
        market_rules=_rules(),
        selected_scenario_id="scenario-second",
    )

    assert neutral["policy_evaluation"]["selected_scenario_id"] == "scenario-first"
    assert neutral["outcome"] == "EXPIRED"
    assert account_selected["policy_evaluation"]["selected_scenario_id"] == "scenario-second"
    assert account_selected["outcome"] == "WIN"


def test_timeout_exit_does_not_assume_fill_on_one_price_limit_down() -> None:
    scenario = _scenario().model_copy(
        update={
            "target_1": Decimal("150"),
            "target_2": Decimal("160"),
            "target_1_exit_ratio": Decimal("1"),
        }
    )
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},
        {"date": "2026-08-13", "open": 90, "high": 90, "low": 90, "close": 90, "volume": 1000},
    ]

    evaluation = evaluate_price_plan_backtest(
        _artifact(scenario),
        bars,
        market_rules=_rules(),
        max_hold_trading_days=1,
    )

    assert evaluation["evaluation_status"] == "unevaluable"
    assert evaluation["unevaluable_reason"] == "timeout_exit_blocked_by_limit_down"


def test_order_window_open_below_stop_does_not_create_trade() -> None:
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 94, "high": 96, "low": 93, "close": 95, "volume": 1400},
    ]

    evaluation = evaluate_price_plan_backtest(
        _artifact(_scenario()),
        bars,
        market_rules=_rules(),
    )
    item = evaluation["scenario_evaluations"][0]

    assert evaluation["outcome"] == "INVALIDATED"
    assert item["filled_at"] is None
    assert item["exit_reason"] == "opened_at_or_below_stop"


def test_intraday_fill_and_invalidation_order_is_unresolved() -> None:
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 103, "high": 104, "low": 94, "close": 94.5, "volume": 1400},
    ]

    evaluation = evaluate_price_plan_backtest(
        _artifact(_scenario()),
        bars,
        market_rules=_rules(),
    )
    item = evaluation["scenario_evaluations"][0]

    assert evaluation["outcome"] == "UNRESOLVED"
    assert item["filled_at"] is None
    assert item["ambiguous_bar"] is True
    assert item["exit_reason"] == "intraday_fill_and_invalidation_order_unknown"


def test_entry_day_invalidation_exits_on_next_tradable_day_for_t_plus_one() -> None:
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 99, "high": 100, "low": 94, "close": 94.5, "volume": 1400},
        {"date": "2026-08-13", "open": 93, "high": 95, "low": 92, "close": 94, "volume": 1200},
    ]

    evaluation = evaluate_price_plan_backtest(
        _artifact(_scenario()),
        bars,
        market_rules=_rules(),
    )
    item = evaluation["scenario_evaluations"][0]

    assert item["filled_at"] == "2026-08-12"
    assert item["exit_at"] == "2026-08-13"
    assert item["exit_reason"] == "t_plus_one_stop_after_entry_day_invalidation"
    assert evaluation["outcome"] == "LOSS"


def test_scenario_lifecycle_reconciliation_is_idempotent(tmp_path) -> None:
    from src.storage import DatabaseManager

    scenario = _scenario()
    artifact = _artifact(scenario)
    bars = [
        {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
        {"date": "2026-08-12", "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 1000},
    ]
    events = evaluate_scenario_lifecycle(scenario, bars)
    assert [item.event_type for item in events] == ["touched", "technical_confirmed"]

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-lifecycle.db'}")
    try:
        repo = PricePlanRepository(db)
        repo.save_market_rules(_rules())
        stored = repo.save_artifact(artifact)
        first = repo.reconcile_lifecycle(stored, bars)
        first_events = repo.list_lifecycle_events(stored.plan_id)
        second = repo.reconcile_lifecycle(stored, bars)
        second_events = repo.list_lifecycle_events(stored.plan_id)

        assert first[0].state == second[0].state == "confirmed"
        assert first[0].state_version == second[0].state_version == 3
        assert len(first_events) == len(second_events) == 3
    finally:
        DatabaseManager.reset_instance()


def test_confirmed_scenario_can_later_be_invalidated() -> None:
    events = evaluate_scenario_lifecycle(
        _scenario(),
        [
            {"date": "2026-08-11", "open": 100, "high": 101, "low": 97.5, "close": 100.5, "volume": 700},
            {"date": "2026-08-12", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"date": "2026-08-13", "open": 96, "high": 97, "low": 94, "close": 95, "volume": 1300},
        ],
    )

    assert [item.event_type for item in events] == [
        "touched",
        "technical_confirmed",
        "invalidated",
    ]


def test_suspended_bar_does_not_consume_confirmation_validity() -> None:
    events = evaluate_scenario_lifecycle(
        _scenario(),
        [
            {"date": "2026-08-11", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 0},
            {"date": "2026-08-12", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 1000},
            {"date": "2026-08-13", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 1000},
            {"date": "2026-08-14", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 1000},
            {"date": "2026-08-17", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 1000},
            {"date": "2026-08-18", "open": 105, "high": 106, "low": 104, "close": 105, "volume": 1000},
        ],
    )

    assert events[-1].event_type == "expired"
    assert events[-1].occurred_on == date(2026, 8, 18)
