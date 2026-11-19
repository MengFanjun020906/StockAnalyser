# -*- coding: utf-8 -*-
"""Read-only data access for factor evaluation.

All A-share factors use Tushare snapshots keyed by trading day:

- ``daily``        : unadjusted OHLCV for the full market
- ``adj_factor``   : cumulative adjustment factor, joined to build adjusted prices
- ``daily_basic``  : point-in-time market cap, turnover and valuation
- ``index_daily``  : benchmark series (A-share default ``000985.CSI``)

The module deliberately does not guess index suffixes.  A missing benchmark is
an evaluation error, not a reason to switch to another index.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pandas as pd

from data_provider.tushare_client import query_tushare_api

logger = logging.getLogger(__name__)

BAR_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
BASIC_FIELDS = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe_ttm,pb,total_mv,circ_mv"
)
ADJ_FIELDS = "ts_code,trade_date,adj_factor"
STOCK_BASIC_FIELDS = "ts_code,name,industry,market,list_date,delist_date,list_status"
INDUSTRY_FIELDS = "l1_code,l1_name,ts_code,name,in_date,out_date,is_new"
_DATE_COLUMNS = ("list_date", "delist_date", "in_date", "out_date")


class EvaluationDataError(RuntimeError):
    """Raised when required evaluation data cannot be loaded."""


@dataclass
class TushareEvaluationSource:
    """Tushare-backed snapshot loader with an optional CSV cache."""

    cache_dir: Optional[Path] = None
    sleep_seconds: float = 0.0
    query: Optional[Callable[..., pd.DataFrame]] = None

    def __post_init__(self) -> None:
        self._query = self.query or query_tushare_api
        if self.cache_dir:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # public loaders
    # ------------------------------------------------------------------

    def load_calendar(self, start: str, end: str) -> List[str]:
        df = self._call(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": start,
                "end_date": end,
                "is_open": "1",
            },
            fields="cal_date",
        )
        if df is None or df.empty:
            raise EvaluationDataError(f"empty trading calendar for {start}..{end}")
        dates = sorted(str(value) for value in df["cal_date"].tolist())
        return dates

    def load_bars(self, day: str) -> pd.DataFrame:
        return self._daily_cached("bars", day, "daily", BAR_FIELDS)

    def load_basics(self, day: str) -> pd.DataFrame:
        return self._daily_cached("basic", day, "daily_basic", BASIC_FIELDS)

    def load_adj_factors(self, day: str) -> pd.DataFrame:
        return self._daily_cached("adj", day, "adj_factor", ADJ_FIELDS)

    def load_stock_basic(self) -> pd.DataFrame:
        """Listed ∪ delisted pool (``list_status`` L/D), deduplicated by code.

        Dropping delisted stocks biases every cross-sectional result, so the
        evaluation universe must contain both statuses.  ``delist_date`` is
        kept so the evaluator can apply the delisted-exit rule.
        """

        path = self._cache_path("stock_basic_LD.csv")
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        frames: List[pd.DataFrame] = []
        for status in ("L", "D"):
            df = self._call(
                "stock_basic",
                {"exchange": "", "list_status": status},
                fields=STOCK_BASIC_FIELDS,
            )
            if df is not None and not df.empty:
                frames.append(self._normalize(df))
        if not frames:
            raise EvaluationDataError("empty stock_basic response for list_status L/D")
        pool = pd.concat(frames, ignore_index=True, sort=False)
        pool = pool.drop_duplicates("ts_code", keep="first").reset_index(drop=True)
        self._write_cache(path, pool)
        return pool

    def load_industry_snapshot(self) -> pd.DataFrame:
        """SW2021 L1 membership snapshot, cached as a single CSV.

        One ``index_classify`` call lists the 31 L1 industries, then one
        ``index_member_all(l1_code=..., is_new="Y")`` call per industry.  Only
        the latest SW2021 classification is available, so ``in_date`` is kept
        and the evaluator must refuse to assign an industry before that date.
        """

        path = self._cache_path("industry_snapshot_sw2021.csv")
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        frames: List[pd.DataFrame] = []
        for code, _name in self._sw_l1_codes():
            df = self._call(
                "index_member_all",
                {"l1_code": code, "is_new": "Y"},
                fields=INDUSTRY_FIELDS,
            )
            if df is not None and not df.empty:
                frames.append(self._normalize(df))
        if not frames:
            raise EvaluationDataError("empty SW2021 L1 membership snapshot")
        snapshot = pd.concat(frames, ignore_index=True, sort=False)
        snapshot = snapshot.dropna(subset=["ts_code"])
        # A code may appear in more than one historical membership row; keep
        # the latest `in_date` so the snapshot is one industry per stock.
        snapshot = snapshot.sort_values("in_date").drop_duplicates("ts_code", keep="last")
        snapshot = snapshot.reset_index(drop=True)
        self._write_cache(path, snapshot)
        return snapshot

    def load_benchmark(self, code: str, start: str, end: str) -> pd.DataFrame:
        if not code:
            raise EvaluationDataError("benchmark code is empty")
        path = self._cache_path(f"benchmark_{_safe(code)}_{start}_{end}.csv")
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        try:
            df = self._call(
                "index_daily",
                {"ts_code": code, "start_date": start, "end_date": end},
                fields="trade_date,open,close",
            )
        except Exception as exc:
            raise EvaluationDataError(
                f"benchmark {code} fetch failed: {type(exc).__name__}: {exc}"
            ) from exc
        if df is None or df.empty:
            raise EvaluationDataError(
                f"benchmark {code} returned no rows for {start}..{end}; "
                "refusing to substitute another index"
            )
        df = self._normalize(df).sort_values("trade_date").reset_index(drop=True)
        self._write_cache(path, df)
        return df

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _sw_l1_codes(self) -> List[Tuple[str, str]]:
        """Return ``[(l1_code, l1_name), ...]`` for SW2021 level-1 industries."""

        path = self._cache_path("index_classify_sw2021_l1.csv")
        cached = self._read_cache(path)
        if cached is not None:
            return [(str(row.index_code), str(row.industry_name)) for row in cached.itertuples()]
        df = self._call(
            "index_classify",
            {"level": "L1", "src": "SW2021"},
            fields="index_code,industry_name,level,src",
        )
        df = pd.DataFrame() if df is None else self._normalize(df)
        if df.empty:
            raise EvaluationDataError("empty SW2021 L1 classification")
        self._write_cache(path, df)
        return [(str(row.index_code), str(row.industry_name)) for row in df.itertuples()]

    def _daily_cached(
        self, kind: str, day: str, api: str, fields: str
    ) -> pd.DataFrame:
        path = self._cache_path(f"{kind}_{day}.csv")
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        df = self._call(api, {"trade_date": day}, fields=fields)
        df = pd.DataFrame() if df is None else self._normalize(df)
        self._write_cache(path, df)
        return df

    def _call(self, api: str, params: dict, fields: str) -> pd.DataFrame:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return self._query(api, params, fields=fields, timeout=30)

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if "trade_date" in out.columns:
            out["trade_date"] = out["trade_date"].astype(str)
        if "cal_date" in out.columns:
            out["cal_date"] = out["cal_date"].astype(str)
        if "ts_code" in out.columns:
            out["ts_code"] = out["ts_code"].astype(str)
        for column in _DATE_COLUMNS:
            if column in out.columns:
                out[column] = out[column].map(_date_text)
        return out

    def _cache_path(self, name: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        return self.cache_dir / name

    def _read_cache(self, path: Optional[Path]) -> Optional[pd.DataFrame]:
        if path is None or not path.exists():
            return None
        try:
            dtype = {"ts_code": str, "trade_date": str}
            dtype.update({column: str for column in _DATE_COLUMNS})
            return self._normalize(pd.read_csv(path, dtype=dtype))
        except Exception as exc:  # pragma: no cover - corrupt cache is non-fatal
            logger.warning("ignoring unreadable factor-eval cache %s: %s", path, exc)
            return None

    def _write_cache(self, path: Optional[Path], df: pd.DataFrame) -> None:
        if path is None:
            return
        try:
            df.to_csv(path, index=False)
        except Exception as exc:  # pragma: no cover - cache is optional
            logger.warning("failed to write factor-eval cache %s: %s", path, exc)


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def _date_text(value) -> str:
    """Normalize an optional YYYYMMDD value to ``""`` when absent."""

    if value is None or value is pd.NA:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else str(int(value))
    text = str(value).strip()
    return "" if text in {"", "nan", "None", "NaT", "<NA>"} else text


def code6(ts_code: str) -> str:
    return str(ts_code).split(".")[0]
