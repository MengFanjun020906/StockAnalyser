#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the README entry-execution backtest chart as a static SVG."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKTEST_PATH = ROOT / "data/agent_reviews/entry_execution_backtest.jsonl"
DEFAULT_BENCHMARK_DB_PATH = ROOT / "Sequoia-X/data/sequoia_v2.db"
DEFAULT_OUTPUT_PATH = ROOT / "docs/assets/backtest-pnl-benchmark.svg"

STRATEGIES = (
    ("strict_ai_entry", "Strict AI Entry"),
    ("next_open_baseline", "Next Open Baseline"),
    ("atr_elastic_entry", "ATR Elastic Entry"),
)
BENCHMARKS = (
    ("000001.SH", "SSE Composite"),
    ("000300.SH", "CSI 300"),
)


@dataclass(frozen=True)
class SeriesPoint:
    date: date
    value_pct: float


@dataclass(frozen=True)
class ChartSeries:
    key: str
    label: str
    kind: str
    points: tuple[SeriesPoint, ...]

    @property
    def latest_pct(self) -> float | None:
        return self.points[-1].value_pct if self.points else None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def load_backtest_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def resolve_sample_window(rows: Sequence[dict[str, Any]]) -> tuple[date, date]:
    decision_dates = [parsed for row in rows if (parsed := parse_date(row.get("decision_date")))]
    exit_dates = [
        parsed
        for row in rows
        for strategy in _strategy_payloads(row)
        if strategy.get("status") == "filled"
        if (parsed := parse_date(strategy.get("exit_date")))
    ]
    if not decision_dates:
        raise ValueError("No decision_date values found in backtest rows.")
    start_date = min(decision_dates)
    end_date = max(exit_dates or decision_dates)
    return start_date, end_date


def build_strategy_series(
    rows: Sequence[dict[str, Any]],
    *,
    key: str,
    label: str,
    start_date: date,
) -> ChartSeries | None:
    grouped: dict[date, list[float]] = defaultdict(list)
    for row in rows:
        strategy = row.get("strategies", {}).get(key)
        if not isinstance(strategy, dict) or strategy.get("status") != "filled":
            continue
        exit_date = parse_date(strategy.get("exit_date"))
        pnl_pct = _safe_float(strategy.get("pnl_pct"))
        if exit_date is None or pnl_pct is None:
            continue
        grouped[exit_date].append(pnl_pct)

    if not grouped:
        return None

    equity = 1.0
    points = [SeriesPoint(start_date, 0.0)]
    for event_date in sorted(grouped):
        for pnl_pct in grouped[event_date]:
            equity *= 1.0 + pnl_pct / 100.0
        points.append(SeriesPoint(event_date, (equity - 1.0) * 100.0))
    return ChartSeries(key=key, label=label, kind="strategy", points=tuple(points))


def load_benchmark_series(
    db_path: Path,
    *,
    symbol: str,
    label: str,
    start_date: date,
    end_date: date,
) -> ChartSeries | None:
    if not db_path.exists():
        return None

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT date, close
            FROM stock_daily
            WHERE symbol = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (symbol, start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    values = [(parse_date(row[0]), _safe_float(row[1])) for row in rows]
    values = [(day, close) for day, close in values if day is not None and close and close > 0]
    if not values:
        return None

    base_close = values[0][1]
    points = tuple(SeriesPoint(day, (close / base_close - 1.0) * 100.0) for day, close in values)
    return ChartSeries(key=symbol, label=label, kind="benchmark", points=points)


def build_chart_series(
    backtest_path: Path,
    benchmark_db_path: Path,
    *,
    benchmark_end_date: date,
) -> tuple[date, date, list[ChartSeries]]:
    rows = load_backtest_rows(backtest_path)
    start_date, sample_end_date = resolve_sample_window(rows)
    series: list[ChartSeries] = []

    for key, label in STRATEGIES:
        strategy_series = build_strategy_series(rows, key=key, label=label, start_date=start_date)
        if strategy_series is not None:
            series.append(strategy_series)

    for symbol, label in BENCHMARKS:
        benchmark_series = load_benchmark_series(
            benchmark_db_path,
            symbol=symbol,
            label=label,
            start_date=start_date,
            end_date=benchmark_end_date,
        )
        if benchmark_series is not None:
            series.append(benchmark_series)

    return start_date, sample_end_date, series


def render_svg(series: Sequence[ChartSeries], output_path: Path, *, start_date: date, sample_end_date: date) -> None:
    cache_root = Path(tempfile.gettempdir()) / "stockanalyser-matplotlib-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_root / "matplotlib"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))

    try:
        import matplotlib

        matplotlib.use("Agg")
        matplotlib.rcParams["svg.fonttype"] = "none"
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except ImportError as exc:
        raise SystemExit(
            "matplotlib is required. Install dependencies with: uv pip install -r requirements.txt"
        ) from exc

    if not series:
        raise ValueError("No chart series to render.")

    colors = {
        "strict_ai_entry": "#2563eb",
        "next_open_baseline": "#dc2626",
        "atr_elastic_entry": "#7c3aed",
        "000001.SH": "#059669",
        "000300.SH": "#d97706",
    }
    linestyles = {"strategy": "-", "benchmark": "--"}

    fig, ax = plt.subplots(figsize=(11.0, 6.0), dpi=140)
    for item in series:
        dates = [point.date for point in item.points]
        values = [point.value_pct for point in item.points]
        latest = item.latest_pct
        suffix = f" ({latest:+.1f}%)" if latest is not None else ""
        ax.plot(
            dates,
            values,
            label=f"{item.label}{suffix}",
            color=colors.get(item.key),
            linewidth=2.4 if item.kind == "strategy" else 2.0,
            linestyle=linestyles.get(item.kind, "-"),
            marker="o" if item.kind == "strategy" else None,
            markersize=3.4 if item.kind == "strategy" else 0,
            alpha=0.96,
        )

    ax.axhline(0, color="#111827", linewidth=0.9, alpha=0.55)
    ax.set_title("Entry Execution Backtest: Cumulative PnL vs Benchmarks", loc="left", fontsize=15, pad=16)
    ax.set_ylabel("Cumulative return")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}%"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.grid(True, axis="y", color="#e5e7eb", linewidth=0.8)
    ax.grid(True, axis="x", color="#f3f4f6", linewidth=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="lower left", frameon=False, fontsize=9)

    note = (
        "Strategy curves compound filled trades by exit date; unfilled signals affect trigger rate, not PnL. "
        f"Sample window: {start_date.isoformat()} to {sample_end_date.isoformat()}."
    )
    fig.text(0.075, 0.02, note, ha="left", va="bottom", fontsize=8.5, color="#4b5563")
    fig.tight_layout(rect=(0, 0.04, 1, 1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="svg", metadata={"Date": None})
    plt.close(fig)


def summarize(series: Sequence[ChartSeries]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "kind": item.kind,
            "point_count": len(item.points),
            "latest_pct": round(item.latest_pct, 4) if item.latest_pct is not None else None,
            "start_date": item.points[0].date.isoformat() if item.points else None,
            "end_date": item.points[-1].date.isoformat() if item.points else None,
        }
        for item in series
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render README backtest benchmark chart.")
    parser.add_argument("--backtest-jsonl", type=Path, default=DEFAULT_BACKTEST_PATH)
    parser.add_argument("--benchmark-db", type=Path, default=DEFAULT_BENCHMARK_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--benchmark-end-date", default=date.today().isoformat())
    args = parser.parse_args(argv)

    benchmark_end_date = parse_date(args.benchmark_end_date)
    if benchmark_end_date is None:
        raise SystemExit(f"Invalid --benchmark-end-date: {args.benchmark_end_date}")

    start_date, sample_end_date, series = build_chart_series(
        args.backtest_jsonl,
        args.benchmark_db,
        benchmark_end_date=benchmark_end_date,
    )
    render_svg(series, args.output, start_date=start_date, sample_end_date=sample_end_date)
    print(
        json.dumps(
            {
                "output_path": str(args.output),
                "start_date": start_date.isoformat(),
                "sample_end_date": sample_end_date.isoformat(),
                "series": summarize(series),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strategy_payloads(row: dict[str, Any]) -> Iterable[dict[str, Any]]:
    strategies = row.get("strategies")
    if not isinstance(strategies, dict):
        return []
    return [value for value in strategies.values() if isinstance(value, dict)]


if __name__ == "__main__":
    raise SystemExit(main())
