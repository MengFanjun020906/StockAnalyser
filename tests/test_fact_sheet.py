import sys
from datetime import datetime
from unittest.mock import MagicMock

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.agent.candidate_experts_v2.fact_sheet import (
    build_fact_sheet,
    fact_sheet_cache_key,
    intraday_bucket,
)
from src.agent.candidate_experts_v2.schemas import FactSheet


def _bars(closes, *, vol=1000.0, turnover=2.0e7):
    """Build oldest→newest OHLCV dicts from a close series (high/low padded)."""
    rows = []
    for c in closes:
        rows.append(
            {
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": vol,
                "turnover": turnover,
            }
        )
    return rows


# --- determinism / reproducibility ---------------------------------------

def test_build_is_deterministic_and_reproducible():
    bars = _bars([10 + i * 0.1 for i in range(130)])
    a = build_fact_sheet("600000", bars)
    b = build_fact_sheet("600000", bars)
    assert a.model_dump() == b.model_dump()


def test_position_fields_from_fixed_bars():
    # rising series → close near top of range
    closes = [10 + i * 0.1 for i in range(130)]
    sheet = build_fact_sheet("600000", closes_to_df(closes))
    assert sheet.range_pct_120 is not None
    assert sheet.range_pct_120 > 0.9
    assert sheet.gain_5d is not None and sheet.gain_5d > 0
    assert sheet.bias_ma20 is not None and sheet.bias_ma20 > 0
    assert sheet.dist_to_high_20 is not None


def closes_to_df(closes):
    return _bars(closes)


def test_trend_state_bullish_on_uptrend():
    sheet = build_fact_sheet("600000", _bars([10 + i * 0.2 for i in range(80)]))
    assert sheet.trend_state == "bullish"


def test_trend_state_bearish_on_downtrend():
    sheet = build_fact_sheet("600000", _bars([40 - i * 0.2 for i in range(80)]))
    assert sheet.trend_state == "bearish"


def test_rsi_extremes():
    up = build_fact_sheet("600000", _bars([10 + i * 0.5 for i in range(40)]))
    assert up.rsi14 is not None and up.rsi14 > 80
    down = build_fact_sheet("600000", _bars([40 - i * 0.5 for i in range(40)]))
    assert down.rsi14 is not None and down.rsi14 < 20


def test_volume_ratio_spike():
    bars = _bars([10.0] * 40)
    bars[-1]["volume"] = 3000.0  # 3x prior average
    sheet = build_fact_sheet("600000", bars)
    assert sheet.volume_ratio is not None
    assert round(sheet.volume_ratio, 1) == 3.0


def test_liquidity_flag_respects_min_turnover():
    low = build_fact_sheet("600000", _bars([10.0] * 30, turnover=5.0e6), min_avg_turnover=1.0e7)
    assert low.liquidity_ok is False
    ok = build_fact_sheet("600000", _bars([10.0] * 30, turnover=5.0e7), min_avg_turnover=1.0e7)
    assert ok.liquidity_ok is True


def test_insufficient_bars_returns_safe_defaults():
    sheet = build_fact_sheet("600000", _bars([10.0]))
    assert isinstance(sheet, FactSheet)
    assert sheet.trend_state == "unknown"
    assert sheet.range_pct_120 is None
    assert "insufficient_daily_bars" in sheet.warnings


# --- Phase B passthrough --------------------------------------------------

def test_phase_b_fields_passthrough():
    sheet = build_fact_sheet(
        "600000",
        _bars([10.0] * 30),
        capital_direction="inflow",
        sector_name="半导体",
        sector_strength="strong",
        sector_rank_pct=0.05,
        leader_already_up=True,
    )
    assert sheet.capital_direction == "inflow"
    assert sheet.sector_name == "半导体"
    assert sheet.sector_strength == "strong"
    assert sheet.sector_rank_pct == 0.05
    assert sheet.leader_already_up is True


# --- red lines: threshold-gated (default off → no误杀) ---------------------

def test_red_lines_default_off_never_trigger():
    # bearish + sharp drop + outflow, but thresholds unset → both bools stay False
    bars = _bars([40 - i * 0.5 for i in range(60)])
    sheet = build_fact_sheet(
        "600000",
        bars,
        capital_direction="outflow",
        capital_metrics={"net_outflow_pct": 99.0},
    )
    assert sheet.capital_violent_outflow is False
    assert sheet.breakdown_accelerating is False


def test_violent_outflow_triggers_only_with_threshold():
    bars = _bars([10.0] * 30)
    sheet = build_fact_sheet(
        "600000",
        bars,
        capital_direction="outflow",
        capital_metrics={"net_outflow_pct": 12.0},
        violent_outflow_threshold=10.0,
    )
    assert sheet.capital_violent_outflow is True


def test_violent_outflow_not_triggered_below_threshold():
    bars = _bars([10.0] * 30)
    sheet = build_fact_sheet(
        "600000",
        bars,
        capital_direction="outflow",
        capital_metrics={"net_outflow_pct": 3.0},
        violent_outflow_threshold=10.0,
    )
    assert sheet.capital_violent_outflow is False


def test_violent_outflow_not_triggered_when_direction_not_outflow():
    bars = _bars([10.0] * 30)
    sheet = build_fact_sheet(
        "600000",
        bars,
        capital_direction="neutral",
        capital_metrics={"net_outflow_pct": 50.0},
        violent_outflow_threshold=10.0,
    )
    assert sheet.capital_violent_outflow is False


def test_breakdown_accel_triggers_with_threshold():
    bars = _bars([40 - i * 0.6 for i in range(60)])
    sheet = build_fact_sheet("600000", bars, breakdown_accel_threshold=2.0)
    assert sheet.trend_state == "bearish"
    assert sheet.breakdown_accelerating is True


def test_breakdown_accel_not_triggered_for_low_base_uptick():
    # low base, rising → bullish/neutral, not breakdown even with threshold set
    bars = _bars([10 + i * 0.1 for i in range(60)])
    sheet = build_fact_sheet("600000", bars, breakdown_accel_threshold=2.0)
    assert sheet.breakdown_accelerating is False


# --- cache key helper -----------------------------------------------------

def test_intraday_bucket_boundaries():
    assert intraday_bucket(datetime(2026, 5, 29, 9, 45)) == "b1"
    assert intraday_bucket(datetime(2026, 5, 29, 10, 5)) == "b2"
    assert intraday_bucket(datetime(2026, 5, 29, 11, 29)) == "b4"
    assert intraday_bucket(datetime(2026, 5, 29, 12, 0)) == "midday"
    assert intraday_bucket(datetime(2026, 5, 29, 13, 5)) == "b5"
    assert intraday_bucket(datetime(2026, 5, 29, 14, 59)) == "b8"
    assert intraday_bucket(datetime(2026, 5, 29, 15, 0)) == "eod"
    assert intraday_bucket(datetime(2026, 5, 29, 8, 0)) == "eod"


def test_fact_sheet_cache_key_format():
    key = fact_sheet_cache_key("600000", date="2026-05-29", now=datetime(2026, 5, 29, 9, 45))
    assert key == "600000:2026-05-29:b1"
