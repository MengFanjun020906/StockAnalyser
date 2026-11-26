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

import csv
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from data_provider.tushare_client import query_tushare_api

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]

# Bare 6-digit codes are shared by stocks and indices (``000985`` is both the
# CSI All Share index and 大庆华科).  Only this explicit allow-list may be
# upgraded to a qualified index identifier; everything else must already carry
# an exchange/type suffix.  Index levels are never resolved from ``stock_daily``.
_CN_BENCHMARK_ALIASES = {
    "000985": "000985.CSI",
    "000300": "000300.SH",
    "000001": "000001.SH",
    "000905": "000905.SH",
    "000688": "000688.SH",
    "399001": "399001.SZ",
    "399006": "399006.SZ",
}


@dataclass(frozen=True)
class BenchmarkSeries:
    """Index benchmark bars keyed by trading day."""

    code: str
    bars: Dict[date, Dict[str, float]]
    source: str  # "cache" | "cache+tushare" | "tushare" | "none"
    files: Tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.bars)


_SERIES_CACHE: Dict[Tuple[str, str], BenchmarkSeries] = {}


def reset_benchmark_series_cache() -> None:
    """Drop the in-process benchmark cache (tests / long-lived workers)."""

    _SERIES_CACHE.clear()


def normalize_benchmark_code(code: str, market: str = "cn") -> Optional[str]:
    """Return a qualified index identifier, or ``None`` when not resolvable.

    Qualified codes such as ``000985.CSI`` / ``000001.SH`` are returned as-is.
    Bare codes are only accepted for the fixed CN index allow-list above; the
    function never inspects stock bars.
    """

    text = str(code or "").strip()
    if not text:
        return None
    if "." in text:
        return text.upper()
    if market != "cn":
        return None
    return _CN_BENCHMARK_ALIASES.get(text)


def default_benchmark_cache_dir() -> Path:
    """Resolve the factor-eval cache dir (env > config > ``data/factor_eval_cache``)."""

    raw = os.getenv("INDEX_BENCHMARK_CACHE_DIR", "").strip()
    if not raw:
        try:
            from src.config import get_config

            raw = str(getattr(get_config(), "index_benchmark_cache_dir", "") or "").strip()
        except Exception:  # pragma: no cover - config is optional for data access
            raw = ""
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else _ROOT / candidate
    return _ROOT / "data" / "factor_eval_cache"


def load_benchmark_series(
    code: str,
    start: date,
    end: date,
    *,
    cache_dir: Optional[Path | str] = None,
    allow_fetch: Optional[bool] = None,
    query: Optional[Callable[..., pd.DataFrame]] = None,
) -> BenchmarkSeries:
    """Load an index benchmark from the ``index_daily`` CSV cache.

    Existing ``benchmark_{safe(code)}_{start}_{end}.csv`` files are merged so a
    caller asking for an arbitrary window still gets the cached index levels.
    Only when the cached coverage misses part of the window *and* fetching is
    explicitly allowed do we call :meth:`TushareEvaluationSource.load_benchmark`
    (which is ``index_daily``).  Fetch failures are logged, never fabricated.
    """

    normalized = normalize_benchmark_code(code)
    if normalized is None:
        return BenchmarkSeries(code=str(code or ""), bars={}, source="none")

    directory = Path(cache_dir).expanduser() if cache_dir else default_benchmark_cache_dir()
    cache_key = (normalized, str(directory))
    cached = _SERIES_CACHE.get(cache_key)
    if cached is None:
        bars: Dict[date, Dict[str, float]] = {}
        files: List[str] = []
        for path in sorted(directory.glob(f"benchmark_{_safe(normalized)}_*.csv")):
            files.append(path.name)
            for day, bar in _read_benchmark_csv(path):
                bars.setdefault(day, bar)
        cached = BenchmarkSeries(
            code=normalized,
            bars=bars,
            source="cache" if bars else "none",
            files=tuple(files),
        )
        _SERIES_CACHE[cache_key] = cached

    if _should_fetch(cached, start, end, allow_fetch):
        fetched = _fetch_benchmark(normalized, start, end, directory, query)
        if fetched:
            merged = dict(cached.bars)
            for day, bar in fetched.items():
                merged.setdefault(day, bar)
            cached = BenchmarkSeries(
                code=normalized,
                bars=merged,
                source="cache+tushare" if cached.bars else "tushare",
                files=cached.files,
            )
            _SERIES_CACHE[cache_key] = cached

    return BenchmarkSeries(
        code=cached.code,
        bars={day: bar for day, bar in cached.bars.items() if start <= day <= end},
        source=cached.source,
        files=cached.files,
    )


def _read_benchmark_csv(path: Path) -> List[Tuple[date, Dict[str, float]]]:
    rows: List[Tuple[date, Dict[str, float]]] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            for item in csv.DictReader(handle):
                day = _parse_benchmark_date(item.get("trade_date") or item.get("date"))
                if day is None:
                    continue
                close = _to_float(item.get("close"))
                if close is None:
                    continue
                rows.append((day, {"open": _to_float(item.get("open")), "close": close}))
    except OSError as exc:  # pragma: no cover - unreadable cache is non-fatal
        logger.warning("ignoring unreadable benchmark cache %s: %s", path, exc)
    return rows


def _parse_benchmark_date(value) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    if "-" in text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            return None
    return None


def _to_float(value) -> Optional[float]:
    try:
        if value in (None, "", "nan", "None"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _should_fetch(
    series: BenchmarkSeries, start: date, end: date, allow_fetch: Optional[bool]
) -> bool:
    if allow_fetch is None:
        raw = os.getenv("INDEX_BENCHMARK_ALLOW_FETCH", "").strip().lower()
        if raw:
            allow_fetch = raw in {"1", "true", "yes", "on"}
        else:
            try:
                from src.config import get_config

                allow_fetch = bool(getattr(get_config(), "index_benchmark_allow_fetch", False))
            except Exception:  # pragma: no cover
                allow_fetch = False
    if not allow_fetch:
        return False
    if not series.bars:
        return True
    return min(series.bars) > start or max(series.bars) < end


def _fetch_benchmark(
    code: str,
    start: date,
    end: date,
    cache_dir: Path,
    query: Optional[Callable[..., pd.DataFrame]],
) -> Dict[date, Dict[str, float]]:
    try:
        source = TushareEvaluationSource(cache_dir=cache_dir, query=query)
        frame = source.load_benchmark(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    except Exception as exc:  # pragma: no cover - network / token failures are expected
        logger.warning("benchmark %s index_daily fetch skipped: %s: %s", code, type(exc).__name__, exc)
        return {}
    bars: Dict[date, Dict[str, float]] = {}
    for row in frame.itertuples(index=False):
        day = _parse_benchmark_date(getattr(row, "trade_date", None))
        close = _to_float(getattr(row, "close", None))
        if day is None or close is None:
            continue
        bars[day] = {"open": _to_float(getattr(row, "open", None)), "close": close}
    return bars

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
