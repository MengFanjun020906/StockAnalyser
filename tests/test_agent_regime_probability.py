from datetime import date, timedelta

from src.agent.regime import MarketBar
from src.agent.regime_probability import (
    build_path_profile,
    build_regime_probability,
    build_reentry_reference,
    compute_regime_return_frame,
)
from src.agent.stock_selection import _pricing_entry_zone, _regime_probability_report_note
from src.agent.tools.market_tools import ALL_MARKET_TOOLS


def _bars(count: int = 180) -> list[MarketBar]:
    start = date(2025, 1, 1)
    bars: list[MarketBar] = []
    price = 100.0
    for idx in range(count):
        cycle = idx % 20
        price *= 1.004 if cycle < 12 else 0.996
        high = price * (1.01 + (0.002 if cycle in {4, 5} else 0))
        low = price * (0.985 - (0.004 if cycle in {14, 15} else 0))
        bars.append(
            MarketBar(
                date=start + timedelta(days=idx),
                open=price * 0.997,
                high=high,
                low=low,
                close=price,
                volume=1_000_000 + idx,
            )
        )
    return bars


def test_regime_probability_builds_forward_windows_and_reentry():
    frame = compute_regime_return_frame(
        _bars(),
        regime_classifier=lambda _history: "range_bound",
        windows=[7, 30],
        min_classifier_bars=40,
    )
    probability = build_regime_probability(frame, regime="range_bound", windows=[7, 30], min_samples=3)
    profile = build_path_profile(frame, regime="range_bound", window=30)
    reentry = build_reentry_reference(probability, current_price=100.0, window=30)

    assert probability["sample_count"] > 0
    assert probability["windows"]["7"]["n"] > 0
    assert probability["windows"]["30"]["effective_n"] < probability["windows"]["30"]["n"]
    assert probability["windows"]["30"]["p_below_current"] is not None
    assert profile["n"] > 0
    assert "pct_dip_then_up" in profile
    assert reentry["current_price"] == 100.0
    if reentry["reentry_price"] is not None:
        assert reentry["reentry_price"] < 100.0


def test_regime_probability_marks_low_confidence_for_sparse_samples():
    frame = compute_regime_return_frame(
        _bars(80),
        regime_classifier=lambda _history: "risk_off",
        windows=[30],
        min_classifier_bars=40,
    )
    probability = build_regime_probability(frame, regime="risk_off", windows=[30], min_samples=999)

    assert probability["windows"]["30"]["low_confidence"] is True


def test_regime_forward_probability_tool_is_registered():
    names = {tool.name for tool in ALL_MARKET_TOOLS}

    assert "get_regime_forward_probability" in names


def test_pricing_fallback_uses_reentry_as_weak_reference_only():
    regime_probability = {
        "status": "ok",
        "brief": "30d median=-1.2%, p_below_current=62%",
        "windows": {"30": {"low_confidence": True}},
        "reentry_reference": {"reentry_price": 96.8, "low_confidence": True},
    }

    entry_zone = _pricing_entry_zone(
        "Mean_Reversion_Pullback",
        {"mean_reversion_anchor": {"reason": "回踩 20 日线"}},
        regime_probability=regime_probability,
    )
    note = _regime_probability_report_note({"regime_probability": regime_probability})

    assert "96.8" in entry_zone
    assert "弱参考" in entry_zone
    assert "量价确认" in entry_zone
    assert "买回/回踩参考价=96.8" in note
    assert "不能单独支持开仓" in note
