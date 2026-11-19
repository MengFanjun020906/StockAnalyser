# -*- coding: utf-8 -*-
"""Unified factor hit-evaluation engine (OBS-03b).

The evaluator follows ``outputs/hit-evaluation-protocol.md``:

- non-overlapping cohorts of ``horizon`` trading days;
- T+1 open entry, T+H close exit, 20 bp round-trip cost;
- benchmark fixed to the factor's configured index (A-share: ``000985.CSI``);
- one-side limit-up / suspended entries are marked not executable;
- verdict is three-state: ``pass`` / ``fail`` / ``not_evaluable``.

The stock pool is ``list_status="L"`` ∪ ``list_status="D"``.  Delisted stocks
exit at their last tradable close (never forward-filled); missing bars after the
delisting date are counted as "already delisted / not tradable" rather than as
missing data, so ``survivorship_bias`` can be cleared once the pool contains
delisted names.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

try:  # rules.py pulls in agent.pricing, which needs Python >= 3.11 (StrEnum)
    from src.backtest.rules import limit_ratio_for as _rules_limit_ratio_for
except Exception:  # pragma: no cover - exercised on Python 3.9 environments
    _rules_limit_ratio_for = None

from .config import FactorRegistry, FactorSpec, get_factor, load_registry
from .data import EvaluationDataError, TushareEvaluationSource, code6
from .metrics import (
    bootstrap_mean_ci,
    cross_section_beta,
    minimum_detectable_effect,
    monotonicity_score,
    newey_west_t,
    ols_residuals,
    quintile_means,
    spearman_corr,
)

logger = logging.getLogger(__name__)

_A_SHARE_PREFIXES = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
    "300", "301",
    "688", "689",
)
_FACTOR_KINDS = {"momentum", "rsi_reversal", "valuation_pe"}
_CONTROL_COLUMNS = (
    "log_circ_mv",
    "gain_5d",
    "gain_20d",
    "rsi14",
    "turnover_20d",
    "dist_to_high_20",
    "industry_return_5d",
)
# Valuation factors get a different coverage denominator: stocks without a
# meaningful PE (``PE <= 0``, i.e. loss-making) are an economic fact, not a data
# quality problem, so ranking them as "extremely cheap" would be wrong and
# dropping them should not be charged against the factor.  This applies only to
# the valuation kinds below; every other kind keeps the original full-universe
# denominator.
_VALUATION_KINDS = {"valuation_pe"}
# Industry percentiles are ranked inside the SW2021 L1 industry of the decision
# day.  An industry-day with fewer valid observations than this is left missing
# instead of being ranked on a sample too small to be meaningful.
_VALUATION_INDUSTRY_MIN_SAMPLES = 5
# Protocol hard line: if less than half of the qualified universe is even
# valuable, the cross-section cannot speak for the full market, so the verdict
# caps at ``not_evaluable``.
_VALUATION_MIN_VALUABLE_RATIO = 0.50


@dataclass
class _Panel:
    frame: pd.DataFrame
    calendar: List[str]
    stock_basic: pd.DataFrame


def evaluate_factor(
    factor_id: str,
    start: str,
    end: str,
    *,
    registry: Optional[FactorRegistry] = None,
    source: Optional[TushareEvaluationSource] = None,
    cache_dir: Optional[Path] = None,
    survivorship_bias: Optional[bool] = None,
    sleep_seconds: float = 0.0,
) -> Dict[str, object]:
    """Evaluate one factor and return a protocol-shaped report.

    ``survivorship_bias`` defaults to ``None``, which means "derive it from the
    pool": ``False`` when ``stock_basic`` actually contains delisted names,
    ``True`` otherwise.  Pass an explicit boolean to override.
    """

    registry = registry or load_registry()
    spec = get_factor(registry, factor_id)
    defaults = registry.defaults
    horizon = spec.horizon
    source = source or TushareEvaluationSource(
        cache_dir=cache_dir or Path("data/factor_eval_cache"),
        sleep_seconds=sleep_seconds,
    )

    calendar = _load_calendar_with_warmup(source, start, end, horizon, spec.lookback)
    stock_basic = source.load_stock_basic()
    industry_snapshot = source.load_industry_snapshot()
    delisted_sample_count = _count_status(stock_basic, "D")
    delisted_in_window_count = _count_delisted_in_window(stock_basic, start, end)
    if survivorship_bias is None:
        survivorship_bias = delisted_sample_count == 0
    window_industry_lag_risk = str(start) < "20211213"
    panel = _build_panel(source, calendar, stock_basic, industry_snapshot)
    if panel.frame.empty:
        return _empty_report(
            spec, registry, start, end, ["insufficient_obs"],
            survivorship_bias=bool(survivorship_bias),
            industry_lag_risk=window_industry_lag_risk,
        )

    decision_dates = [
        day for day in calendar if start <= day <= end and _has_forward_window(calendar, day, horizon)
    ]
    cohort_dates = select_cohort_dates(decision_dates, horizon)

    benchmark, benchmark_error = _load_benchmark(source, spec, calendar, cohort_dates, horizon)
    if benchmark_error:
        report = _empty_report(
            spec, registry, start, end, ["benchmark_missing"],
            survivorship_bias=bool(survivorship_bias),
            industry_lag_risk=window_industry_lag_risk,
        )
        report["benchmark"]["error"] = benchmark_error
        return report

    observations, daily_stats = _build_observations(
        panel=panel,
        calendar=calendar,
        cohort_dates=cohort_dates,
        spec=spec,
        horizon=horizon,
        window_start=start,
        window_end=end,
    )
    if observations.empty:
        return _empty_report(
            spec, registry, start, end, ["insufficient_obs"],
            survivorship_bias=bool(survivorship_bias),
            industry_lag_risk=window_industry_lag_risk,
        )

    metrics = _compute_metrics(
        observations=observations,
        daily_stats=daily_stats,
        calendar=calendar,
        cohort_dates=cohort_dates,
        decision_dates_count=len(decision_dates),
        spec=spec,
        benchmark=benchmark,
        defaults=defaults,
        window_start=start,
    )
    verdict, reasons = _decide_verdict(
        metrics=metrics,
        spec=spec,
        defaults=defaults,
        survivorship_bias=survivorship_bias,
    )
    limitations = _limitations(metrics, survivorship_bias)
    promo_eligible = (
        verdict == "pass"
        and not survivorship_bias
        and not metrics.get("industry_lag_risk", False)
    )

    return {
        "factor_id": spec.factor_id,
        "factor_version": spec.version,
        "factor_definition": {
            "description": spec.description,
            "direction": spec.direction,
            "horizon": spec.horizon,
            "lookback": spec.lookback,
            "definition_hash": spec.definition_hash,
        },
        "config_path": str(registry.config_path),
        "sample": {
            "start": start,
            "end": end,
            "decision_dates": len(decision_dates),
            "cohorts": len(cohort_dates),
            "observations": int(len(observations)),
            "universe_observations": int(metrics["universe_observations"]),
            "coverage": metrics["coverage"],
            "coverage_full_universe": metrics["coverage_full_universe"],
            "coverage_valuable": metrics["coverage_valuable"],
            "valuable_observations": metrics["valuable_observations"],
            "valuable_ratio": metrics["valuable_ratio"],
            "executability": metrics["executability"],
            "stocks_per_cohort_mean": metrics["stocks_per_cohort_mean"],
            "adj_missing_rate": metrics["adj_missing_rate"],
            "industry_missing_rate": metrics["industry_missing_rate"],
            "industry_unmatched_rate": metrics["industry_unmatched_rate"],
            "delisted_sample_count": int(delisted_sample_count),
            "delisted_in_window_count": int(delisted_in_window_count),
            "delisted_exit_count": int(metrics["delisted_exit_count"]),
            "delisted_skipped_count": int(metrics["delisted_skipped_count"]),
        },
        "primary": {
            "mean_rank_ic": metrics["mean_rank_ic"],
            "std_rank_ic": metrics["std_rank_ic"],
            "icir": metrics["icir"],
            "newey_west_t": metrics["nw_t"],
            "positive_ic_ratio": metrics["positive_ic_ratio"],
            "mde": metrics["mde"],
        },
        "quantiles": metrics["quantiles"],
        "monotonicity": metrics["monotonicity"],
        "long_short": {
            "mean": metrics["long_short_mean"],
            "newey_west_t": metrics["long_short_t"],
            "ci95": metrics["long_short_ci"],
        },
        "secondary": {
            "month_positive_ratio": metrics["month_positive_ratio"],
            "regime_mean_ic": metrics["regime_mean_ic"],
        },
        "partial": {
            "mean_partial_ic": metrics["mean_partial_ic"],
            "newey_west_t": metrics["partial_t"],
            "fama_macbeth_beta": metrics["fm_beta_mean"],
            "fama_macbeth_t": metrics["fm_beta_t"],
            "controls": list(_CONTROL_COLUMNS),
        },
        "benchmark": {
            "code": spec.benchmark,
            "name": defaults.benchmark_name,
            "source": f"tushare:index_daily:{spec.benchmark}",
            "coverage": metrics["benchmark_coverage"],
        },
        "regime_method": "benchmark_trend_proxy_v1",
        "multiplicity": {"method": "bh_fdr", "alpha": defaults.fdr, "family_size": 1},
        "verdict": verdict,
        "reasons": reasons,
        "limitations": limitations,
        "survivorship_bias": bool(survivorship_bias),
        "industry_lag_risk": bool(metrics.get("industry_lag_risk", False)),
        "promotion_eligible": bool(promo_eligible),
        "artifacts": {"config": str(registry.config_path)},
    }


def select_cohort_dates(decision_dates: Sequence[str], horizon: int) -> List[str]:
    """Take the first decision date of every non-overlapping horizon block."""

    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    return list(decision_dates)[::horizon]


def _has_forward_window(calendar: Sequence[str], day: str, horizon: int) -> bool:
    try:
        index = calendar.index(day)
    except ValueError:
        return False
    return index + horizon < len(calendar)


def _load_calendar_with_warmup(
    source: TushareEvaluationSource,
    start: str,
    end: str,
    horizon: int,
    lookback: int,
) -> List[str]:
    warmup_days = max(40, lookback + 25)
    start_dt = datetime.strptime(start, "%Y%m%d") - timedelta(days=warmup_days * 2)
    end_dt = datetime.strptime(end, "%Y%m%d") + timedelta(days=horizon * 3 + 20)
    return source.load_calendar(start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d"))


def _build_panel(
    source: TushareEvaluationSource,
    calendar: Sequence[str],
    stock_basic: pd.DataFrame,
    industry_snapshot: Optional[pd.DataFrame] = None,
) -> _Panel:
    frames: List[pd.DataFrame] = []
    for day in calendar:
        bars = source.load_bars(day)
        if bars is None or bars.empty:
            continue
        adj = source.load_adj_factors(day)
        basics = source.load_basics(day)
        frame = bars.copy()
        if adj is not None and not adj.empty:
            frame = frame.merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="left")
        else:
            frame["adj_factor"] = np.nan
        if basics is not None and not basics.empty:
            keep = ["ts_code", "turnover_rate", "turnover_rate_f", "volume_ratio",
                    "pe_ttm", "pb", "total_mv", "circ_mv"]
            frame = frame.merge(basics[[c for c in keep if c in basics.columns]], on="ts_code", how="left")
        frame["date"] = day
        frame["code"] = frame["ts_code"].map(code6)
        frames.append(frame)

    if not frames:
        return _Panel(frame=pd.DataFrame(), calendar=list(calendar), stock_basic=stock_basic)

    panel = pd.concat(frames, ignore_index=True, sort=False)
    panel["adj_missing"] = panel["adj_factor"].isna() | (panel["adj_factor"] <= 0)
    panel["adj_factor"] = panel["adj_factor"].fillna(1.0)
    for raw, adjusted in (("open", "adj_open"), ("high", "adj_high"), ("close", "adj_close")):
        panel[adjusted] = pd.to_numeric(panel[raw], errors="coerce") * panel["adj_factor"]

    basic_cols = ["ts_code", "name", "industry", "market", "list_date", "delist_date"]
    panel = panel.merge(stock_basic[[c for c in basic_cols if c in stock_basic.columns]],
                        on="ts_code", how="left")
    panel["is_st"] = panel["name"].fillna("").str.upper().str.contains("ST")
    panel["is_a_share"] = panel["code"].map(is_a_share_board)

    # SW2021 L1 snapshot: an industry is only assigned when the decision day is
    # on or after the stock's `in_date`.  Before that the industry is unknown,
    # never back-filled from the current classification.
    panel["industry_used"] = None
    if industry_snapshot is not None and not industry_snapshot.empty:
        snapshot = industry_snapshot.rename(
            columns={"l1_name": "industry_sw", "in_date": "industry_in_date"}
        )
        columns = [c for c in ("ts_code", "industry_sw", "industry_in_date") if c in snapshot.columns]
        panel = panel.merge(snapshot[columns].drop_duplicates("ts_code"), on="ts_code", how="left")
        if {"industry_sw", "industry_in_date"}.issubset(panel.columns):
            matched = (
                panel["industry_sw"].notna()
                & (panel["industry_in_date"].fillna("") != "")
                & (panel["industry_in_date"] <= panel["date"])
            )
            panel.loc[matched, "industry_used"] = panel.loc[matched, "industry_sw"]

    drop_columns = [
        column for column in
        ("name", "industry", "market", "industry_sw", "industry_in_date")
        if column in panel.columns
    ]
    if drop_columns:
        panel = panel.drop(columns=drop_columns)
    return _Panel(frame=panel, calendar=list(calendar), stock_basic=stock_basic)


def is_a_share_board(code: str) -> bool:
    return str(code).startswith(_A_SHARE_PREFIXES)


def _load_benchmark(
    source: TushareEvaluationSource,
    spec: FactorSpec,
    calendar: Sequence[str],
    cohort_dates: Sequence[str],
    horizon: int,
) -> tuple:
    if not cohort_dates:
        return pd.DataFrame(), "no cohort dates"
    start = calendar[0]
    end = calendar[min(len(calendar) - 1, calendar.index(cohort_dates[-1]) + horizon)]
    try:
        benchmark = source.load_benchmark(spec.benchmark, start, end)
    except EvaluationDataError as exc:
        return pd.DataFrame(), str(exc)
    benchmark = benchmark.set_index("trade_date")
    required = set()
    for day in cohort_dates:
        index = calendar.index(day)
        required.add(calendar[index + 1])
        required.add(calendar[index + horizon])
    missing = [day for day in required if day not in benchmark.index]
    coverage = 1.0 - len(missing) / max(1, len(required))
    if coverage < 0.90:
        return pd.DataFrame(), f"benchmark coverage {coverage:.2%} below 90%"
    return benchmark, None


def _pivot(panel: pd.DataFrame, column: str) -> pd.DataFrame:
    return panel.pivot_table(index="date", columns="code", values=column, aggfunc="first")


def _rsi14(close: pd.DataFrame) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.where(avg_loss > 0, 100.0).where(avg_gain.notna())


def _feature_matrices(panel: pd.DataFrame, spec: FactorSpec) -> Dict[str, pd.DataFrame]:
    close = _pivot(panel, "adj_close").sort_index()
    high = _pivot(panel, "adj_high").sort_index()
    turnover = _pivot(panel, "turnover_rate").sort_index()
    gain_5d = close / close.shift(5) - 1.0
    gain_20d = close / close.shift(20) - 1.0
    rsi14 = _rsi14(close)
    features = {
        "gain_5d": gain_5d,
        "gain_20d": gain_20d,
        "rsi14": rsi14,
        "turnover_20d": turnover.rolling(20, min_periods=10).mean(),
        "dist_to_high_20": close / high.rolling(20, min_periods=10).max() - 1.0,
    }
    if spec.kind == "momentum":
        features["factor"] = close / close.shift(max(1, spec.lookback)) - 1.0
    elif spec.kind == "reversal":
        features["factor"] = -(close / close.shift(max(1, spec.lookback)) - 1.0)
    elif spec.kind == "rsi_reversal":
        features["factor"] = rsi14
    elif spec.kind == "valuation_pe":
        features["factor"] = _valuation_pe_industry_percentile(panel)
    else:
        raise ValueError(f"unsupported factor kind: {spec.kind}")
    return features


def _panel_dates_codes(panel: pd.DataFrame) -> tuple:
    """Full (date, code) axes so all-NaN columns are never silently dropped."""

    if "date" not in panel.columns or "code" not in panel.columns:
        return None, None
    dates = pd.Index(sorted(panel["date"].dropna().astype(str).unique()))
    codes = pd.Index(sorted(panel["code"].dropna().astype(str).unique()))
    return dates, codes


def _valuation_pe_mask(panel: pd.DataFrame) -> pd.DataFrame:
    """Boolean matrix marking samples with a usable PE(TTM).

    ``PE <= 0`` is not an extreme low valuation: it means the earnings base is
    negative or the metric is not meaningful.  Those cells become missing for
    both the factor itself and the "valuable" coverage denominator.
    """

    if "pe_ttm" not in panel.columns:
        return pd.DataFrame()
    pe = _pivot(panel, "pe_ttm").apply(pd.to_numeric, errors="coerce")
    mask = pe.notna() & (pe > 0)
    dates, codes = _panel_dates_codes(panel)
    if dates is not None:
        mask = mask.reindex(index=dates, columns=codes).fillna(False).astype(bool)
    return mask


def _valuation_pe_industry_percentile(panel: pd.DataFrame) -> pd.DataFrame:
    """PE(TTM) percentile inside the stock's SW2021 L1 industry that day.

    The industry dimension is the point-in-time ``industry_used`` column built
    in :func:`_build_panel` (``industry`` is only a fallback for tests/legacy
    panels).  Sorting happens per industry-day so "cheap" is always relative to
    peers, never to the whole market.  Industry-days with fewer than
    ``_VALUATION_INDUSTRY_MIN_SAMPLES`` valid PEs are left missing.
    """

    pe = _pivot(panel, "pe_ttm").apply(pd.to_numeric, errors="coerce") if "pe_ttm" in panel.columns else None
    if pe is None:
        return pd.DataFrame()
    pe = pe.where(pe > 0).sort_index()
    industry_column = "industry_used" if "industry_used" in panel.columns else "industry"
    if industry_column not in panel.columns:
        return pd.DataFrame(index=pe.index, columns=pe.columns, dtype="float64")
    industry = _pivot(panel, industry_column).sort_index()
    industry = industry.reindex(index=pe.index, columns=pe.columns)

    long = pe.stack().rename("pe").to_frame()
    long["industry"] = industry.stack().reindex(long.index)
    long = long.dropna(subset=["industry"])
    if long.empty:
        return pd.DataFrame(index=pe.index, columns=pe.columns, dtype="float64")

    grouped = long.groupby([long.index.get_level_values(0), "industry"])["pe"]
    long["percentile"] = grouped.rank(pct=True)
    long["sample_size"] = grouped.transform("count")
    long.loc[long["sample_size"] < _VALUATION_INDUSTRY_MIN_SAMPLES, "percentile"] = np.nan
    factor = long["percentile"].unstack().reindex(index=pe.index, columns=pe.columns)
    dates, codes = _panel_dates_codes(panel)
    if dates is not None:
        factor = factor.reindex(index=dates, columns=codes)
    return factor


def _build_observations(
    *,
    panel: _Panel,
    calendar: Sequence[str],
    cohort_dates: Sequence[str],
    spec: FactorSpec,
    horizon: int,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> tuple:
    frame = panel.frame
    close = _pivot(frame, "adj_close").sort_index()
    open_adj = _pivot(frame, "adj_open").sort_index()
    open_raw = _pivot(frame, "open").sort_index()
    close_raw = _pivot(frame, "close").sort_index()
    volume = _pivot(frame, "vol").sort_index()
    circ_mv = _pivot(frame, "circ_mv").sort_index()
    features = _feature_matrices(frame, spec)
    valuation_mask = _valuation_pe_mask(frame) if spec.kind in _VALUATION_KINDS else pd.DataFrame()

    industry_column = "industry_used" if "industry_used" in frame.columns else "industry"
    industry = _pivot(frame, industry_column).sort_index()
    list_date = frame.drop_duplicates("code").set_index("code")["list_date"]
    is_st = frame.drop_duplicates("code").set_index("code")["is_st"]
    is_a_share = frame.drop_duplicates("code").set_index("code")["is_a_share"]

    delist_dates = _delist_map(panel.stock_basic)
    last_bar_date = close.apply(lambda series: series.last_valid_index())
    window_start = str(window_start or calendar[0])
    window_end = str(window_end or calendar[-1])
    window_delisted = {
        code: day for code, day in delist_dates.items()
        if day and window_start <= day <= window_end
    }

    rows: List[pd.DataFrame] = []
    daily_stats: List[dict] = []
    adj_missing_days = 0
    total_days = 0

    for day in cohort_dates:
        index = calendar.index(day)
        entry_day = calendar[index + 1]
        exit_day = calendar[index + horizon]
        if entry_day not in open_raw.index or exit_day not in close.index:
            continue
        total_days += 1
        if "adj_missing" in frame.columns:
            adj_missing_days += int(frame.loc[frame["date"] == day, "adj_missing"].mean() > 0.20)

        entry = open_adj.loc[entry_day]
        exit_price = close.loc[exit_day].copy()
        forward = exit_price / entry - 1.0
        # Delisted-exit rule: a stock that was delisted on or before the exit
        # day exits at its last tradable close.  This is not forward filling:
        # the series itself is never extended, we only define where the position
        # ends.
        forced_exit = pd.Series(False, index=close.columns)
        for code in forward.index[forward.isna()]:
            delist_date = delist_dates.get(code)
            if not delist_date or delist_date > exit_day:
                continue
            last_day = last_bar_date.get(code)
            if not isinstance(last_day, str) or last_day < entry_day or last_day > exit_day:
                continue
            exit_price.at[code] = close.at[last_day, code]
            forced_exit.at[code] = True
        forward = exit_price / entry - 1.0
        raw_entry = open_raw.loc[entry_day]
        previous_close = close_raw.loc[day]
        volume_entry = volume.loc[entry_day]

        ratio = previous_close.index.to_series().map(
            lambda code: _entry_limit_ratio(code, entry_day, bool(is_st.get(code, False)))
        )
        limit_up = previous_close * (1.0 + ratio)
        executable = (
            volume_entry.fillna(0).astype(float) > 0
        ) & raw_entry.notna() & (raw_entry > 0) & (raw_entry < limit_up - 0.01) & forward.notna()

        factor = features["factor"].loc[day]
        universe = (
            factor.index.to_series().map(lambda code: bool(is_a_share.get(code, False)))
            & ~factor.index.to_series().map(lambda code: bool(is_st.get(code, False)))
            & factor.index.to_series().map(lambda code: _listed_long_enough(list_date.get(code), calendar, index))
        )
        eligible = universe & factor.notna()
        executable = executable & eligible

        # ``valuable`` is the denominator for valuation-factor coverage: the
        # qualified universe minus stocks whose PE is not meaningful.  Other
        # kinds keep the full universe so their coverage is unchanged.
        if spec.kind in _VALUATION_KINDS:
            if day in valuation_mask.index:
                ready = valuation_mask.loc[day].reindex(factor.index).fillna(False).astype(bool)
                valuable = universe & ready
            else:
                valuable = universe & False
        else:
            valuable = universe

        industry_today = (
            industry.loc[day].reindex(factor.index)
            if day in industry.index
            else pd.Series(index=factor.index, dtype="object")
        )
        industry_return = features["gain_5d"].loc[day].groupby(industry_today).transform("mean")

        block = pd.DataFrame(
            {
                "date": day,
                "code": factor.index,
                "factor": factor.to_numpy(),
                "forward_return": forward.reindex(factor.index).to_numpy(),
                "executable": executable.reindex(factor.index).fillna(False).to_numpy(),
                "universe": universe.reindex(factor.index).fillna(False).to_numpy(),
                "eligible": eligible.reindex(factor.index).fillna(False).to_numpy(),
                "delisted_exit": forced_exit.reindex(factor.index).fillna(False).to_numpy(),
                "gain_5d": features["gain_5d"].loc[day].reindex(factor.index).to_numpy(),
                "gain_20d": features["gain_20d"].loc[day].reindex(factor.index).to_numpy(),
                "rsi14": features["rsi14"].loc[day].reindex(factor.index).to_numpy(),
                "turnover_20d": features["turnover_20d"].loc[day].reindex(factor.index).to_numpy(),
                "dist_to_high_20": features["dist_to_high_20"].loc[day].reindex(factor.index).to_numpy(),
                "industry": industry_today.to_numpy(),
                "industry_return_5d": industry_return.reindex(factor.index).to_numpy(),
                "circ_mv": circ_mv.loc[day].reindex(factor.index).to_numpy(),
            }
        )
        daily_stats.append(
            {
                "date": day,
                "universe": int(universe.sum()),
                "eligible": int(eligible.sum()),
                "executable": int(executable.sum()),
                "valuable": int(valuable.sum()),
                # Delisted before entry: no bar can exist, so this is "already
                # delisted / not tradable", not missing data.
                "delisted_skipped": sum(
                    1 for delist_date in window_delisted.values() if delist_date < entry_day
                ),
            }
        )
        rows.append(block)

    if not rows:
        return pd.DataFrame(), daily_stats

    observations = pd.concat(rows, ignore_index=True)
    observations = observations[observations["executable"]].copy()
    observations["log_circ_mv"] = np.log(
        pd.to_numeric(observations["circ_mv"], errors="coerce").clip(lower=1.0)
    )
    observations["adj_missing_rate"] = adj_missing_days / max(1, total_days)
    return observations, daily_stats


def _listed_long_enough(list_value, calendar: Sequence[str], index: int) -> bool:
    if not isinstance(list_value, str) or not list_value:
        return True
    cutoff_index = max(0, index - 60)
    return list_value <= calendar[cutoff_index]


def _count_status(stock_basic: pd.DataFrame, status: str) -> int:
    if "list_status" not in stock_basic.columns:
        return 0
    return int((stock_basic["list_status"].astype(str) == status).sum())


def _count_delisted_in_window(stock_basic: pd.DataFrame, start: str, end: str) -> int:
    if "delist_date" not in stock_basic.columns:
        return 0
    values = stock_basic["delist_date"].map(lambda value: value if isinstance(value, str) else "")
    in_window = (values.str.len() == 8) & (values >= str(start)) & (values <= str(end))
    return int(in_window.sum())


def _delist_map(stock_basic: pd.DataFrame) -> Dict[str, str]:
    """Map 6-digit code -> delist date (YYYYMMDD) for delisted stocks."""

    if "delist_date" not in stock_basic.columns or "ts_code" not in stock_basic.columns:
        return {}
    mapping: Dict[str, str] = {}
    for row in stock_basic[["ts_code", "delist_date"]].itertuples(index=False):
        value = getattr(row, "delist_date", "")
        if not isinstance(value, str) or len(value) != 8:
            continue
        mapping[code6(getattr(row, "ts_code"))] = value
    return mapping


def _compute_metrics(
    *,
    observations: pd.DataFrame,
    daily_stats: Sequence[dict],
    calendar: Sequence[str],
    cohort_dates: Sequence[str],
    decision_dates_count: int,
    spec: FactorSpec,
    benchmark: pd.DataFrame,
    defaults,
    window_start: str = "",
) -> Dict[str, object]:
    observations = observations.copy()
    observations["excess_return"] = observations["forward_return"] - defaults.cost_bps / 10000.0

    bench_returns = _benchmark_returns(benchmark, calendar, cohort_dates, spec.horizon)
    observations["benchmark_return"] = observations["date"].map(bench_returns)
    observations["excess_return"] = observations["excess_return"] - observations["benchmark_return"]

    ic_values: List[float] = []
    quintile_rows: List[dict] = []
    long_short: List[float] = []
    partial_ics: List[float] = []
    fm_betas: List[float] = []
    month_buckets: Dict[str, List[float]] = {}
    regime_buckets: Dict[str, List[float]] = {}
    factor_unique: List[int] = []
    factor_std: List[float] = []

    regimes = _classify_regimes(benchmark, calendar, cohort_dates)
    regime_series = pd.Series(regimes, dtype="object")

    for day in cohort_dates:
        block = observations[observations["date"] == day]
        if len(block) < defaults.min_stocks_per_date:
            continue
        factor_unique.append(int(block["factor"].nunique()))
        factor_std.append(float(block["factor"].std(ddof=0) or 0.0))
        ic = spearman_corr(block["factor"], block["excess_return"])
        if ic is None:
            continue
        directional_ic = ic * spec.sign
        ic_values.append(directional_ic)
        month_buckets.setdefault(day[:6], []).append(directional_ic)
        regime = str(regime_series.get(day, "unknown"))
        regime_buckets.setdefault(regime, []).append(directional_ic)

        means = quintile_means(block, "factor", "excess_return", groups=5)
        if means:
            quintile_rows.append(means)
            low, high = (1, 5)
            long_short.append(means[high] - means[low] if spec.direction == "positive"
                              else means[low] - means[high])

        partial = _partial_ic(block)
        if partial is not None:
            partial_ics.append(partial * spec.sign)
        beta = cross_section_beta(block, "factor", "excess_return", _CONTROL_COLUMNS)
        if beta is not None:
            fm_betas.append(beta * spec.sign)

    mean_ic = float(np.mean(ic_values)) if ic_values else None
    std_ic = float(np.std(ic_values, ddof=1)) if len(ic_values) > 1 else None
    icir = (mean_ic / std_ic) if mean_ic is not None and std_ic else None
    nw_t = newey_west_t(ic_values, spec.horizon - 1)
    positive_ratio = float(np.mean([1.0 if value > 0 else 0.0 for value in ic_values])) if ic_values else None
    mde = minimum_detectable_effect(std_ic, len(ic_values))

    quantiles = _aggregate_quantiles(quintile_rows)
    monotonicity = monotonicity_score(quantiles, spec.direction)
    long_short_mean = float(np.mean(long_short)) if long_short else None
    long_short_t = newey_west_t(long_short, spec.horizon - 1)
    long_short_ci = bootstrap_mean_ci(long_short, rounds=defaults.bootstrap_rounds)

    partial_mean = float(np.mean(partial_ics)) if partial_ics else None
    partial_t = newey_west_t(partial_ics, spec.horizon - 1)
    fm_beta_mean = float(np.mean(fm_betas)) if fm_betas else None
    fm_beta_t = newey_west_t(fm_betas, spec.horizon - 1)

    month_positive = _positive_ratio([np.mean(values) for values in month_buckets.values()])
    regime_mean = {key: float(np.mean(values)) for key, values in regime_buckets.items() if values}

    coverage_block = _coverage_metrics(
        daily_stats,
        use_valuable_denominator=spec.kind in _VALUATION_KINDS,
    )
    universe_obs = coverage_block["universe_observations"]
    eligible_obs = coverage_block["eligible_observations"]
    executable_obs = coverage_block["executable_observations"]
    coverage = coverage_block["coverage"]
    executability = executable_obs / eligible_obs if eligible_obs else 0.0
    stocks_per_cohort = executable_obs / max(1, len(cohort_dates))

    industry_missing = float(observations["industry"].isna().mean()) if len(observations) else 1.0
    # SW2021 only carries the latest classification; a window that starts before
    # the SW2021 switch-over (2021-12-13) cannot claim point-in-time industry.
    industry_lag_risk = bool(
        (str(window_start or "00000000") < "20211213") or industry_missing > 0.30
    )
    delisted_exit_count = (
        int(observations["delisted_exit"].sum()) if "delisted_exit" in observations.columns else 0
    )
    delisted_skipped_count = int(sum(item.get("delisted_skipped", 0) for item in daily_stats))

    bench_required = 0
    bench_present = 0
    for day in cohort_dates:
        index = calendar.index(day)
        for target in (calendar[index + 1], calendar[index + spec.horizon]):
            bench_required += 1
            if target in benchmark.index:
                bench_present += 1
    benchmark_coverage = bench_present / bench_required if bench_required else 0.0

    return {
        "mean_rank_ic": mean_ic,
        "std_rank_ic": std_ic,
        "icir": icir,
        "nw_t": nw_t,
        "positive_ic_ratio": positive_ratio,
        "mde": mde,
        "quantiles": quantiles,
        "monotonicity": monotonicity,
        "long_short_mean": long_short_mean,
        "long_short_t": long_short_t,
        "long_short_ci": long_short_ci,
        "month_positive_ratio": month_positive,
        "regime_mean_ic": regime_mean,
        "mean_partial_ic": partial_mean,
        "partial_t": partial_t,
        "fm_beta_mean": fm_beta_mean,
        "fm_beta_t": fm_beta_t,
        "universe_observations": universe_obs,
        "n_decision_dates": int(decision_dates_count),
        "coverage": coverage,
        "coverage_full_universe": coverage_block["coverage_full_universe"],
        "coverage_valuable": coverage_block["coverage_valuable"],
        "valuable_observations": coverage_block["valuable_observations"],
        "valuable_ratio": coverage_block["valuable_ratio"],
        "executability": executability,
        "stocks_per_cohort_mean": stocks_per_cohort,
        "adj_missing_rate": float(observations["adj_missing_rate"].iloc[0]) if len(observations) else 0.0,
        "industry_missing_rate": industry_missing,
        "industry_unmatched_rate": industry_missing,
        "delisted_exit_count": delisted_exit_count,
        "delisted_skipped_count": delisted_skipped_count,
        "benchmark_coverage": benchmark_coverage,
        "regime_counts": pd.Series(regimes).value_counts().to_dict() if regimes else {},
        "industry_lag_risk": industry_lag_risk,
        "degenerate_factor": bool(
            not factor_unique
            or float(np.median(factor_unique)) < 3
            or float(np.median(factor_std)) <= 0.0
        ),
        "ic_values": ic_values,
    }


def _coverage_metrics(daily_stats: Sequence[dict], *, use_valuable_denominator: bool) -> Dict[str, float]:
    """Coverage under both denominators.

    ``coverage_full_universe`` is the original ``eligible / universe`` caliber
    used by every non-valuation kind.  ``coverage_valuable`` divides by the
    samples that actually have a usable valuation input, and
    ``valuable_ratio = valuable / universe`` tells how much of the qualified
    universe that denominator represents.  ``coverage`` is the caliber the
    verdict uses: valuable-based for valuation kind, full-universe otherwise.
    """

    universe_obs = int(sum(item["universe"] for item in daily_stats))
    eligible_obs = int(sum(item["eligible"] for item in daily_stats))
    executable_obs = int(sum(item["executable"] for item in daily_stats))
    valuable_obs = int(sum(item.get("valuable", item["universe"]) for item in daily_stats))
    coverage_full = eligible_obs / universe_obs if universe_obs else 0.0
    coverage_valuable = eligible_obs / valuable_obs if valuable_obs else 0.0
    valuable_ratio = valuable_obs / universe_obs if universe_obs else 0.0
    return {
        "universe_observations": universe_obs,
        "eligible_observations": eligible_obs,
        "executable_observations": executable_obs,
        "valuable_observations": valuable_obs,
        "coverage": coverage_valuable if use_valuable_denominator else coverage_full,
        "coverage_full_universe": coverage_full,
        "coverage_valuable": coverage_valuable,
        "valuable_ratio": valuable_ratio,
    }


def _partial_ic(block: pd.DataFrame) -> Optional[float]:
    controls = [column for column in _CONTROL_COLUMNS if column in block.columns]
    sample = block[["factor", "excess_return", *controls]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(sample) < 20:
        return None
    residual_factor = ols_residuals(sample["factor"], sample[controls])
    residual_return = ols_residuals(sample["excess_return"], sample[controls])
    joined = pd.concat([residual_factor.rename("f"), residual_return.rename("r")], axis=1).dropna()
    if len(joined) < 20:
        return None
    return spearman_corr(joined["f"], joined["r"])


def _aggregate_quantiles(rows: Sequence[dict]) -> dict:
    if not rows:
        return {}
    keys = sorted({key for row in rows for key in row})
    return {
        str(key): float(np.mean([row[key] for row in rows if key in row]))
        for key in keys
    }


def _positive_ratio(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(np.mean([1.0 if value > 0 else 0.0 for value in values]))


def _benchmark_returns(
    benchmark: pd.DataFrame,
    calendar: Sequence[str],
    cohort_dates: Sequence[str],
    horizon: int,
) -> Dict[str, float]:
    returns: Dict[str, float] = {}
    for day in cohort_dates:
        index = calendar.index(day)
        entry_day = calendar[index + 1]
        exit_day = calendar[index + horizon]
        if entry_day not in benchmark.index or exit_day not in benchmark.index:
            continue
        entry = pd.to_numeric(benchmark.loc[entry_day, "open"], errors="coerce")
        exit_price = pd.to_numeric(benchmark.loc[exit_day, "close"], errors="coerce")
        if pd.isna(entry) or pd.isna(exit_price) or entry <= 0:
            continue
        returns[day] = float(exit_price / entry - 1.0)
    return returns


def _classify_regimes(
    benchmark: pd.DataFrame,
    calendar: Sequence[str],
    cohort_dates: Sequence[str],
) -> Dict[str, str]:
    close = pd.to_numeric(benchmark["close"], errors="coerce")
    returns = close.pct_change()
    regimes: Dict[str, str] = {}
    for day in cohort_dates:
        index = calendar.index(day)
        if index < 20 or day not in close.index:
            continue
        prior = calendar[index - 20]
        if prior not in close.index:
            continue
        trend = close.loc[day] / close.loc[prior] - 1.0
        window = returns.loc[[d for d in calendar[index - 19:index + 1] if d in returns.index]]
        vol = float(window.std(ddof=0)) if len(window) >= 10 else 0.0
        trend_state = "bull" if trend > 0.03 else ("bear" if trend < -0.03 else "range")
        vol_state = "high_vol" if vol > 0.02 else "low_vol"
        regimes[day] = f"{trend_state}|{vol_state}"
    return regimes


def _decide_verdict(
    *,
    metrics: Dict[str, object],
    spec: FactorSpec,
    defaults,
    survivorship_bias: bool,
) -> tuple:
    reasons: List[str] = []
    if metrics["benchmark_coverage"] < 0.90:
        reasons.append("benchmark_missing")
    if metrics["n_decision_dates"] < defaults.min_decision_dates:
        reasons.append("insufficient_dates")
    if (
        metrics["universe_observations"] < defaults.min_observations
        or metrics["stocks_per_cohort_mean"] < defaults.min_stocks_per_date
    ):
        reasons.append("insufficient_obs")
    if metrics["coverage"] < defaults.min_coverage_not_evaluable:
        reasons.append("low_coverage")
    if (
        spec.kind in _VALUATION_KINDS
        and float(metrics.get("valuable_ratio") or 0.0) < _VALUATION_MIN_VALUABLE_RATIO
    ):
        # A valuation factor computed on less than half of the qualified
        # universe cannot be extrapolated to the full market.  The cap is
        # ``not_evaluable``: we must not read a thin sample as "no effect".
        reasons.append("low_valuable_ratio")
    if metrics["executability"] < defaults.min_executability:
        reasons.append("low_executability")
    if metrics["mde"] is None or metrics["mde"] > defaults.max_mde:
        reasons.append("insufficient_power")
    if _single_regime(metrics["regime_counts"]):
        reasons.append("single_regime")
    if metrics["adj_missing_rate"] > 0.20:
        reasons.append("data_quality_breach")
    if float(metrics.get("industry_unmatched_rate", 0.0) or 0.0) > 0.30:
        reasons.append("industry_unmatched")
    if survivorship_bias:
        reasons.append("survivorship")
    if metrics.get("degenerate_factor", False):
        reasons.append("degenerate_factor")

    if reasons:
        return "not_evaluable", reasons

    passed = (
        metrics["coverage"] >= defaults.min_coverage_pass
        and metrics["executability"] >= defaults.min_executability
        and metrics["mean_rank_ic"] is not None
        and metrics["mean_rank_ic"] >= defaults.min_mean_rank_ic
        and metrics["icir"] is not None
        and metrics["icir"] >= defaults.min_icir
        and metrics["nw_t"] is not None
        and metrics["nw_t"] >= defaults.min_ic_t
        and metrics["positive_ic_ratio"] is not None
        and metrics["positive_ic_ratio"] >= defaults.min_positive_ic_ratio
        and metrics["monotonicity"] is not None
        and metrics["monotonicity"] >= defaults.min_monotonicity
        and metrics["long_short_mean"] is not None
        and metrics["long_short_mean"] > 0
        and metrics["long_short_ci"] is not None
        and metrics["long_short_ci"][0] > 0
        and metrics["mean_partial_ic"] is not None
        and metrics["mean_partial_ic"] >= defaults.min_partial_ic
        and metrics["partial_t"] is not None
        and metrics["partial_t"] >= defaults.min_partial_t
        and metrics["month_positive_ratio"] is not None
        and metrics["month_positive_ratio"] >= defaults.min_month_positive_ratio
        and _regime_consistency(metrics["regime_mean_ic"])
    )
    return ("pass" if passed else "fail"), []


def _single_regime(counts: Dict[str, int]) -> bool:
    if not counts or len(counts) < 2:
        return True
    total = sum(counts.values())
    return bool(total and max(counts.values()) / total > 0.70)


def _regime_consistency(regime_mean: Dict[str, float]) -> bool:
    if not regime_mean:
        return False
    positive = [value for value in regime_mean.values() if value > 0]
    return len(positive) >= 2


def _limitations(metrics: Dict[str, object], survivorship_bias: bool) -> List[str]:
    limitations: List[str] = []
    if survivorship_bias:
        limitations.append("survivorship")
    if metrics.get("industry_lag_risk", False):
        limitations.append("industry_lag")
    if metrics.get("adj_missing_rate", 0.0) > 0.0:
        limitations.append("adj_factor_gap")
    return limitations


def _entry_limit_ratio(code: str, day: str, st: bool) -> float:
    session = _to_date(day)
    if _rules_limit_ratio_for is not None:
        return float(_rules_limit_ratio_for(code, session, st=st))
    # Fallback mirrors src/backtest/rules.py:limit_ratio_for so the evaluator can
    # run on Python 3.9, where importing rules.py fails on StrEnum.
    board = "star" if code.startswith(("688", "689")) else (
        "chinext" if code.startswith(("300", "301")) else "main"
    )
    if board == "star" and session < date(2019, 7, 22):
        return 0.20 if st else 0.10
    if board == "chinext" and session < date(2020, 8, 24):
        return 0.20 if st else 0.10
    if board in {"star", "chinext"}:
        return 0.20
    return 0.05 if st else 0.10


def _to_date(day: str) -> date:
    return datetime.strptime(str(day), "%Y%m%d").date()


def _empty_report(
    spec: FactorSpec,
    registry: FactorRegistry,
    start: str,
    end: str,
    reasons: Sequence[str],
    survivorship_bias: bool = True,
    industry_lag_risk: bool = True,
) -> Dict[str, object]:
    return {
        "factor_id": spec.factor_id,
        "factor_version": spec.version,
        "factor_definition": {
            "description": spec.description,
            "direction": spec.direction,
            "horizon": spec.horizon,
            "lookback": spec.lookback,
            "definition_hash": spec.definition_hash,
        },
        "config_path": str(registry.config_path),
        "sample": {
            "start": start,
            "end": end,
            "coverage": None,
            "coverage_full_universe": None,
            "coverage_valuable": None,
            "valuable_observations": None,
            "valuable_ratio": None,
            "delisted_exit_count": 0,
            "delisted_skipped_count": 0,
            "industry_unmatched_rate": None,
        },
        "benchmark": {
            "code": spec.benchmark,
            "name": registry.defaults.benchmark_name,
            "source": f"tushare:index_daily:{spec.benchmark}",
        },
        "verdict": "not_evaluable",
        "reasons": list(reasons),
        "limitations": list(reasons),
        "survivorship_bias": bool(survivorship_bias),
        "industry_lag_risk": bool(industry_lag_risk),
        "promotion_eligible": False,
    }


def write_report(report: MappingLike, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


MappingLike = Dict[str, object]
