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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKTEST_PATH = ROOT / "data/agent_reviews/entry_execution_backtest.jsonl"
DEFAULT_BENCHMARK_DB_PATH = ROOT / "Sequoia-X/data/sequoia_v2.db"
DEFAULT_OUTPUT_PATH = ROOT / "docs/assets/backtest-pnl-benchmark.svg"

STRICT_ENTRY_STRATEGY = ("strict_ai_entry", "Strict AI Entry")
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


def resolve_sample_window(rows: Sequence[dict[str, Any]], *, strategy_key: str) -> tuple[date, date]:
    decision_dates = [parsed for row in rows if (parsed := parse_date(row.get("decision_date")))]
    exit_dates = [
        parsed
        for row in rows
        if isinstance(row.get("strategies"), dict)
        for strategy in [row["strategies"].get(strategy_key)]
        if isinstance(strategy, dict)
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
    end_date: date,
    calendar_dates: Sequence[date] | None = None,
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

    series_dates = _daily_series_dates(
        start_date=start_date,
        end_date=end_date,
        calendar_dates=calendar_dates,
        event_dates=grouped.keys(),
    )
    equity = 1.0
    points: list[SeriesPoint] = []
    for current_date in series_dates:
        for pnl_pct in grouped.get(current_date, []):
            equity *= 1.0 + pnl_pct / 100.0
        points.append(SeriesPoint(current_date, (equity - 1.0) * 100.0))
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
    strategy_key, strategy_label = STRICT_ENTRY_STRATEGY
    start_date, sample_end_date = resolve_sample_window(rows, strategy_key=strategy_key)
    benchmark_items: list[ChartSeries] = []

    for symbol, label in BENCHMARKS:
        benchmark_item = load_benchmark_series(
            benchmark_db_path,
            symbol=symbol,
            label=label,
            start_date=start_date,
            end_date=benchmark_end_date,
        )
        if benchmark_item is not None:
            benchmark_items.append(benchmark_item)

    benchmark_calendar = sorted(
        {
            point.date
            for item in benchmark_items
            for point in item.points
            if start_date <= point.date <= sample_end_date
        }
    )
    strategy_series = build_strategy_series(
        rows,
        key=strategy_key,
        label=strategy_label,
        start_date=start_date,
        end_date=sample_end_date,
        calendar_dates=benchmark_calendar,
    )
    series = ([strategy_series] if strategy_series is not None else []) + benchmark_items

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
            drawstyle="steps-post" if item.kind == "strategy" else "default",
            alpha=0.96,
        )

    ax.axhline(0, color="#111827", linewidth=0.9, alpha=0.55)
    ax.set_title("Strict Entry Backtest: Daily Cumulative PnL vs Benchmarks", loc="left", fontsize=15, pad=16)
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
        "Strict-entry PnL compounds filled trades by exit date and carries forward on trading days. "
        f"Window: {start_date.isoformat()} to {sample_end_date.isoformat()}."
    )
    fig.text(0.075, 0.02, note, ha="left", va="bottom", fontsize=8.5, color="#4b5563")
    fig.tight_layout(rect=(0, 0.04, 1, 1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="svg", metadata={"Date": None})
    plt.close(fig)
    _strip_trailing_whitespace(output_path)


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


def _daily_series_dates(
    *,
    start_date: date,
    end_date: date,
    calendar_dates: Sequence[date] | None,
    event_dates: Iterable[date],
) -> list[date]:
    if calendar_dates:
        dates = {day for day in calendar_dates if start_date <= day <= end_date}
    else:
        span_days = max(0, (end_date - start_date).days)
        dates = {start_date + timedelta(days=offset) for offset in range(span_days + 1)}
    dates.add(start_date)
    dates.add(end_date)
    dates.update(day for day in event_dates if start_date <= day <= end_date)
    return sorted(dates)


def _strip_trailing_whitespace(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(line.rstrip() for line in lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
