# -*- coding: utf-8 -*-
"""Tests for the Kronos model A/B runner helpers."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd


def _load_script_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_kronos_model_ab.py"
    spec = importlib.util.spec_from_file_location("run_kronos_model_ab", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_models_expands_aliases() -> None:
    module = _load_script_module()

    assert module.parse_models("small,base,custom/model") == [
        "NeoQuasar/Kronos-small",
        "NeoQuasar/Kronos-base",
        "custom/model",
    ]


def test_seed_case_is_stable_for_same_case() -> None:
    module = _load_script_module()

    first = module.seed_case(100, "small", "600519", "2026-06-30", "5")
    second = module.seed_case(100, "small", "600519", "2026-06-30", "5")
    different = module.seed_case(100, "base", "600519", "2026-06-30", "5")

    assert first == second
    assert first != different
    assert module.seed_case(-1, "small") is None


def test_summarize_computes_per_model_metrics() -> None:
    module = _load_script_module()

    summary = module.summarize(
        [
            {
                "model": "small",
                "status": "ok",
                "actual_status": "ok",
                "direction_correct": True,
                "absolute_return_error_pct": 1.0,
                "elapsed_sec": 2.0,
                "predicted_return_pct": 3.0,
                "actual_return_pct": 2.0,
                "pattern_types": ["double_bottom", "range_breakout"],
            },
            {
                "model": "small",
                "status": "ok",
                "actual_status": "ok",
                "direction_correct": False,
                "absolute_return_error_pct": 2.0,
                "elapsed_sec": 4.0,
                "predicted_return_pct": -1.0,
                "actual_return_pct": 1.0,
                "pattern_types": ["double_bottom"],
            },
            {
                "model": "base",
                "symbol": "600519",
                "target_date": "2026-06-30",
                "status": "error",
                "reason": "download failed",
                "actual_status": "not_evaluated",
            },
        ]
    )

    assert summary["small"]["cases"] == 2
    assert summary["small"]["ok_cases"] == 2
    assert summary["small"]["evaluated_cases"] == 2
    assert summary["small"]["direction_accuracy"] == 0.5
    assert summary["small"]["mean_absolute_return_error_pct"] == 1.5
    assert summary["small"]["mean_elapsed_sec"] == 3.0
    assert summary["small"]["pattern_type_counts"] == {"double_bottom": 2, "range_breakout": 1}
    assert summary["small"]["pattern_mean_actual_return_pct"] == {"double_bottom": 1.5, "range_breakout": 2.0}
    assert summary["base"]["failures"] == [
        {
            "symbol": "600519",
            "target_date": "2026-06-30",
            "status": "error",
            "reason": "download failed",
        }
    ]


def test_load_actual_future_uses_direct_history_when_freshness_guard_misses(monkeypatch) -> None:
    module = _load_script_module()

    monkeypatch.setattr(module, "load_history_df", lambda *args, **kwargs: (None, "db_cache_miss"))
    monkeypatch.setattr(
        module,
        "load_direct_main_history_df",
        lambda *args, **kwargs: (
            pd.DataFrame(
                [
                    {"date": "2026-06-30", "close": 100.0},
                    {"date": "2026-07-01", "close": 102.0},
                    {"date": "2026-07-02", "close": 103.0},
                ]
            ),
            "main_stock_daily_direct",
        ),
    )
    monkeypatch.setattr(module, "_load_sequoia_history_df", lambda *args, **kwargs: None)

    actual = module.load_actual_future(
        "600519",
        target_date=date(2026, 6, 30),
        horizon=2,
        lookback=30,
        last_close=100.0,
        fallback_to_network=False,
    )

    assert actual["status"] == "ok"
    assert actual["source"] == "main_stock_daily_direct"
    assert actual["future_date"] == "2026-07-02"
    assert actual["actual_return_pct"] == 3.0
