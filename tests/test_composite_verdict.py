from src.agent.scoring.composite_verdict import evaluate_composite_verdict
from src.agent.scoring.position_sizing import calculate_position_sizing
from src.schemas.composite_verdict import (
    ChanFeatureSnapshot,
    ChanSignal,
    CompositeScoreSnapshot,
    S4FeatureSnapshot,
)


def test_chan_only_snapshot_stays_shadow_and_does_not_emit_full_score():
    snapshot = CompositeScoreSnapshot(
        symbol="600519",
        as_of="2026-08-05",
        quote_basis="eod_current",
        s4=S4FeatureSnapshot(
            missing_features=["region_pass", "resonance_count", "golden_cross_today", "all_ok"]
        ),
        chan=ChanFeatureSnapshot(
            signals=[
                ChanSignal(name="first_buy", timeframe="1d", triggered=True),
                ChanSignal(name="bottom_exhaustion", timeframe="1d", triggered=True),
            ]
        ),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.verdict_status == "not_evaluable"
    assert verdict.score is None
    assert verdict.scale_max == 20
    assert verdict.components.chan.score == 7
    assert verdict.components.s4.score is None
    assert verdict.shadow_mode is True
    assert verdict.entry_permission == "not_evaluable"
    assert verdict.target_position_cap_pct is None


def test_complete_snapshot_requires_first_buy_for_strong_confirmation():
    complete_s4 = S4FeatureSnapshot(
        status="ok",
        region_pass=True,
        resonance_count=4,
        volume_ratio_5d=1.6,
        volatility_pct=5.0,
        gain_10d_pct=8.0,
        golden_cross_today=True,
        all_ok=True,
    )
    common = {
        "symbol": "600519",
        "as_of": "2026-08-05",
        "quote_basis": "eod_current",
        "s4": complete_s4,
    }

    strong = evaluate_composite_verdict(
        CompositeScoreSnapshot(
            **common,
            chan=ChanFeatureSnapshot(
                signals=[
                    ChanSignal(name="first_buy", timeframe="1d", triggered=True),
                    ChanSignal(name="bottom_exhaustion", timeframe="1d", triggered=True),
                    ChanSignal(name="weekly_confirmation", timeframe="1w", triggered=True),
                ]
            ),
        )
    )
    without_first_buy = evaluate_composite_verdict(
        CompositeScoreSnapshot(
            **common,
            chan=ChanFeatureSnapshot(
                signals=[
                    ChanSignal(name="second_buy", timeframe="1d", triggered=True),
                    ChanSignal(name="bottom_exhaustion", timeframe="1d", triggered=True),
                    ChanSignal(name="weekly_confirmation", timeframe="1w", triggered=True),
                    ChanSignal(name="third_buy", timeframe="1d", triggered=True),
                ]
            ),
        )
    )

    assert strong.components.s4.score == 10
    assert strong.components.chan.score == 8
    assert strong.score == 18
    assert strong.signal_level == "strong_confirmed"
    assert strong.entry_permission == "allowed"
    assert without_first_buy.score == 19
    assert without_first_buy.signal_level == "buy_valid"


def test_exit_warning_is_independent_and_duplicate_chan_signals_score_once():
    snapshot = CompositeScoreSnapshot(
        symbol="600519",
        as_of="2026-08-05",
        holding=True,
        s4=S4FeatureSnapshot(missing_features=["region_pass"]),
        chan=ChanFeatureSnapshot(
            signals=[
                ChanSignal(name="first_buy", timeframe="1d", triggered=True),
                ChanSignal(name="first_buy", timeframe="1w", triggered=True),
                ChanSignal(name="top_divergence", timeframe="1d", triggered=True),
                ChanSignal(name="top_exhaustion", timeframe="1d", triggered=True),
            ]
        ),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.components.chan.score == 0
    assert verdict.exit_warning.level == 3
    assert verdict.exit_warning.items == ["top_divergence", "top_exhaustion"]
    assert verdict.required_holding_action == "reduce"
    assert verdict.score is None


def test_sell_points_are_exit_evidence_not_negative_buy_score():
    snapshot = CompositeScoreSnapshot(
        symbol="600519",
        as_of="2026-08-05",
        holding=True,
        s4=S4FeatureSnapshot(missing_features=["region_pass"]),
        chan=ChanFeatureSnapshot(signals=[
            ChanSignal(name="second_buy", timeframe="1d", triggered=True),
            ChanSignal(name="third_sell", timeframe="1d", triggered=True),
        ]),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.components.chan.score == 4
    assert verdict.exit_warning.level == 2
    assert verdict.exit_warning.items == ["third_sell"]


def test_insufficient_chan_data_is_not_scored_as_zero():
    snapshot = CompositeScoreSnapshot(
        symbol="600519",
        as_of="2026-08-05",
        chan=ChanFeatureSnapshot(status="insufficient"),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.components.chan.status == "not_evaluable"
    assert verdict.components.chan.score is None
    assert "chan_signals" in verdict.missing_features


def test_provisional_weekly_confirmation_is_preserved_but_not_scored():
    snapshot = CompositeScoreSnapshot(
        symbol="600519",
        as_of="2026-08-05",
        chan=ChanFeatureSnapshot(
            status="partial",
            signals=[
                ChanSignal(name="first_buy", timeframe="1d", triggered=True),
                ChanSignal(
                    name="weekly_confirmation",
                    timeframe="1w",
                    triggered=True,
                    metrics={"provisional": True},
                ),
            ],
        ),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.components.chan.status == "partial"
    assert verdict.components.chan.score == 5
    assert "weekly_confirmation" not in verdict.components.chan.breakdown


def test_incomplete_week_exposes_provisional_strategy4_score_without_promoting_it():
    snapshot = CompositeScoreSnapshot(
        symbol="600330",
        as_of="2026-08-11",
        quote_basis="latest_trading_day",
        s4=S4FeatureSnapshot(
            status="partial",
            region_pass=False,
            resonance_count=2,
            volume_ratio_5d=0.78635,
            volatility_pct=8.422227,
            gain_10d_pct=25.404531,
            golden_cross_today=False,
            all_ok=False,
            weekly_provisional=True,
        ),
        chan=ChanFeatureSnapshot(
            status="partial",
            signals=[
                ChanSignal(name="second_sell", timeframe="1d", triggered=True),
                ChanSignal(
                    name="weekly_confirmation",
                    timeframe="1w",
                    triggered=True,
                    metrics={"confirmed_signal": "second_buy", "provisional": True},
                ),
            ],
        ),
    )

    verdict = evaluate_composite_verdict(snapshot)

    assert verdict.verdict_status == "not_evaluable"
    assert verdict.score is None
    assert verdict.provisional_score == 0
    assert verdict.components.s4.score is None
    assert verdict.components.s4.provisional_score == 0
    assert verdict.components.s4.provisional_breakdown == {
        "region": 0,
        "resonance": 0,
        "volume_ratio": 0,
        "volatility": 0,
        "gain_10d": 0,
        "golden_cross": 0,
    }
    assert verdict.components.chan.score == 0
    assert verdict.formalization_reason_codes == ["S4_WEEKLY_BAR_UNCONFIRMED"]
    assert "BACKTEST_UNAVAILABLE" in verdict.quality_warning_codes


def test_position_sizing_uses_ratio_internally_and_percent_at_gate_boundary():
    features = S4FeatureSnapshot(
        status="ok",
        all_ok=True,
        resonance_count=4,
        volume_ratio_5d=1.6,
    )

    sizing = calculate_position_sizing(
        features,
        {"regime": "trending_up", "volatility_bucket": "low"},
    )

    assert sizing["position_tier"] == "S"
    assert sizing["target_position_cap_ratio"] == 0.2
    assert sizing["target_position_cap_pct"] == 20.0
