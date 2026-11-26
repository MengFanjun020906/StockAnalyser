# -*- coding: utf-8 -*-
"""Index benchmark cache reader: qualified codes only, never stock bars."""

from __future__ import annotations

from datetime import date

import pytest

from src.backtest.factor_eval import data as benchmark_data


@pytest.fixture(autouse=True)
def _clear_cache():
    benchmark_data.reset_benchmark_series_cache()
    yield
    benchmark_data.reset_benchmark_series_cache()


def _write(directory, name, rows):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("w", encoding="utf-8") as handle:
        handle.write("trade_date,open,close\n")
        for day, open_, close in rows:
            handle.write(f"{day.strftime('%Y%m%d')},{open_},{close}\n")
    return path


def test_normalize_benchmark_code_requires_qualified_or_allowlisted():
    assert benchmark_data.normalize_benchmark_code("000985.CSI") == "000985.CSI"
    assert benchmark_data.normalize_benchmark_code("000001.sh") == "000001.SH"
    assert benchmark_data.normalize_benchmark_code("000985") == "000985.CSI"
    assert benchmark_data.normalize_benchmark_code("600519") is None
    assert benchmark_data.normalize_benchmark_code("HSI") is None
    assert benchmark_data.normalize_benchmark_code("") is None


def test_merges_multiple_cache_files_and_filters_window(tmp_path):
    cache = tmp_path / "cache"
    _write(cache, "benchmark_000985_CSI_20240102_20240103.csv", [(date(2024, 1, 2), 100.0, 101.0)])
    _write(cache, "benchmark_000985_CSI_20240103_20240104.csv", [(date(2024, 1, 3), 101.0, 102.0)])

    series = benchmark_data.load_benchmark_series(
        "000985.CSI", date(2024, 1, 2), date(2024, 1, 3), cache_dir=cache
    )

    assert series.code == "000985.CSI"
    assert series.source == "cache"
    assert sorted(series.bars) == [date(2024, 1, 2), date(2024, 1, 3)]
    assert series.bars[date(2024, 1, 2)] == {"open": 100.0, "close": 101.0}


def test_unknown_code_returns_empty_without_touching_cache(tmp_path):
    cache = tmp_path / "cache"
    _write(cache, "benchmark_000985_CSI_20240102_20240103.csv", [(date(2024, 1, 2), 100.0, 101.0)])

    series = benchmark_data.load_benchmark_series("600519", date(2024, 1, 2), date(2024, 1, 3), cache_dir=cache)

    assert series.bars == {}
    assert series.source == "none"


def test_allow_fetch_failure_is_non_fatal(tmp_path, monkeypatch):
    cache = tmp_path / "cache"

    def _boom(self, code, start, end):
        raise RuntimeError("token expired")

    monkeypatch.setattr(benchmark_data.TushareEvaluationSource, "load_benchmark", _boom)

    series = benchmark_data.load_benchmark_series(
        "000985.CSI", date(2024, 1, 2), date(2024, 1, 3), cache_dir=cache, allow_fetch=True
    )

    assert series.bars == {}
    assert series.source == "none"
