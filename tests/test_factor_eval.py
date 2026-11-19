# -*- coding: utf-8 -*-
"""Offline tests for the unified factor hit-evaluation protocol."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.factor_eval.config import load_registry
from src.backtest.factor_eval.data import EvaluationDataError, TushareEvaluationSource
from src.backtest.factor_eval.evaluator import (
    _build_observations,
    _build_panel,
    _coverage_metrics,
    _decide_verdict,
    _entry_limit_ratio,
    _feature_matrices,
    _valuation_pe_mask,
    _Panel,
    evaluate_factor,
    select_cohort_dates,
)
from src.backtest.factor_eval.metrics import (
    bh_fdr,
    minimum_detectable_effect,
    monotonicity_score,
    newey_west_t,
    quintile_means,
)


def test_registry_defaults_to_000985_and_versions_are_set():
    registry = load_registry()
    assert registry.defaults.benchmark == "000985.CSI"
    assert registry.factors["momentum_5d"].benchmark == "000985.CSI"
    assert registry.factors["momentum_5d"].version == "1.0.0"
    assert registry.factors["momentum_5d"].definition_hash


def test_select_cohort_dates_is_non_overlapping():
    dates = [f"202601{day:02d}" for day in range(1, 21)]
    cohorts = select_cohort_dates(dates, 5)
    assert cohorts == dates[::5]
    gaps = [dates.index(b) - dates.index(a) for a, b in zip(cohorts, cohorts[1:])]
    assert all(gap == 5 for gap in gaps)


def test_newey_west_t_and_mde_are_finite():
    values = [0.02, 0.03, 0.01, 0.04, 0.02, 0.03, 0.05, 0.01]
    assert newey_west_t(values, 4) is not None
    assert minimum_detectable_effect(float(np.std(values, ddof=1)), len(values)) is not None
    assert newey_west_t([0.1], 4) is None


def test_quintile_monotonicity_respects_direction():
    frame = pd.DataFrame(
        {
            "factor": list(range(100)),
            "excess_return": [value * 0.001 for value in range(100)],
        }
    )
    means = quintile_means(frame, "factor", "excess_return", groups=5)
    assert means is not None
    assert monotonicity_score(means, "positive") == pytest.approx(1.0)
    assert monotonicity_score(means, "negative") == pytest.approx(-1.0)


def test_bh_fdr_marks_only_small_pvalues():
    flags = bh_fdr([0.001, 0.02, 0.5, 0.8], alpha=0.10)
    assert flags[0] is True
    assert flags[-1] is False


def _strong_metrics() -> dict:
    return {
        "benchmark_coverage": 1.0,
        "n_decision_dates": 120,
        "universe_observations": 10_000,
        "stocks_per_cohort_mean": 100.0,
        "coverage": 0.90,
        "executability": 0.90,
        "mde": 0.01,
        "regime_counts": {"bull|low_vol": 6, "bear|low_vol": 6},
        "adj_missing_rate": 0.0,
        "ic_values": [0.05] * 24,
        "mean_rank_ic": 0.05,
        "icir": 0.50,
        "nw_t": 2.5,
        "positive_ic_ratio": 0.60,
        "monotonicity": 0.90,
        "long_short_mean": 0.01,
        "long_short_ci": (0.001, 0.02),
        "mean_partial_ic": 0.03,
        "partial_t": 2.2,
        "month_positive_ratio": 0.70,
        "regime_mean_ic": {"bull|low_vol": 0.03, "bear|low_vol": 0.02},
        "industry_lag_risk": False,
        "degenerate_factor": False,
    }


def test_survivorship_bias_blocks_pass():
    registry = load_registry()
    spec = registry.factors["momentum_5d"]
    verdict, reasons = _decide_verdict(
        metrics=_strong_metrics(),
        spec=spec,
        defaults=registry.defaults,
        survivorship_bias=True,
    )
    assert verdict == "not_evaluable"
    assert "survivorship" in reasons

    verdict, reasons = _decide_verdict(
        metrics=_strong_metrics(),
        spec=spec,
        defaults=registry.defaults,
        survivorship_bias=False,
    )
    assert verdict == "pass"
    assert reasons == []


def test_insufficient_power_is_not_evaluable_not_fail():
    registry = load_registry()
    spec = registry.factors["momentum_5d"]
    metrics = _strong_metrics()
    metrics["mde"] = 0.20
    verdict, reasons = _decide_verdict(
        metrics=metrics,
        spec=spec,
        defaults=registry.defaults,
        survivorship_bias=False,
    )
    assert verdict == "not_evaluable"
    assert "insufficient_power" in reasons


def test_limit_ratio_matches_board_rules():
    assert _entry_limit_ratio("600000", "20260105", st=False) == pytest.approx(0.10)
    assert _entry_limit_ratio("600000", "20260105", st=True) == pytest.approx(0.05)
    assert _entry_limit_ratio("300750", "20260105", st=False) == pytest.approx(0.20)
    assert _entry_limit_ratio("688981", "20260105", st=False) == pytest.approx(0.20)


def test_limit_up_open_is_not_executable():
    calendar = [f"202601{day:02d}" for day in range(1, 13)]
    codes = ["600001", "600002", "600003"]
    rows = []
    for index, day in enumerate(calendar):
        for offset, code in enumerate(codes):
            close = 10.0 + index * 0.1 + offset
            rows.append(
                {
                    "date": day,
                    "code": code,
                    "open": close,
                    "high": close,
                    "close": close,
                    "adj_open": close,
                    "adj_high": close,
                    "adj_close": close,
                    "vol": 1000.0,
                    "turnover_rate": 1.0,
                    "circ_mv": 100000.0,
                    "industry": "bank",
                    "list_date": "20100101",
                    "is_st": False,
                    "is_a_share": True,
                }
            )
    # Make the entry day of 600001 a one-price limit-up: entry open = prev close * 1.10.
    entry_day = calendar[6]
    prev_close = 10.0 + 5 * 0.1
    for row in rows:
        if row["date"] == entry_day and row["code"] == "600001":
            row["open"] = prev_close * 1.10
            row["adj_open"] = prev_close * 1.10
    panel = _Panel(frame=pd.DataFrame(rows), calendar=calendar, stock_basic=pd.DataFrame())
    registry = load_registry()
    observations, _ = _build_observations(
        panel=panel,
        calendar=calendar,
        cohort_dates=[calendar[5]],
        spec=registry.factors["momentum_5d"],
        horizon=5,
    )
    assert observations[observations["code"] == "600001"].empty
    assert not observations[observations["code"] == "600002"].empty


class _FakeSource:
    def __init__(self, days: int = 90, include_delisted: bool = False, provide_benchmark: bool = False):
        self.days = [f"2026{month:02d}{day:02d}" for month in range(1, 13) for day in range(1, 29)]
        self.days = [day for day in self.days if pd.Timestamp(day).weekday() < 5][:days]
        self.requested_benchmark = None
        self.include_delisted = include_delisted
        self.provide_benchmark = provide_benchmark

    def load_calendar(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def load_stock_basic(self):
        rows = {
            "ts_code": ["600001.SH", "600002.SH"],
            "name": ["Alpha", "Beta"],
            "industry": ["bank", "bank"],
            "market": ["主板", "主板"],
            "list_date": ["20100101", "20100101"],
            "delist_date": ["", ""],
        }
        if self.include_delisted:
            rows["ts_code"].append("600003.SH")
            rows["name"].append("Gamma退")
            rows["industry"].append("")
            rows["market"].append("主板")
            rows["list_date"].append("20100101")
            rows["delist_date"].append("20250105")
            rows["list_status"] = ["L", "L", "D"]
        return pd.DataFrame(rows)

    def load_industry_snapshot(self):
        return pd.DataFrame(
            {
                "ts_code": ["600001.SH", "600002.SH", "600003.SH"],
                "l1_name": ["银行", "银行", "银行"],
                "in_date": ["20100101", "20100101", "20100101"],
            }
        )

    def load_bars(self, day):
        return pd.DataFrame(
            {
                "ts_code": ["600001.SH", "600002.SH"],
                "trade_date": [day, day],
                "open": [10.0, 10.0],
                "high": [10.2, 10.2],
                "low": [9.8, 9.8],
                "close": [10.1, 10.1],
                "pre_close": [10.0, 10.0],
                "pct_chg": [1.0, 1.0],
                "vol": [1000.0, 1000.0],
                "amount": [10000.0, 10000.0],
            }
        )

    def load_adj_factors(self, day):
        return pd.DataFrame({"ts_code": ["600001.SH", "600002.SH"], "trade_date": [day, day], "adj_factor": [1.0, 1.0]})

    def load_basics(self, day):
        return pd.DataFrame(
            {
                "ts_code": ["600001.SH", "600002.SH"],
                "trade_date": [day, day],
                "turnover_rate": [1.0, 1.0],
                "turnover_rate_f": [1.0, 1.0],
                "volume_ratio": [1.0, 1.0],
                "pe_ttm": [10.0, 10.0],
                "pb": [1.0, 1.0],
                "total_mv": [100000.0, 100000.0],
                "circ_mv": [100000.0, 100000.0],
            }
        )

    def load_benchmark(self, code, start, end):
        self.requested_benchmark = code
        if not self.provide_benchmark:
            raise EvaluationDataError("benchmark unavailable")
        days = [day for day in self.days if start <= day <= end]
        return pd.DataFrame(
            {"trade_date": days, "open": [100.0] * len(days), "close": [101.0] * len(days)}
        )


def test_benchmark_missing_is_explicit_and_not_substituted():
    source = _FakeSource(days=90)
    calendar = source.days
    report = evaluate_factor(
        "momentum_5d",
        calendar[40],
        calendar[80],
        source=source,
    )
    assert report["verdict"] == "not_evaluable"
    assert "benchmark_missing" in report["reasons"]
    assert source.requested_benchmark == "000985.CSI"


def test_load_stock_basic_merges_listed_and_delisted(tmp_path):
    calls = []

    def fake_query(api, params=None, fields="", timeout=30):
        calls.append((api, dict(params or {})))
        if (params or {}).get("list_status") == "L":
            return pd.DataFrame(
                {
                    "ts_code": ["600001.SH"],
                    "name": ["Alpha"],
                    "industry": ["bank"],
                    "market": ["主板"],
                    "list_date": ["20100101"],
                    "delist_date": [None],
                    "list_status": ["L"],
                }
            )
        return pd.DataFrame(
            {
                "ts_code": ["600002.SH"],
                "name": ["Beta退"],
                "industry": [None],
                "market": ["主板"],
                "list_date": ["20000101"],
                "delist_date": ["20230630"],
                "list_status": ["D"],
            }
        )

    source = TushareEvaluationSource(cache_dir=tmp_path, query=fake_query)
    pool = source.load_stock_basic()
    assert set(pool["ts_code"]) == {"600001.SH", "600002.SH"}
    assert len(calls) == 2
    assert pool.set_index("ts_code").loc["600002.SH", "delist_date"] == "20230630"
    assert pool.set_index("ts_code").loc["600001.SH", "delist_date"] == ""

    # Second call must reuse the CSV cache instead of hitting Tushare again.
    source.load_stock_basic()
    assert len(calls) == 2


def test_industry_snapshot_respects_in_date():
    class _PanelSource:
        def load_bars(self, day):
            return pd.DataFrame(
                {
                    "ts_code": ["600001.SH"],
                    "trade_date": [day],
                    "open": [10.0],
                    "high": [10.0],
                    "low": [10.0],
                    "close": [10.0],
                    "vol": [100.0],
                    "amount": [1000.0],
                }
            )

        def load_adj_factors(self, day):
            return pd.DataFrame({"ts_code": ["600001.SH"], "adj_factor": [1.0]})

        def load_basics(self, day):
            return pd.DataFrame({"ts_code": ["600001.SH"], "turnover_rate": [1.0], "circ_mv": [1000.0]})

    stock_basic = pd.DataFrame(
        {
            "ts_code": ["600001.SH"],
            "name": ["Alpha"],
            "industry": [""],
            "market": ["主板"],
            "list_date": ["20100101"],
            "delist_date": [""],
        }
    )
    snapshot = pd.DataFrame(
        {"ts_code": ["600001.SH"], "l1_name": ["银行"], "in_date": ["20220110"]}
    )
    panel = _build_panel(_PanelSource(), ["20220105", "20220111"], stock_basic, snapshot)
    frame = panel.frame
    assert frame.loc[frame["date"] == "20220105", "industry_used"].isna().all()
    assert frame.loc[frame["date"] == "20220111", "industry_used"].eq("银行").all()


def _delisted_panel(delist_date: str, last_bar_index: int = 7) -> _Panel:
    calendar = [f"202601{day:02d}" for day in range(1, 16)]
    rows = []
    for index, day in enumerate(calendar):
        rows.append(
            {
                "date": day,
                "code": "600001",
                "open": 10.0 + index * 0.1 - 0.05,
                "high": 10.2 + index * 0.1,
                "close": 10.0 + index * 0.1,
                "adj_open": 10.0 + index * 0.1 - 0.05,
                "adj_high": 10.2 + index * 0.1,
                "adj_close": 10.0 + index * 0.1,
                "vol": 1000.0,
                "turnover_rate": 1.0,
                "circ_mv": 100000.0,
                "industry": "bank",
                "list_date": "20100101",
                "is_st": False,
                "is_a_share": True,
            }
        )
        if index <= last_bar_index:
            close = 10.0 if index < last_bar_index else 10.5
            rows.append(
                {
                    "date": day,
                    "code": "600003",
                    "open": close - 0.05,
                    "high": close + 0.1,
                    "close": close,
                    "adj_open": close - 0.05,
                    "adj_high": close + 0.1,
                    "adj_close": close,
                    "vol": 1000.0,
                    "turnover_rate": 1.0,
                    "circ_mv": 100000.0,
                    "industry": "bank",
                    "list_date": "20100101",
                    "is_st": False,
                    "is_a_share": True,
                }
            )
    stock_basic = pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600003.SH"],
            "list_date": ["20100101", "20100101"],
            "delist_date": ["", delist_date],
            "list_status": ["L", "D"],
        }
    )
    return _Panel(frame=pd.DataFrame(rows), calendar=calendar, stock_basic=stock_basic)


def test_delisted_stock_exits_at_last_tradable_close():
    panel = _delisted_panel(delist_date="20260109", last_bar_index=7)
    registry = load_registry()
    observations, _ = _build_observations(
        panel=panel,
        calendar=panel.calendar,
        cohort_dates=[panel.calendar[5]],
        spec=registry.factors["momentum_5d"],
        horizon=5,
        window_start="20260101",
        window_end="20260131",
    )
    row = observations[observations["code"] == "600003"]
    assert len(row) == 1
    assert bool(row["delisted_exit"].iloc[0]) is True
    assert row["forward_return"].iloc[0] == pytest.approx(10.5 / 9.95 - 1.0)


def test_delisted_stock_before_entry_is_counted_not_tradable():
    panel = _delisted_panel(delist_date="20260105", last_bar_index=3)
    registry = load_registry()
    observations, stats = _build_observations(
        panel=panel,
        calendar=panel.calendar,
        cohort_dates=[panel.calendar[5]],
        spec=registry.factors["momentum_5d"],
        horizon=5,
        window_start="20260101",
        window_end="20260131",
    )
    assert observations[observations["code"] == "600003"].empty
    assert stats[0]["delisted_skipped"] == 1


def test_survivorship_bias_is_cleared_when_pool_has_delisted():
    source = _FakeSource(days=90, include_delisted=True, provide_benchmark=True)
    calendar = source.days
    report = evaluate_factor(
        "momentum_5d",
        calendar[40],
        calendar[80],
        source=source,
    )
    assert report["survivorship_bias"] is False
    assert report["sample"]["delisted_sample_count"] == 1
    assert "survivorship" not in report["reasons"]


def test_reversal_factor_is_negated_momentum_with_positive_direction():
    registry = load_registry()
    reversal = registry.factors["reversal_5d"]
    assert reversal.kind == "reversal"
    assert reversal.direction == "positive"
    assert reversal.lookback == 5

    rows = [
        {
            "date": f"202601{index + 1:02d}",
            "code": "600001",
            "adj_close": 10.0 + index,
            "adj_high": 10.5 + index,
            "turnover_rate": 1.0,
        }
        for index in range(12)
    ]
    frame = pd.DataFrame(rows)
    momentum_matrix = _feature_matrices(frame, registry.factors["momentum_5d"])["factor"]
    reversal_matrix = _feature_matrices(frame, reversal)["factor"]
    pd.testing.assert_frame_equal(reversal_matrix, -momentum_matrix)


def _valuation_frame(pe_by_code: dict, industry_by_code: dict, day: str = "20260101") -> pd.DataFrame:
    rows = []
    for code, pe in pe_by_code.items():
        rows.append(
            {
                "date": day,
                "code": code,
                "adj_close": 10.0,
                "adj_high": 10.5,
                "turnover_rate": 1.0,
                "pe_ttm": pe,
                "industry": industry_by_code[code],
            }
        )
    return pd.DataFrame(rows)


def test_valuation_pe_percentile_is_industry_neutral_and_drops_non_positive_pe():
    """The factor is an in-industry PE percentile, not a market-wide raw PE."""

    registry = load_registry()
    spec = registry.factors["pe_ttm_low"]
    assert spec.kind == "valuation_pe"
    assert spec.direction == "negative"

    pe_by_code = {
        "600001": 4.0,
        "600002": 5.0,
        "600003": 6.0,
        "600004": 7.0,
        "600005": 8.0,
        "600006": 9.0,
        # PE <= 0 is "not meaningful", never an extreme cheap value.
        "600009": -3.0,
        "600008": float("nan"),
        # Only 3 peers: below the industry sample floor.
        "600010": 10.0,
        "600011": 11.0,
        "600012": 12.0,
    }
    industry_by_code = {code: "银行" for code in pe_by_code if code < "600010"}
    industry_by_code.update({code: "科技" for code in ("600010", "600011", "600012")})
    factor = _feature_matrices(_valuation_frame(pe_by_code, industry_by_code), spec)["factor"]

    # Six valid bank PEs -> evenly spaced percentiles; the -3.0 row is not
    # ranked (otherwise the cheapest bank would sit at 1/7 instead of 1/6).
    assert factor.loc["20260101", "600001"] == pytest.approx(1.0 / 6.0)
    assert factor.loc["20260101", "600006"] == pytest.approx(1.0)
    assert pd.isna(factor.loc["20260101", "600009"])
    assert pd.isna(factor.loc["20260101", "600008"])
    # An industry with fewer than 5 valid samples is left missing.
    assert factor.loc["20260101", "600010":"600012"].isna().all()


def test_valuation_pe_mask_treats_non_positive_pe_as_not_valuable():
    frame = _valuation_frame(
        {"600001": 10.0, "600002": 0.0, "600003": -1.0, "600004": float("nan")},
        {code: "银行" for code in ("600001", "600002", "600003", "600004")},
    )
    mask = _valuation_pe_mask(frame)
    assert bool(mask.loc["20260101", "600001"]) is True
    assert bool(mask.loc["20260101", "600002"]) is False
    assert bool(mask.loc["20260101", "600003"]) is False
    assert bool(mask.loc["20260101", "600004"]) is False


def test_coverage_reports_both_denominators_and_valuable_ratio():
    stats = [
        {"universe": 100, "eligible": 70, "executable": 70, "valuable": 60},
        {"universe": 100, "eligible": 70, "executable": 70, "valuable": 60},
    ]
    full = _coverage_metrics(stats, use_valuable_denominator=False)
    valuable = _coverage_metrics(stats, use_valuable_denominator=True)
    assert full["coverage"] == pytest.approx(0.70)
    assert valuable["coverage"] == pytest.approx(140.0 / 120.0)
    assert valuable["coverage_full_universe"] == pytest.approx(0.70)
    assert valuable["valuable_ratio"] == pytest.approx(0.60)

    # Non-valuation kinds have no valuable key; denominator stays the universe.
    plain = _coverage_metrics(
        [{"universe": 100, "eligible": 80, "executable": 80}],
        use_valuable_denominator=False,
    )
    assert plain["valuable_ratio"] == pytest.approx(1.0)
    assert plain["coverage"] == pytest.approx(0.80)


def test_valuation_verdict_caps_at_not_evaluable_when_valuable_ratio_below_half():
    registry = load_registry()
    spec = registry.factors["pe_ttm_low"]
    metrics = _strong_metrics()
    metrics["valuable_ratio"] = 0.40
    verdict, reasons = _decide_verdict(
        metrics=metrics,
        spec=spec,
        defaults=registry.defaults,
        survivorship_bias=False,
    )
    assert verdict == "not_evaluable"
    assert "low_valuable_ratio" in reasons

    metrics["valuable_ratio"] = 0.55
    verdict, reasons = _decide_verdict(
        metrics=metrics,
        spec=spec,
        defaults=registry.defaults,
        survivorship_bias=False,
    )
    assert verdict == "pass"
    assert reasons == []


def test_valuable_ratio_gate_does_not_apply_to_non_valuation_kinds():
    registry = load_registry()
    metrics = _strong_metrics()
    metrics["valuable_ratio"] = 0.10
    verdict, reasons = _decide_verdict(
        metrics=metrics,
        spec=registry.factors["momentum_5d"],
        defaults=registry.defaults,
        survivorship_bias=False,
    )
    assert verdict == "pass"
    assert reasons == []


def test_valuation_observations_count_valuable_samples_separately():
    calendar = [f"202601{day:02d}" for day in range(1, 8)]
    pe_by_code = {"600001": 5.0, "600002": 6.0, "600003": 7.0, "600004": 8.0,
                  "600005": 0.0, "600006": -2.0}
    rows = []
    for day in calendar:
        for code, pe in pe_by_code.items():
            rows.append(
                {
                    "date": day,
                    "code": code,
                    "open": 10.0,
                    "high": 10.2,
                    "close": 10.1,
                    "adj_open": 10.0,
                    "adj_high": 10.2,
                    "adj_close": 10.1,
                    "vol": 1000.0,
                    "turnover_rate": 1.0,
                    "circ_mv": 100000.0,
                    "pe_ttm": pe,
                    "industry": "银行",
                    "list_date": "20100101",
                    "is_st": False,
                    "is_a_share": True,
                }
            )
    panel = _Panel(frame=pd.DataFrame(rows), calendar=calendar, stock_basic=pd.DataFrame())
    registry = load_registry()
    _, stats = _build_observations(
        panel=panel,
        calendar=calendar,
        cohort_dates=[calendar[0]],
        spec=registry.factors["pe_ttm_low"],
        horizon=5,
        window_start="20260101",
        window_end="20260131",
    )
    assert stats[0]["universe"] == 6
    assert stats[0]["valuable"] == 4
    assert stats[0]["eligible"] == 0  # 4 peers < industry sample floor

