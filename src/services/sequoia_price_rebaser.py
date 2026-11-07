# -*- coding: utf-8 -*-
"""Normalize Sequoia daily OHLC prices to StockAnalyser cash-price scale."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

OHLC_COLUMNS = ("open", "high", "low", "close")
_MIN_REBASE_RATIO = 1.5
_MAX_RELATIVE_SPREAD = 0.02
_MAX_RELATIVE_FACTOR_STEP = 0.10
_AMOUNT_VOLUME_FACTOR_LOOKBACK = 20
_MIN_AMOUNT_VOLUME_FACTOR_ROWS = 5
_MAX_AMOUNT_VOLUME_RELATIVE_SPREAD = 0.30
_AMOUNT_VOLUME_ROLLING_FACTOR_RADIUS = 2
_MIN_AMOUNT_VOLUME_ROLLING_FACTOR_ROWS = 3
_KNOWN_INDEX_SYMBOLS = {
    "000001.SH",
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "399001.SZ",
    "399006.SZ",
    "SH.000001",
    "SH.000300",
    "SH.000905",
    "SH.000852",
    "SZ.399001",
    "SZ.399006",
    "SH000001",
    "SH000300",
    "SH000905",
    "SH000852",
    "SZ399001",
    "SZ399006",
}


@dataclass(frozen=True)
class SequoiaPriceRebase:
    """Resolved Sequoia-to-cash price factor."""

    adjusted: bool
    factor: Optional[float] = None
    factor_by_date: Tuple[Tuple[str, float], ...] = ()
    overlap_count: int = 0
    basis: str = "none"

    def factor_for_date(self, value: Any) -> Optional[float]:
        if not self.adjusted:
            return None
        date_key = _date_key(value)
        if not self.factor_by_date or not date_key:
            return self.factor
        selected = self.factor_by_date[0][1]
        for factor_date, factor in self.factor_by_date:
            if factor_date > date_key:
                break
            selected = factor
        return selected


def resolve_sequoia_cash_price_rebase(
    stock_code: str,
    rows: Iterable[Any],
    *,
    database_path: Optional[str] = None,
) -> SequoiaPriceRebase:
    """Return a factor that rebases Sequoia OHLC rows to local cash prices.

    Some local Sequoia snapshots store A-share stock OHLC as 后复权 prices while
    StockAnalyser charting and execution logic expects cash/未复权 prices.  The
    safest local correction is to use overlapping rows from the main
    ``stock_daily`` table and only apply a stable, material ratio.
    """

    row_list = list(rows)
    sequoia_by_date = _sequoia_close_by_date(row_list)
    if not sequoia_by_date:
        return SequoiaPriceRebase(adjusted=False, basis="no_sequoia_rows")

    amount_volume_rebase = resolve_amount_volume_cash_price_rebase(row_list)
    db_path = Path(database_path or os.getenv("DATABASE_PATH") or "./data/stock_analysis.db").expanduser()
    if not db_path.exists():
        if amount_volume_rebase is not None:
            return amount_volume_rebase
        return SequoiaPriceRebase(adjusted=False, basis="main_db_missing")

    dates = sorted(sequoia_by_date)
    for candidates in _main_code_candidate_tiers(stock_code):
        factor_points = _load_overlap_factors(db_path, candidates, sequoia_by_date, dates)
        if not factor_points:
            continue
        ratios = [factor for _date_key, factor in factor_points]
        factor = median(ratios)
        overlap_count = len(ratios)
        if not _all_material_rebase_factors(ratios):
            if amount_volume_rebase is not None and amount_volume_rebase.adjusted:
                return amount_volume_rebase
            return SequoiaPriceRebase(
                adjusted=False,
                factor=factor,
                overlap_count=overlap_count,
                basis="already_cash_scale",
            )
        if not (_is_stable_factor(factor, ratios) or _is_smooth_factor_series(factor_points)):
            if amount_volume_rebase is not None and amount_volume_rebase.adjusted:
                return amount_volume_rebase
            return SequoiaPriceRebase(
                adjusted=False,
                factor=factor,
                overlap_count=overlap_count,
                basis="unstable_overlap_ratio",
            )
        logger.debug(
            "Sequoia OHLC rebased for %s by factor %.8f using %d main DB overlaps",
            stock_code,
            factor,
            overlap_count,
        )
        return SequoiaPriceRebase(
            adjusted=True,
            factor=factor,
            factor_by_date=tuple(factor_points),
            overlap_count=overlap_count,
            basis="main_stock_daily_overlap",
        )

    if amount_volume_rebase is not None:
        return amount_volume_rebase

    return SequoiaPriceRebase(adjusted=False, basis="no_main_db_overlap")


def rebase_ohlc_value(value: Any, factor: Optional[float]) -> Any:
    """Divide one OHLC value by ``factor`` when a valid factor is available."""

    if factor is None or factor == 0:
        return value
    number = _safe_float(value)
    if number is None:
        return value
    return number / factor


def rebase_ohlc_value_for_date(value: Any, date_value: Any, rebase: SequoiaPriceRebase) -> Any:
    """Divide one OHLC value by the nearest known factor for ``date_value``."""

    return rebase_ohlc_value(value, rebase.factor_for_date(date_value))


def _sequoia_close_by_date(rows: Iterable[Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in rows:
        date_key = _date_key(_row_get(row, "date"))
        close = _safe_float(_row_get(row, "close"))
        if date_key and close is not None and close > 0:
            values[date_key] = close
    return values


def resolve_amount_volume_cash_price_rebase(
    rows: Iterable[Any],
    *,
    adjusted_basis: str = "amount_volume_vwap",
    already_cash_basis: str = "amount_volume_already_cash_scale",
    unstable_basis: str = "amount_volume_unstable_ratio",
) -> Optional[SequoiaPriceRebase]:
    """Infer a cash-price factor from turnover/volume VWAP."""

    factor_points: List[Tuple[str, float]] = []
    for row in rows:
        date_key = _date_key(_row_get(row, "date"))
        close = _safe_float(_row_get(row, "close"))
        volume = _safe_float(_row_get(row, "volume"))
        turnover = _safe_float(_row_get(row, "turnover"))
        if turnover is None:
            turnover = _safe_float(_row_get(row, "amount"))
        if not date_key or close is None or volume is None or turnover is None:
            continue
        if close <= 0 or volume <= 0 or turnover <= 0:
            continue
        cash_vwap = turnover / volume
        if cash_vwap <= 0:
            continue
        factor_points.append((date_key, close / cash_vwap))

    if not factor_points:
        return None
    recent_points = sorted(factor_points, key=lambda item: item[0])[-_AMOUNT_VOLUME_FACTOR_LOOKBACK:]
    ratios = [factor for _date_key, factor in recent_points]
    if len(ratios) < _MIN_AMOUNT_VOLUME_FACTOR_ROWS:
        return None

    factor = median(ratios)
    overlap_count = len(ratios)
    if not _is_material_rebase_factor(factor):
        return SequoiaPriceRebase(
            adjusted=False,
            factor=factor,
            overlap_count=overlap_count,
            basis=already_cash_basis,
        )
    if not _is_stable_factor_with_spread(factor, ratios, _MAX_AMOUNT_VOLUME_RELATIVE_SPREAD):
        rolling_factor_points = _rolling_amount_volume_factor_points(recent_points)
        if rolling_factor_points:
            return SequoiaPriceRebase(
                adjusted=True,
                factor=rolling_factor_points[-1][1],
                factor_by_date=tuple(rolling_factor_points),
                overlap_count=overlap_count,
                basis=f"{adjusted_basis}_rolling",
            )
        return SequoiaPriceRebase(
            adjusted=False,
            factor=factor,
            overlap_count=overlap_count,
            basis=unstable_basis,
        )
    logger.debug(
        "OHLC rebased by amount/volume factor %.8f using %d rows",
        factor,
        overlap_count,
    )
    return SequoiaPriceRebase(
        adjusted=True,
        factor=factor,
        overlap_count=overlap_count,
        basis=adjusted_basis,
    )


def _rolling_amount_volume_factor_points(factor_points: Sequence[Tuple[str, float]]) -> Tuple[Tuple[str, float], ...]:
    ordered = sorted(factor_points, key=lambda item: item[0])
    if len(ordered) < _MIN_AMOUNT_VOLUME_FACTOR_ROWS:
        return ()

    raw_factors = [factor for _date_key, factor in ordered]
    smoothed: List[Tuple[str, float]] = []
    for index, (date_key, _factor) in enumerate(ordered):
        start = max(0, index - _AMOUNT_VOLUME_ROLLING_FACTOR_RADIUS)
        end = min(len(raw_factors), index + _AMOUNT_VOLUME_ROLLING_FACTOR_RADIUS + 1)
        window = raw_factors[start:end]
        if len(window) < _MIN_AMOUNT_VOLUME_ROLLING_FACTOR_ROWS:
            if index < _AMOUNT_VOLUME_ROLLING_FACTOR_RADIUS:
                window = raw_factors[:_MIN_AMOUNT_VOLUME_ROLLING_FACTOR_ROWS]
            else:
                window = raw_factors[-_MIN_AMOUNT_VOLUME_ROLLING_FACTOR_ROWS:]
        factor = median(window)
        if factor <= 0:
            return ()
        smoothed.append((date_key, factor))
    if not _is_material_rebase_factor(smoothed[-1][1]):
        return ()
    return tuple(smoothed)


def _load_overlap_factors(
    db_path: Path,
    candidates: Sequence[str],
    sequoia_by_date: Mapping[str, float],
    dates: Sequence[str],
) -> List[Tuple[str, float]]:
    if not candidates or not dates:
        return []

    code_placeholders = ",".join("?" * len(candidates))
    date_placeholders = ",".join("?" * len(dates))
    params = [*candidates, *dates]
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT code, date, close
                    FROM stock_daily
                    WHERE code IN ({code_placeholders})
                      AND date IN ({date_placeholders})
                    ORDER BY date""",
                params,
            ).fetchall()
    except Exception as exc:
        logger.debug("Sequoia cash-price overlap read failed from %s: %s", db_path, exc)
        return []

    ratios: List[Tuple[str, float]] = []
    for row in rows:
        date_key = _date_key(row["date"])
        sequoia_close = sequoia_by_date.get(date_key or "")
        main_close = _safe_float(row["close"])
        if sequoia_close is None or main_close is None or main_close <= 0:
            continue
        ratios.append((date_key or "", float(sequoia_close) / float(main_close)))
    return ratios


