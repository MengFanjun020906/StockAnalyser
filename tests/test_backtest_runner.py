# -*- coding: utf-8 -*-
"""End-to-end runner determinism, persistence and cancellation tests."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from src.backtest.entities import CorporateAction
from src.backtest.plan_adapter import PlanAdaptationResult
from src.backtest.runner import EngineIdentity, RunnerOptions, run_experiment
from src.backtest.schemas import (
    CorporateActionPolicy,
    PlanInputRecord,
    SourcePipeline,
)
from src.backtest.snapshot import SnapshotBuilder
from tests.backtest_helpers import (
    CALENDAR,
    config,
    daily_bars,
    plan,
    rules_for,
)


class FakeAdapter:
    def __init__(self, plans, inputs):
        self.plans = plans
        self.inputs = inputs

    def adapt(self):
        return PlanAdaptationResult(
            plans=self.plans,
            inputs=self.inputs,
            diagnostics={"fake": True},
            quality_flags=[],
        )


def _inputs(plan_obj):
    return [
        PlanInputRecord(
            input_id="input-1",
            source_id=plan_obj.plan_id,
            plan_id=plan_obj.plan_id,
            scenario_id=plan_obj.scenario_id,
            symbol=plan_obj.symbol,
            included=True,
            source_pipeline=SourcePipeline.SINGLE_STOCK,
        )
    ]


def _run(tmp_path: Path, run_id: str, *, cancel=False, plans=None):
    plan_obj = (plans or [plan()])[0]
    cfg = config(corporate_action_policy=CorporateActionPolicy.STRICT)
    cfg = cfg.model_copy(update={"corporate_action_version": "test.v1"})
    builder = SnapshotBuilder(
        run_id=run_id,
        config=cfg,
        plans=plans or [plan_obj],
        calendar=CALENDAR,
        daily_bars=daily_bars(),
        rules=rules_for(),
        actions=[
            CorporateAction(
                symbol="600519",
                ex_date=date(2026, 1, 6),
                action_type="cash_dividend",
                cash_per_share=Decimal("0"),
                version="test.v1",
                source="test",
                verified=True,
            )
        ],
        created_at=datetime(2026, 1, 2, 20, 0),
    )
    adapter = FakeAdapter(plans or [plan_obj], _inputs(plan_obj))
    return run_experiment(
        run_id=run_id,
        config=cfg,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CALENDAR,
            engine_identity=EngineIdentity("test", "abc", False),
            plan_adapter=adapter,
            snapshot_builder=builder,
            cancel_callback=(lambda: True) if cancel else None,
        ),
    )


def test_runner_persists_complete_ledger_and_checksums(tmp_path):
    result = _run(tmp_path, "bt-test-0001")
    assert result.status == "succeeded", (result.status, result.summary)
    run_dir = result.result_dir
    assert run_dir is not None
    for name in (
        "snapshot.sqlite",
        "manifest.json",
        "ledger.sqlite",
        "config.json",
        "inputs.json",
        "plans.json",
        "metrics.json",
        "diagnostics.json",
        "result.json",
        "checksums.json",
    ):
        assert (run_dir / name).exists(), name
    conn = sqlite3.connect(f"file:{run_dir / 'ledger.sqlite'}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM equity_points").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0] == 2
    finally:
        conn.close()


def test_runner_is_deterministic_for_same_snapshot_and_config(tmp_path):
    first = _run(tmp_path / "a", "bt-determinism-1")
    second = _run(tmp_path / "b", "bt-determinism-1")
    assert first.status == second.status
    with sqlite3.connect(f"file:{first.result_dir / 'ledger.sqlite'}?mode=ro", uri=True) as conn_a:
        rows_a = {
            table: conn_a.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("orders", "fills", "equity_points")
        }
    with sqlite3.connect(f"file:{second.result_dir / 'ledger.sqlite'}?mode=ro", uri=True) as conn_b:
        rows_b = {
            table: conn_b.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("orders", "fills", "equity_points")
        }
    assert rows_a == rows_b


def test_runner_marks_cancelled_without_success_metrics(tmp_path):
    result = _run(tmp_path, "bt-cancel-1", cancel=True)
    assert result.status == "cancelled"
    assert result.metrics is None
    assert (result.result_dir / "ledger.sqlite").exists()
    assert result.summary["status"] == "cancelled"


def test_runner_fails_cleanly_when_no_plans(tmp_path):
    cfg = config()
    adapter = FakeAdapter([], [])
    result = run_experiment(
        run_id="bt-empty-1",
        config=cfg,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CALENDAR,
            engine_identity=EngineIdentity("test", "abc", False),
            plan_adapter=adapter,
        ),
    )
    assert result.status == "failed"
    assert result.error_code == "no_included_plans"
    assert result.result_dir is None
