# -*- coding: utf-8 -*-

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

chart = importlib.import_module("render_backtest_readme_chart")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")


def test_build_strategy_series_compounds_filled_trades_and_carries_daily_values(tmp_path: Path) -> None:
    rows = [
        {
            "decision_date": "2026-01-01",
            "strategies": {
                "strict_ai_entry": {
                    "status": "filled",
                    "exit_date": "2026-01-03 15:00:00",
                    "pnl_pct": 10,
                }
            },
        },
        {
            "decision_date": "2026-01-02",
            "strategies": {
                "strict_ai_entry": {
                    "status": "not_filled",
                    "exit_date": "2026-01-04 15:00:00",
                    "pnl_pct": 99,
                }
            },
        },
        {
            "decision_date": "2026-01-02",
            "strategies": {
                "strict_ai_entry": {
                    "status": "filled",
                    "exit_date": "2026-01-05 15:00:00",
                    "pnl_pct": -5,
                }
            },
        },
    ]

    series = chart.build_strategy_series(
        rows,
        key="strict_ai_entry",
        label="Strict AI Entry",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 5),
        calendar_dates=[
            date(2026, 1, 1),
            date(2026, 1, 2),
            date(2026, 1, 3),
            date(2026, 1, 4),
            date(2026, 1, 5),
        ],
    )

    assert series is not None
    assert [point.date for point in series.points] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ]
    assert [point.value_pct for point in series.points] == pytest.approx([0.0, 0.0, 10.0, 10.0, 4.5])
    assert series.points[-1].value_pct == pytest.approx(4.5)


def test_load_benchmark_series_normalizes_close_prices(tmp_path: Path) -> None:
    db_path = tmp_path / "sequoia.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE stock_daily (symbol TEXT, date TEXT, close REAL)")
        conn.executemany(
            "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
            [
                ("000001.SH", "2026-01-01", 100.0),
                ("000001.SH", "2026-01-02", 105.0),
                ("000001.SH", "2026-01-03", 102.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    series = chart.load_benchmark_series(
        db_path,
        symbol="000001.SH",
        label="SSE Composite",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 3),
    )

    assert series is not None
    assert [point.value_pct for point in series.points] == pytest.approx([0.0, 5.0, 2.0])


def test_build_chart_series_reads_backtest_and_benchmark_inputs(tmp_path: Path) -> None:
    backtest_path = tmp_path / "entry_execution_backtest.jsonl"
    _write_jsonl(
        backtest_path,
        [
            {
                "decision_date": "2026-01-01",
                "strategies": {
                    "strict_ai_entry": {
                        "status": "filled",
                        "exit_date": "2026-01-03 15:00:00",
                        "pnl_pct": 10,
                    },
                    "next_open_baseline": {
                        "status": "filled",
                        "exit_date": "2026-01-03 15:00:00",
                        "pnl_pct": 50,
                    }
                },
            }
        ],
    )
    db_path = tmp_path / "sequoia.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE stock_daily (symbol TEXT, date TEXT, close REAL)")
        conn.executemany(
            "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
            [
                ("000001.SH", "2026-01-01", 100.0),
                ("000001.SH", "2026-01-03", 90.0),
                ("000300.SH", "2026-01-01", 200.0),
                ("000300.SH", "2026-01-03", 220.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    start_date, sample_end_date, series = chart.build_chart_series(
        backtest_path,
        db_path,
        benchmark_end_date=date(2026, 1, 3),
    )

    assert start_date == date(2026, 1, 1)
    assert sample_end_date == date(2026, 1, 3)
    assert {item.key for item in series} == {"strict_ai_entry", "000001.SH", "000300.SH"}
    assert next(item for item in series if item.key == "strict_ai_entry").latest_pct == pytest.approx(10.0)
