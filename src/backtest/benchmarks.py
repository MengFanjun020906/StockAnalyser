# -*- coding: utf-8 -*-
"""Multi-index benchmark series for the equity chart.

Benchmarks are display/audit series only: they do not affect order sizing,
cash, P&L or risk gates.  The series are normalised to the run's initial equity
so they can be compared with the strategy curve on the same axis.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARKS: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"),
)

BenchmarkLoader = Callable[[str, date, date], dict[date, Decimal] | None]


@dataclass
class BenchmarkSpec:
    symbol: str
    name: str


def build_benchmark_payload(
    *,
    run_id: str,
    sessions: list[date],
    initial_equity: Decimal,
    loader: BenchmarkLoader | None = None,
    cache_path: Path | None = None,
    strategy_return_pct: float | None = None,
    specs: Iterable[tuple[str, str]] = DEFAULT_BENCHMARKS,
) -> dict[str, Any]:
    if cache_path is not None and cache_path.exists():
        try:
            return _attach_strategy_returns(
                json.loads(cache_path.read_text(encoding="utf-8")),
                strategy_return_pct,
            )
        except Exception:
            logger.warning("failed to read benchmark cache %s", cache_path, exc_info=True)
    if not sessions:
        return {"run_id": run_id, "initial_equity": str(initial_equity), "series": [], "warnings": []}
    loader = loader or _default_loader
    start = sessions[0] - timedelta(days=14)
    end = sessions[-1] + timedelta(days=1)
    series: list[dict[str, Any]] = []
    warnings: list[str] = []
    for symbol, name in specs:
        try:
            closes = loader(symbol, start, end) or {}
        except Exception as exc:
            logger.warning("benchmark %s load failed: %s", symbol, exc)
            closes = {}
            warnings.append(f"{symbol}:load_failed")
        if not closes:
            warnings.append(f"{symbol}:unavailable")
            series.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "status": "unavailable",
                    "points": [],
                }
            )
            continue
        ordered = sorted(closes.items())
        base: Decimal | None = None
        carried: Decimal | None = None
        points = []
        for session in sessions:
            value = closes.get(session)
            if value is not None and value > 0:
                carried = value
            if carried is None:
                points.append({"session": session.isoformat(), "value": None})
                continue
            if base is None:
                base = carried
            normalized = initial_equity * carried / base
            points.append({"session": session.isoformat(), "value": float(normalized)})
        if base is None:
            warnings.append(f"{symbol}:no_base")
            series.append(
                {"symbol": symbol, "name": name, "status": "unavailable", "points": []}
            )
            continue
        series.append(
            {
                "symbol": symbol,
                "name": name,
                "status": "ok",
                "points": points,
                "first_session": ordered[0][0].isoformat(),
                "last_session": ordered[-1][0].isoformat(),
            }
        )
    payload = {
        "run_id": run_id,
        "initial_equity": str(initial_equity),
        "series": series,
        "warnings": sorted(set(warnings)),
        "source": "tushare:index_daily",
        "note": "benchmark series are display-only and normalised to initial equity",
    }
    payload = _attach_strategy_returns(payload, strategy_return_pct)
    if cache_path is not None:
        try:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("failed to write benchmark cache %s", cache_path, exc_info=True)
    return payload


def _default_loader(symbol: str, start: date, end: date) -> dict[date, Decimal] | None:
    rows = _tushare_index(symbol, start, end)
    if rows:
        return rows
    try:
        from .research.sequoia import SequoiaDailySource

        source = SequoiaDailySource()
        if source.available:
            return source.benchmark(symbol, start_date=start, end_date=end)
    except Exception:
        logger.warning("Sequoia benchmark fallback failed for %s", symbol, exc_info=True)
    return None


def _tushare_index(symbol: str, start: date, end: date) -> dict[date, Decimal]:
    try:
        from src.config import setup_env

        setup_env()
        from data_provider.tushare_client import query_tushare_api

        df = query_tushare_api(
            "index_daily",
            {
                "ts_code": symbol,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            },
            fields="trade_date,close",
            timeout=15,
        )
        result: dict[date, Decimal] = {}
        if df is None or df.empty:
            return result
        for row in df.to_dict("records"):
            raw = str(row.get("trade_date") or "")
            close = row.get("close")
            if len(raw) != 8:
                continue
            try:
                result[date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))] = Decimal(str(close))
            except Exception:
                continue
        return result
    except Exception:
        logger.warning("Tushare index_daily unavailable for %s", symbol, exc_info=True)
        return {}


def _attach_strategy_returns(payload: dict[str, Any], strategy_return_pct: float | None) -> dict[str, Any]:
    initial = float(payload.get("initial_equity") or 0)
    benchmark_returns: dict[str, float] = {}
    for series in payload.get("series", []):
        symbol = str(series.get("symbol") or "")
        last_value = None
        for point in reversed(series.get("points", [])):
            if point.get("value") is not None:
                last_value = float(point["value"])
                break
        if symbol and initial > 0 and last_value is not None:
            benchmark_returns[symbol] = (last_value / initial - 1.0) * 100.0
    payload["benchmark_returns"] = benchmark_returns
    if strategy_return_pct is not None:
        strategy = float(strategy_return_pct)
        payload["strategy_return_pct"] = strategy
    else:
        strategy = payload.get("strategy_return_pct")
    shanghai = benchmark_returns.get("000001.SH")
    if shanghai is not None:
        payload["shanghai_return_pct"] = float(shanghai)
    if strategy is not None and shanghai is not None:
        payload["excess_vs_shanghai_pct"] = float(strategy) - float(shanghai)
    return payload


__all__ = ["DEFAULT_BENCHMARKS", "build_benchmark_payload"]