def _main_code_candidate_tiers(stock_code: str) -> List[Tuple[str, ...]]:
    text = str(stock_code or "").strip()
    upper = text.upper()
    tiers: List[Tuple[str, ...]] = []

    exact = tuple(dict.fromkeys(value for value in (text, upper) if value))
    if exact:
        tiers.append(exact)
    if upper in _KNOWN_INDEX_SYMBOLS:
        return tiers

    digits = "".join(ch for ch in upper if ch.isdigit())
    stripped = ""
    if upper.startswith(("SH", "SZ")) and len(upper) >= 8 and upper[2:8].isdigit():
        stripped = upper[2:8]
    elif "." in upper and upper.split(".", 1)[0].isdigit():
        stripped = upper.split(".", 1)[0]
    elif len(digits) == 6:
        stripped = digits

    if stripped and stripped not in exact:
        tiers.append((stripped,))

    return tiers


def _is_material_rebase_factor(factor: float) -> bool:
    if factor <= 0:
        return False
    return factor >= _MIN_REBASE_RATIO or factor <= 1.0 / _MIN_REBASE_RATIO


def _all_material_rebase_factors(ratios: Sequence[float]) -> bool:
    return bool(ratios) and all(_is_material_rebase_factor(ratio) for ratio in ratios)


def _is_stable_factor(factor: float, ratios: Sequence[float]) -> bool:
    return _is_stable_factor_with_spread(factor, ratios, _MAX_RELATIVE_SPREAD)


def _is_stable_factor_with_spread(factor: float, ratios: Sequence[float], max_relative_spread: float) -> bool:
    if factor <= 0:
        return False
    if len(ratios) <= 1:
        return True
    relative_spread = max(abs(ratio - factor) / factor for ratio in ratios)
    return relative_spread <= max_relative_spread


def _is_smooth_factor_series(factor_points: Sequence[Tuple[str, float]]) -> bool:
    if len(factor_points) <= 1:
        return True
    ordered = sorted(factor_points, key=lambda item: item[0])
    previous = ordered[0][1]
    for _date_key, factor in ordered[1:]:
        if previous <= 0:
            return False
        if abs(factor - previous) / previous > _MAX_RELATIVE_FACTOR_STEP:
            return False
        previous = factor
    return True


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)


def _date_key(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        return number
    except Exception:
        return None
