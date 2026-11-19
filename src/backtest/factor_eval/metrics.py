# -*- coding: utf-8 -*-
"""Statistical primitives for the unified hit-evaluation protocol.

Only numpy/pandas are used so the evaluator stays dependency-light.  The
functions are deliberately small and pure so they can be unit-tested with
synthetic panels (no network access required).
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

Z_ALPHA_95 = 1.959963984540054
Z_POWER_80 = 0.8416212335729143


def spearman_corr(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Spearman rank correlation; returns None for degenerate samples."""

    sx = pd.Series(list(x), dtype="float64")
    sy = pd.Series(list(y), dtype="float64")
    mask = sx.notna() & sy.notna()
    if int(mask.sum()) < 3:
        return None
    rx = sx[mask].rank(method="average")
    ry = sy[mask].rank(method="average")
    if rx.nunique() < 2 or ry.nunique() < 2:
        return None
    value = rx.corr(ry)
    return None if pd.isna(value) else float(value)


def newey_west_t(values: Iterable[float], lag: int) -> Optional[float]:
    """Newey-West t-stat of the mean with Bartlett kernel."""

    arr = np.asarray([v for v in values if v is not None and not pd.isna(v)], dtype="float64")
    n = arr.size
    if n < 3:
        return None
    mean = float(arr.mean())
    errors = arr - mean
    gamma0 = float(errors @ errors / n)
    variance = gamma0
    max_lag = max(0, min(int(lag), n - 1))
    for offset in range(1, max_lag + 1):
        gamma = float(errors[offset:] @ errors[:-offset] / n)
        weight = 1.0 - offset / (max_lag + 1.0)
        variance += 2.0 * weight * gamma
    if variance <= 0:
        return None
    se = math.sqrt(variance / n)
    if se == 0:
        return None
    return mean / se


def bootstrap_mean_ci(
    values: Sequence[float],
    rounds: int = 1000,
    alpha: float = 0.05,
    seed: int = 20261117,
) -> Optional[tuple]:
    """Percentile bootstrap CI for the mean of non-overlapping cohorts."""

    arr = np.asarray([v for v in values if v is not None and not pd.isna(v)], dtype="float64")
    if arr.size < 3:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(arr, size=(max(1, int(rounds)), arr.size), replace=True).mean(axis=1)
    lower = float(np.quantile(draws, alpha / 2.0))
    upper = float(np.quantile(draws, 1.0 - alpha / 2.0))
    return lower, upper


def bh_fdr(pvalues: Sequence[float], alpha: float = 0.10) -> List[bool]:
    """Benjamini-Hochberg FDR rejection flags (preserves input order)."""

    values = np.asarray(list(pvalues), dtype="float64")
    n = values.size
    if n == 0:
        return []
    order = np.argsort(values)
    ranked = values[order]
    thresholds = alpha * (np.arange(1, n + 1) / n)
    below = ranked <= thresholds
    flags = np.zeros(n, dtype=bool)
    if not below.any():
        return flags.tolist()
    cutoff = int(np.max(np.where(below)[0]))
    flags[order[: cutoff + 1]] = True
    return flags.tolist()


def minimum_detectable_effect(
    std: Optional[float],
    n: int,
    alpha: float = 0.05,
    power: float = 0.80,
) -> Optional[float]:
    """Two-sided MDE for the mean of n independent cohort statistics."""

    if std is None or n <= 0 or pd.isna(std) or std <= 0:
        return None
    z_alpha = Z_ALPHA_95 if abs(alpha - 0.05) < 1e-9 else abs(_norm_ppf(1.0 - alpha / 2.0))
    z_power = Z_POWER_80 if abs(power - 0.80) < 1e-9 else abs(_norm_ppf(power))
    return float((z_alpha + z_power) * std / math.sqrt(n))


def quintile_means(
    df: pd.DataFrame,
    factor_col: str,
    return_col: str,
    groups: int = 5,
) -> Optional[dict]:
    """Mean return by factor quantile (1 = lowest factor value)."""

    sample = df[[factor_col, return_col]].dropna()
    if len(sample) < groups * 2 or sample[factor_col].nunique() < groups:
        return None
    ranks = sample[factor_col].rank(method="first")
    buckets = pd.qcut(ranks, groups, labels=False, duplicates="drop")
    if buckets.nunique() < groups:
        return None
    means = sample.groupby(buckets)[return_col].mean()
    return {int(idx) + 1: float(value) for idx, value in means.items()}


def monotonicity_score(means: Optional[dict], direction: str) -> Optional[float]:
    """Spearman correlation between quantile order and mean return.

    Positive direction expects increasing returns; negative direction expects
    decreasing returns.  The score is direction-adjusted, so 1.0 is perfect.
    """

    if not means or len(means) < 3:
        return None
    ordered = sorted(means)
    score = spearman_corr(ordered, [means[key] for key in ordered])
    if score is None:
        return None
    return score if direction == "positive" else -score


def zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    std = values.std(ddof=0)
    if not std or pd.isna(std):
        return pd.Series(0.0, index=series.index)
    return (values - values.mean()) / std


def ols_residuals(y: pd.Series, x: pd.DataFrame) -> pd.Series:
    """Residuals of y on [1, x] with listwise deletion."""

    frame = pd.concat([pd.to_numeric(y, errors="coerce").rename("__y"), x], axis=1)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < 3:
        return pd.Series(dtype="float64")
    design = np.column_stack([np.ones(len(frame)), frame[x.columns].to_numpy(dtype="float64")])
    target = frame["__y"].to_numpy(dtype="float64")
    try:
        beta, *_ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError:
        return pd.Series(dtype="float64")
    residuals = target - design @ beta
    return pd.Series(residuals, index=frame.index)


def cross_section_beta(
    df: pd.DataFrame,
    factor_col: str,
    return_col: str,
    control_cols: Sequence[str],
) -> Optional[float]:
    """OLS slope on the standardized factor, controlling for covariates."""

    needed = [factor_col, return_col, *control_cols]
    frame = df[needed].replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < max(10, len(control_cols) + 3):
        return None
    y = frame[return_col].to_numpy(dtype="float64")
    factor = zscore(frame[factor_col]).to_numpy(dtype="float64")
    design_parts = [np.ones(len(frame)), factor]
    for col in control_cols:
        design_parts.append(zscore(frame[col]).to_numpy(dtype="float64"))
    design = np.column_stack(design_parts)
    try:
        beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return float(beta[1])


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF approximation (avoids a scipy dependency)."""

    if p <= 0.0 or p >= 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )
