# -*- coding: utf-8 -*-
"""Experiment runner: plan adaptation -> freeze -> execute -> metrics -> persist."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src.storage import DatabaseManager

from .calendar import CnTradingCalendar
from .data_view import InMemoryDataView
from .entities import ClosedTrade, EquityPoint, Fill, Order
from .execution import BacktestCancelled, ExecutionEngine, ExecutionResult
from .fees import get_fee_policy
from .metrics import compute_metrics
from .plan_adapter import PlanAdapter, PlanAdaptationResult
from .schemas import (
    BacktestMetrics,
    DataQuality,
    ENGINE_VERSION,
    ExperimentConfig,
    PlanInputRecord,
    ReplayPlan,
    RunStatus,
    SnapshotManifest,
    SourcePipeline,
    fingerprint_value,
)
from .snapshot import SnapshotBuilder, SnapshotBuildResult, write_snapshot

logger = logging.getLogger(__name__)


class BacktestRunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class EngineIdentity:
    engine_version: str = ENGINE_VERSION
    git_revision: str = "unknown"
    git_dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "git_revision": self.git_revision,
            "git_dirty": self.git_dirty,
        }


@dataclass
class RunnerResult:
    run_id: str
    status: str
    request_fingerprint: str
    experiment_fingerprint: str
    manifest: SnapshotManifest | None
    metrics: BacktestMetrics | None
    summary: dict[str, Any]
    diagnostics: dict[str, Any]
    inputs: list[PlanInputRecord]
    plans: list[ReplayPlan]
    result_dir: Path | None = None
    reused_from_run_id: str | None = None
    engine_identity: EngineIdentity = field(default_factory=EngineIdentity)
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class RunnerOptions:
    runs_root: Path
    calendar: CnTradingCalendar | None = None
    db_manager: DatabaseManager | None = None
    engine_identity: EngineIdentity | None = None
    reuse_lookup: Callable[[str], RunnerResult | None] | None = None
    progress_callback: Callable[[float, str], None] | None = None
    cancel_callback: Callable[[], bool] | None = None
    snapshot_builder: Any | None = None
    plan_adapter: Any | None = None
    write_snapshot_file: bool = True
    research_llm_adapter: Any | None = None
    research_universe: list[str] | None = None


def get_engine_identity() -> EngineIdentity:
    revision = "unknown"
    dirty = False
    try:
        root = Path(__file__).resolve().parents[2]
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
        )
    except Exception:
        pass
    return EngineIdentity(git_revision=revision, git_dirty=dirty)


def run_experiment(
    *,
    run_id: str,
    config: ExperimentConfig,
    options: RunnerOptions,
) -> RunnerResult:
    identity = options.engine_identity or get_engine_identity()
    calendar = options.calendar or CnTradingCalendar()
    if config.mode.value == "historical_agent_research":
        return _run_research_experiment(
            run_id=run_id,
            config=config,
            options=options,
            identity=identity,
            calendar=calendar,
        )
    adapter = options.plan_adapter or PlanAdapter(
        config=config,
        db_manager=options.db_manager,
        calendar=calendar,
    )
    adaptation: PlanAdaptationResult = adapter.adapt()
    request_fingerprint = build_request_fingerprint(
        config,
        adaptation.plans,
        identity=identity,
        calendar_version=calendar.version,
    )
    if not adaptation.plans:
        diagnostics = {
            "plan_adapter": adaptation.diagnostics,
            "exclusion_reasons": adaptation.diagnostics.get("exclusion_reasons", {}),
        }
        return RunnerResult(
            run_id=run_id,
            status=RunStatus.FAILED.value,
            request_fingerprint=request_fingerprint,
            experiment_fingerprint=request_fingerprint,
            manifest=None,
            metrics=None,
            summary={"included_plans": 0, "excluded_plans": adaptation.excluded_count},
            diagnostics=diagnostics,
            inputs=adaptation.inputs,
            plans=[],
            engine_identity=identity,
            error_code="no_included_plans",
            error_message="no machine-executable plans after input validation",
        )
    _progress(options, 0.02, "snapshot")
    builder = options.snapshot_builder or SnapshotBuilder(
        run_id=run_id,
        config=config,
        plans=adaptation.plans,
        calendar=calendar,
        db_manager=options.db_manager,
    )
    try:
        snapshot: SnapshotBuildResult = builder.build()
    except Exception as exc:
        logger.exception("backtest snapshot build failed: %s", exc)
        raise BacktestRunError("snapshot_failed", str(exc)) from exc
    experiment_fingerprint = _experiment_fingerprint(
        request_fingerprint,
        manifest=snapshot.manifest,
        adaptation=adaptation,
    )
    if options.reuse_lookup:
        reused = options.reuse_lookup(experiment_fingerprint)
        if reused is not None:
            reused.reused_from_run_id = reused.run_id
            reused.run_id = run_id
            reused.request_fingerprint = request_fingerprint
            reused.experiment_fingerprint = experiment_fingerprint
            reused.inputs = adaptation.inputs
            reused.plans = adaptation.plans
            return reused
    run_dir = options.runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if options.write_snapshot_file:
        try:
            manifest = write_snapshot(snapshot, run_dir)
        except Exception as exc:
            logger.exception("backtest snapshot write failed: %s", exc)
            raise BacktestRunError("snapshot_persist_failed", str(exc)) from exc
    else:
        manifest = snapshot.manifest
    _progress(options, 0.05, "running")
    fee_policy = get_fee_policy(config.fee_policy_ref)
    engine = ExecutionEngine(
        run_id=run_id,
        config=config,
        plans=adaptation.plans,
        data=snapshot.data,
        calendar=calendar,
        fee_policy=fee_policy,
        progress_callback=lambda processed, total, phase: _progress(
            options, 0.05 + 0.85 * (processed / max(1, total)), phase
        ),
        cancel_callback=options.cancel_callback,
    )
    try:
        execution: ExecutionResult = engine.run()
    except BacktestCancelled:
        execution = engine.cancel()
    except Exception as exc:
        logger.exception("backtest execution failed: %s", exc)
        raise BacktestRunError("execution_failed", str(exc)) from exc

    if execution.status == "cancelled":
        _progress(options, 0.95, "persisting")
        summary = _write_run_artifacts(
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adaptation=adaptation,
            execution=execution,
            manifest=manifest,
            metrics=None,
            identity=identity,
            request_fingerprint=request_fingerprint,
            experiment_fingerprint=experiment_fingerprint,
            status=RunStatus.CANCELLED.value,
        )
        return RunnerResult(
            run_id=run_id,
            status=RunStatus.CANCELLED.value,
            request_fingerprint=request_fingerprint,
            experiment_fingerprint=experiment_fingerprint,
            manifest=manifest,
            metrics=None,
            summary=summary,
            diagnostics=execution.diagnostics,
            inputs=adaptation.inputs,
            plans=adaptation.plans,
            result_dir=run_dir,
            engine_identity=identity,
        )

    _apply_mfe_mae(execution.ledger.closed_trades, snapshot.data)
    _progress(options, 0.92, "metrics")
    try:
        metrics = compute_metrics(
            equity_points=execution.ledger.equity_points,
            initial_cash=config.initial_cash,
            fills=execution.fills,
            orders=execution.orders,
            closed_trades=execution.ledger.closed_trades,
            plans_count=len(adaptation.plans),
            diagnostics=execution.diagnostics,
            benchmark=snapshot.benchmark or None,
            annualization_days=config.annualization_days,
            risk_free_rate=config.risk_free_rate,
            fees_paid=execution.ledger.fees_paid,
            slippage_paid=execution.ledger.slippage_paid,
            quality_flags=execution.quality_flags,
            source_pipeline=config.source_pipeline,
            risk_profile=config.risk_profile,
            benchmark_status=manifest.benchmark_status,
        )
    except Exception as exc:
        logger.exception("backtest metrics failed: %s", exc)
        raise BacktestRunError("metrics_failed", str(exc)) from exc
    status = _final_status(execution, metrics, adaptation)
    _progress(options, 0.96, "persisting")
    summary = _write_run_artifacts(
        run_dir=run_dir,
        run_id=run_id,
        config=config,
        adaptation=adaptation,
        execution=execution,
        manifest=manifest,
        metrics=metrics,
        identity=identity,
        request_fingerprint=request_fingerprint,
        experiment_fingerprint=experiment_fingerprint,
        status=status,
    )
    _progress(options, 1.0, "completed")
    return RunnerResult(
        run_id=run_id,
        status=status,
        request_fingerprint=request_fingerprint,
        experiment_fingerprint=experiment_fingerprint,
        manifest=manifest,
        metrics=metrics,
        summary=summary,
        diagnostics=execution.diagnostics,
        inputs=adaptation.inputs,
        plans=adaptation.plans,
        result_dir=run_dir,
        engine_identity=identity,
    )


def _run_research_experiment(
    *,
    run_id: str,
    config: ExperimentConfig,
    options: RunnerOptions,
    identity: EngineIdentity,
    calendar: CnTradingCalendar,
) -> RunnerResult:
    from .research.dataset import build_research_dataset, resolve_research_universe
    from .research.orchestrator import ResearchOrchestrator
    from .research.universe import ResearchUniverseError

    research = config.research
    if research is None:
        raise BacktestRunError("research_config_missing", "research config is required")
    try:
        universe, universe_source = resolve_research_universe(
            config,
            db_manager=options.db_manager,
            override=options.research_universe,
        )
    except ResearchUniverseError as exc:
        raise BacktestRunError("research_universe_failed", str(exc)) from exc
    if not universe:
        raise BacktestRunError("research_universe_empty", "no symbols resolved for research universe")
    request_fingerprint = build_request_fingerprint(
        config,
        [],
        identity=identity,
        calendar_version=calendar.version,
    )
    _progress(options, 0.02, "research_snapshot")
    try:
        if options.snapshot_builder is not None:
            snapshot = options.snapshot_builder.build()
            dataset_warnings: list[str] = []
            dataset_source = "injected"
        else:
            dataset = build_research_dataset(
                config,
                calendar=calendar,
                universe=universe,
                db_manager=options.db_manager,
            )
            snapshot = dataset.snapshot
            dataset_warnings = dataset.warnings
            dataset_source = dataset.data_source
    except Exception as exc:
        logger.exception("research snapshot build failed: %s", exc)
        raise BacktestRunError("research_snapshot_failed", str(exc)) from exc
    experiment_fingerprint = fingerprint_value(
        {
            "request": request_fingerprint,
            "data_content_hash": snapshot.manifest.content_hash,
            "calendar_version": snapshot.manifest.calendar_version,
            "rules_version": snapshot.manifest.rules_version,
            "corporate_action_version": snapshot.manifest.corporate_action_version,
            "universe": universe,
            "universe_source": universe_source,
            "data_source": dataset_source,
            "prompt_version": research.prompt_version,
            "plan_version": research.plan_version,
            "max_llm_calls": research.max_llm_calls,
        }
    )
    run_dir = options.runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = write_snapshot(snapshot, run_dir) if options.write_snapshot_file else snapshot.manifest
    fee_policy = get_fee_policy(config.fee_policy_ref)
    orchestrator = ResearchOrchestrator(
        config=config,
        data=snapshot.data,
        calendar=calendar,
        cache_path=run_dir / "research_cache.sqlite",
        llm_adapter=options.research_llm_adapter,
        universe=universe,
        universe_source=universe_source,
        progress_callback=lambda _event, _payload: _progress(options, 0.08, "research_decision"),
    )
    _progress(options, 0.05, "research_running")
    engine = ExecutionEngine(
        run_id=run_id,
        config=config,
        plans=[],
        data=snapshot.data,
        calendar=calendar,
        fee_policy=fee_policy,
        progress_callback=lambda processed, total, phase: _progress(
            options, 0.05 + 0.80 * (processed / max(1, total)), "research_running"
        ),
        cancel_callback=options.cancel_callback,
        plan_provider=orchestrator.plan_for_decision,
        decision_dates=orchestrator.decision_dates,
    )
    try:
        try:
            execution = engine.run()
        except BacktestCancelled:
            execution = engine.cancel()
        except Exception as exc:
            logger.exception("research execution failed: %s", exc)
            raise BacktestRunError("research_execution_failed", str(exc)) from exc
        research_diagnostics = orchestrator.diagnostics()
        research_diagnostics["data_warnings"] = dataset_warnings
        research_diagnostics["data_source"] = dataset_source
        research_diagnostics["universe_source"] = universe_source
        _progress(options, 0.90, "research_metrics")
        metrics = compute_metrics(
            equity_points=execution.ledger.equity_points,
            initial_cash=config.initial_cash,
            fills=execution.fills,
            orders=execution.orders,
            closed_trades=execution.ledger.closed_trades,
            plans_count=len(engine.plans),
            diagnostics={
                **execution.diagnostics,
                "research_llm_calls": research_diagnostics.get("llm_calls", 0),
                "research_cache_hits": research_diagnostics.get("cache_hits", 0),
            },
            benchmark=snapshot.benchmark or None,
            annualization_days=config.annualization_days,
            risk_free_rate=config.risk_free_rate,
            fees_paid=execution.ledger.fees_paid,
            slippage_paid=execution.ledger.slippage_paid,
            quality_flags=execution.quality_flags
            + list(research_diagnostics.get("labels", []))
            + ([] if not research_diagnostics.get("skipped") else ["research_skips"]),
            source_pipeline=SourcePipeline.RESEARCH,
            risk_profile=config.risk_profile,
            benchmark_status=manifest.benchmark_status,
        )
        status = RunStatus.CANCELLED.value if execution.status == "cancelled" else RunStatus.PARTIAL.value
        adaptation = PlanAdaptationResult(
            plans=list(engine.plans),
            inputs=[],
            diagnostics=research_diagnostics,
            quality_flags=list(research_diagnostics.get("labels", [])),
        )
        _progress(options, 0.96, "research_persisting")
        summary = _write_run_artifacts(
            run_dir=run_dir,
            run_id=run_id,
            config=config,
            adaptation=adaptation,
            execution=execution,
            manifest=manifest,
            metrics=metrics,
            identity=identity,
            request_fingerprint=request_fingerprint,
            experiment_fingerprint=experiment_fingerprint,
            status=status,
        )
        return RunnerResult(
            run_id=run_id,
            status=status,
            request_fingerprint=request_fingerprint,
            experiment_fingerprint=experiment_fingerprint,
            manifest=manifest,
            metrics=metrics,
            summary=summary,
            diagnostics={**execution.diagnostics, "research": research_diagnostics},
            inputs=[],
            plans=list(engine.plans),
            result_dir=run_dir,
            engine_identity=identity,
        )
    finally:
        orchestrator.close()


def _progress(options: RunnerOptions, value: float, phase: str) -> None:
    if options.progress_callback:
        try:
            options.progress_callback(max(0.0, min(1.0, value)), phase)
        except Exception:
            logger.warning("backtest progress callback failed", exc_info=True)


def build_request_fingerprint(
    config: ExperimentConfig,
    plans: Sequence[ReplayPlan],
    *,
    identity: EngineIdentity,
    calendar_version: str,
) -> str:
    payload = {
        "config": config.normalized(),
        "engine": identity.to_dict(),
        "calendar_version": calendar_version,
        "fee_policy": config.fee_policy_ref,
        "risk_policy_version": config.risk_policy.policy_version if config.risk_policy else "",
        "plan_hashes": sorted(plan.fingerprint() for plan in plans),
        "source_pipeline": config.source_pipeline.value,
    }
    return fingerprint_value(payload)


def _experiment_fingerprint(
    request_fingerprint: str,
    *,
    manifest: SnapshotManifest,
    adaptation: PlanAdaptationResult,
) -> str:
    return fingerprint_value(
        {
            "request": request_fingerprint,
            "data_content_hash": manifest.content_hash,
            "calendar_version": manifest.calendar_version,
            "rules_version": manifest.rules_version,
            "corporate_action_version": manifest.corporate_action_version,
            "corporate_action_count": manifest.corporate_action_count,
            "plan_inputs": sorted(
                item.payload_hash or item.input_id
                for item in adaptation.inputs
                if item.included
            ),
        }
    )


def _final_status(
    execution: ExecutionResult,
    metrics: BacktestMetrics,
    adaptation: PlanAdaptationResult,
) -> str:
    if execution.diagnostics.get("ambiguous_fills"):
        return RunStatus.PARTIAL.value
    if metrics.quality is DataQuality.PARTIAL:
        return RunStatus.PARTIAL.value
    if metrics.quality is DataQuality.UNAVAILABLE:
        return RunStatus.PARTIAL.value
    if execution.status == "partial":
        return RunStatus.PARTIAL.value
    if adaptation.excluded_count and adaptation.included_count:
        return RunStatus.PARTIAL.value
    return RunStatus.SUCCEEDED.value


def _apply_mfe_mae(trades: list[ClosedTrade], data: InMemoryDataView) -> None:
    for trade in trades:
        bars = data.daily_bars(trade.symbol, trade.exit_session)
        window = [bar for bar in bars if trade.entry_session <= bar.session <= trade.exit_session]
        if not window:
            continue
        entry = trade.entry_price
        if entry <= 0:
            continue
        trade.mfe_pct = float((max(bar.high for bar in window) / entry - Decimal("1")) * Decimal("100"))
        trade.mae_pct = float((min(bar.low for bar in window) / entry - Decimal("1")) * Decimal("100"))


# ----------------------------------------------------------------------
# Per-run persistence
# ----------------------------------------------------------------------
def _write_run_artifacts(
    *,
    run_dir: Path,
    run_id: str,
    config: ExperimentConfig,
    adaptation: PlanAdaptationResult,
    execution: ExecutionResult,
    manifest: SnapshotManifest,
    metrics: BacktestMetrics | None,
    identity: EngineIdentity,
    request_fingerprint: str,
    experiment_fingerprint: str,
    status: str,
) -> dict[str, Any]:
    ledger_path = run_dir / "ledger.sqlite"
    tmp_path = run_dir / "ledger.sqlite.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    try:
        _create_ledger_schema(conn)
        _insert_orders(conn, execution.orders)
        _insert_fills(conn, execution.fills)
        _insert_ledger_events(conn, execution.ledger.events)
        _insert_lots(conn, execution.ledger.lots)
        _insert_equity_points(conn, execution.ledger.equity_points)
        _insert_closed_trades(conn, execution.ledger.closed_trades)
        _insert_plans(conn, adaptation.plans)
        _insert_inputs(conn, adaptation.inputs)
        _insert_diagnostics(conn, execution.diagnostics, adaptation.diagnostics)
        meta = {
            "run_id": run_id,
            "status": status,
            "config": config.model_dump(mode="json"),
            "engine": identity.to_dict(),
            "request_fingerprint": request_fingerprint,
            "experiment_fingerprint": experiment_fingerprint,
            "manifest": manifest.model_dump(mode="json"),
            "metrics": metrics.model_dump(mode="json") if metrics else None,
            "source_pipeline": config.source_pipeline.value,
            "risk_profile": config.risk_profile.value,
        }
        for key, value in meta.items():
            conn.execute(
                "INSERT INTO meta(key,value) VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=True, sort_keys=True)),
            )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, ledger_path)
    try:
        ledger_path.chmod(0o444)
    except OSError:
        pass
    summary = _build_summary(
        run_id=run_id,
        config=config,
        adaptation=adaptation,
        execution=execution,
        metrics=metrics,
        status=status,
        manifest=manifest,
        experiment_fingerprint=experiment_fingerprint,
    )
    metrics_payload = metrics.model_dump(mode="json") if metrics else None
    _write_json(run_dir / "config.json", config.model_dump(mode="json"))
    _write_json(
        run_dir / "inputs.json",
        [item.model_dump(mode="json") for item in adaptation.inputs],
    )
    _write_json(
        run_dir / "plans.json",
        [item.model_dump(mode="json") for item in adaptation.plans],
    )
    _write_json(run_dir / "metrics.json", metrics_payload)
    _write_json(run_dir / "diagnostics.json", execution.diagnostics)
    _write_json(run_dir / "result.json", summary)
    checksums = {
        "ledger.sqlite": _sha256(ledger_path),
        "metrics.json": _sha256(run_dir / "metrics.json"),
        "result.json": _sha256(run_dir / "result.json"),
    }
    snapshot_path = run_dir / "snapshot.sqlite"
    if snapshot_path.exists():
        checksums["snapshot.sqlite"] = _sha256(snapshot_path)
    _write_json(run_dir / "checksums.json", checksums)
    try:
        for name in ("metrics.json", "result.json", "checksums.json"):
            (run_dir / name).chmod(0o444)
    except OSError:
        pass
    return summary


def _build_summary(
    *,
    run_id: str,
    config: ExperimentConfig,
    adaptation: PlanAdaptationResult,
    execution: ExecutionResult,
    metrics: BacktestMetrics | None,
    status: str,
    manifest: SnapshotManifest,
    experiment_fingerprint: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": status,
        "mode": config.mode.value,
        "source_pipeline": config.source_pipeline.value,
        "risk_profile": config.risk_profile.value,
        "experiment_fingerprint": experiment_fingerprint,
        "included_plans": adaptation.included_count,
        "excluded_plans": adaptation.excluded_count,
        "fills": len(execution.fills),
        "orders": len(execution.orders),
        "closed_trades": len(execution.ledger.closed_trades),
        "final_equity": str(execution.ledger.total_equity),
        "cash": str(execution.ledger.cash),
        "realized_pnl": str(execution.ledger.realized_pnl),
        "fees_paid": str(execution.ledger.fees_paid),
        "slippage_paid": str(execution.ledger.slippage_paid),
        "data_quality": manifest.quality.value,
        "quality_flags": sorted(set(manifest.quality_flags) | set(adaptation.quality_flags)),
        "research": adaptation.diagnostics if config.mode.value == "historical_agent_research" else {},
        "coverage": [row.model_dump(mode="json") for row in manifest.coverage],
        "metrics": metrics.model_dump(mode="json") if metrics else None,
        "counts": {
            "orders": len(execution.orders),
            "fills": len(execution.fills),
            "closed_trades": len(execution.ledger.closed_trades),
            "equity_points": len(execution.ledger.equity_points),
            "included_plans": adaptation.included_count,
            "excluded_plans": adaptation.excluded_count,
            "open_positions": execution.diagnostics.get("open_positions", 0),
        },
    }


def _create_ledger_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE orders(
            order_id TEXT PRIMARY KEY, plan_key TEXT, symbol TEXT, side TEXT, order_type TEXT,
            quantity INTEGER, remaining INTEGER, filled_quantity INTEGER,
            avg_fill_price TEXT, limit_price TEXT, stop_price TEXT, entry_floor TEXT,
            submitted_at TEXT, active_from TEXT, expires_on_session TEXT, status TEXT,
            reserved_cash TEXT, reserved_quantity INTEGER, priority_rank INTEGER,
            reason TEXT, tags_json TEXT, pre_registered_exit INTEGER, exit_reason TEXT
        );
        CREATE INDEX ix_ledger_orders_symbol ON orders(symbol, status);
        CREATE TABLE fills(
            fill_id TEXT PRIMARY KEY, order_id TEXT, symbol TEXT, side TEXT, quantity INTEGER,
            price TEXT, gross_amount TEXT, commission TEXT, transfer_fee TEXT, stamp_duty TEXT,
            slippage_cost TEXT, bar_end_at TEXT, session TEXT, quality TEXT, reason TEXT,
            event_seq INTEGER
        );
        CREATE INDEX ix_ledger_fills_symbol_session ON fills(symbol, session);
        CREATE TABLE ledger_events(
            sequence INTEGER PRIMARY KEY, event_time TEXT, event_type TEXT, symbol TEXT,
            cash_delta TEXT, reserved_delta TEXT, quantity_delta INTEGER, cost_delta TEXT,
            realized_pnl_delta TEXT, fees_delta TEXT, order_id TEXT, fill_id TEXT,
            reason TEXT, payload_json TEXT, idempotency_key TEXT UNIQUE
        );
        CREATE INDEX ix_ledger_events_symbol_time ON ledger_events(symbol, event_time);
        CREATE TABLE position_lots(
            lot_id TEXT PRIMARY KEY, symbol TEXT, acquired_session TEXT, available_session TEXT,
            quantity INTEGER, remaining INTEGER, cost_total TEXT, cost_per_share TEXT,
            source_order_id TEXT, source_fill_id TEXT
        );
        CREATE TABLE equity_points(
            session TEXT PRIMARY KEY, timestamp TEXT, cash TEXT, reserved_cash TEXT,
            available_cash TEXT, market_value TEXT, equity TEXT, peak_equity TEXT,
            drawdown TEXT, stale INTEGER, benchmark_value TEXT, granularity TEXT, quality TEXT
        );
        CREATE TABLE closed_trades(
            trade_id TEXT PRIMARY KEY, symbol TEXT, entry_order_id TEXT, exit_order_id TEXT,
            quantity INTEGER, entry_price TEXT, exit_price TEXT, cost_basis TEXT,
            net_proceeds TEXT, realized_pnl TEXT, return_pct REAL,
            entry_session TEXT, exit_session TEXT, holding_days INTEGER,
            exit_reason TEXT, mfe_pct REAL, mae_pct REAL
        );
        CREATE TABLE plans(plan_key TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
        CREATE TABLE inputs(
            input_id TEXT PRIMARY KEY, plan_id TEXT, scenario_id TEXT, symbol TEXT,
            included INTEGER, exclusion_reason TEXT, source_pipeline TEXT,
            selection_trace_ref TEXT, confirmation_trace_ref TEXT, link_quality TEXT,
            record_json TEXT NOT NULL
        );
        CREATE INDEX ix_ledger_inputs_included ON inputs(included, exclusion_reason);
        CREATE TABLE diagnostics(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        """
    )


def _insert_orders(conn: sqlite3.Connection, orders: Iterable[Order]) -> None:
    rows = []
    for order in orders:
        d = order.to_dict()
        rows.append(
            (
                d["order_id"], d["plan_key"], d["symbol"], d["side"], d["order_type"],
                d["quantity"], d["remaining"], d["filled_quantity"], d["avg_fill_price"],
                d["limit_price"], d["stop_price"], d["entry_floor"], d["submitted_at"],
                d["active_from"], d["expires_on_session"], d["status"], d["reserved_cash"],
                d["reserved_quantity"], d["priority_rank"], d["reason"],
                json.dumps(d["tags"], ensure_ascii=True), int(d["pre_registered_exit"]),
                d["exit_reason"],
            )
        )
    conn.executemany(
        "INSERT INTO orders(order_id,plan_key,symbol,side,order_type,quantity,remaining,"
        "filled_quantity,avg_fill_price,limit_price,stop_price,entry_floor,submitted_at,"
        "active_from,expires_on_session,status,reserved_cash,reserved_quantity,priority_rank,"
        "reason,tags_json,pre_registered_exit,exit_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_fills(conn: sqlite3.Connection, fills: Iterable[Fill]) -> None:
    rows = []
    for fill in fills:
        d = fill.to_dict()
        rows.append(
            (
                d["fill_id"], d["order_id"], d["symbol"], d["side"], d["quantity"],
                d["price"], d["gross_amount"], d["commission"], d["transfer_fee"],
                d["stamp_duty"], d["slippage_cost"], d["bar_end_at"], d["session"],
                d["quality"], d["reason"], d["event_seq"],
            )
        )
    conn.executemany(
        "INSERT INTO fills(fill_id,order_id,symbol,side,quantity,price,gross_amount,commission,"
        "transfer_fee,stamp_duty,slippage_cost,bar_end_at,session,quality,reason,event_seq) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_ledger_events(conn: sqlite3.Connection, events: Iterable[Any]) -> None:
    rows = []
    for event in events:
        d = event.to_dict()
        rows.append(
            (
                d["sequence"], d["event_time"], d["event_type"], d["symbol"],
                d["cash_delta"], d["reserved_delta"], d["quantity_delta"], d["cost_delta"],
                d["realized_pnl_delta"], d["fees_delta"], d["order_id"], d["fill_id"],
                d["reason"], json.dumps(d["payload"], ensure_ascii=True, sort_keys=True),
                d["idempotency_key"],
            )
        )
    conn.executemany(
        "INSERT INTO ledger_events(sequence,event_time,event_type,symbol,cash_delta,reserved_delta,"
        "quantity_delta,cost_delta,realized_pnl_delta,fees_delta,order_id,fill_id,reason,payload_json,"
        "idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_lots(conn: sqlite3.Connection, lots_by_symbol: dict[str, list[Any]]) -> None:
    rows = []
    for symbol in sorted(lots_by_symbol):
        for lot in lots_by_symbol[symbol]:
            d = lot.to_dict()
            rows.append(
                (
                    d["lot_id"], d["symbol"], d["acquired_session"], d["available_session"],
                    d["quantity"], d["remaining"], d["cost_total"], d["cost_per_share"],
                    d["source_order_id"], d["source_fill_id"],
                )
            )
    conn.executemany(
        "INSERT INTO position_lots(lot_id,symbol,acquired_session,available_session,quantity,"
        "remaining,cost_total,cost_per_share,source_order_id,source_fill_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_equity_points(conn: sqlite3.Connection, points: Iterable[EquityPoint]) -> None:
    rows = []
    for point in points:
        d = point.to_dict()
        rows.append(
            (
                d["session"], d["timestamp"], d["cash"], d["reserved_cash"],
                d["available_cash"], d["market_value"], d["equity"], d["peak_equity"],
                d["drawdown"], int(d["stale"]), d["benchmark_value"], d["granularity"],
                d["quality"],
            )
        )
    conn.executemany(
        "INSERT INTO equity_points(session,timestamp,cash,reserved_cash,available_cash,"
        "market_value,equity,peak_equity,drawdown,stale,benchmark_value,granularity,quality) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_closed_trades(conn: sqlite3.Connection, trades: Iterable[ClosedTrade]) -> None:
    rows = []
    for trade in trades:
        d = trade.to_dict()
        rows.append(
            (
                d["trade_id"], d["symbol"], d["entry_order_id"], d["exit_order_id"],
                d["quantity"], d["entry_price"], d["exit_price"], d["cost_basis"],
                d["net_proceeds"], d["realized_pnl"], d["return_pct"], d["entry_session"],
                d["exit_session"], d["holding_days"], d["exit_reason"], d["mfe_pct"],
                d["mae_pct"],
            )
        )
    conn.executemany(
        "INSERT INTO closed_trades(trade_id,symbol,entry_order_id,exit_order_id,quantity,"
        "entry_price,exit_price,cost_basis,net_proceeds,realized_pnl,return_pct,entry_session,"
        "exit_session,holding_days,exit_reason,mfe_pct,mae_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_plans(conn: sqlite3.Connection, plans: Iterable[ReplayPlan]) -> None:
    conn.executemany(
        "INSERT INTO plans(plan_key,plan_json) VALUES (?,?)",
        [(plan.plan_key, plan.model_dump_json()) for plan in plans],
    )


def _insert_inputs(conn: sqlite3.Connection, inputs: Iterable[PlanInputRecord]) -> None:
    rows = []
    for item in inputs:
        rows.append(
            (
                item.input_id, item.plan_id, item.scenario_id, item.symbol,
                int(item.included), item.exclusion_reason, item.source_pipeline.value
                if item.source_pipeline
                else None,
                item.selection_trace_ref, item.confirmation_trace_ref, item.link_quality,
                item.model_dump_json(),
            )
        )
    conn.executemany(
        "INSERT INTO inputs(input_id,plan_id,scenario_id,symbol,included,exclusion_reason,"
        "source_pipeline,selection_trace_ref,confirmation_trace_ref,link_quality,record_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _insert_diagnostics(
    conn: sqlite3.Connection,
    execution: dict[str, Any],
    adaptation: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO diagnostics(key,value_json) VALUES (?,?)",
        ("execution", json.dumps(execution, ensure_ascii=True, sort_keys=True, default=str)),
    )
    conn.execute(
        "INSERT INTO diagnostics(key,value_json) VALUES (?,?)",
        ("plan_adapter", json.dumps(adaptation, ensure_ascii=True, sort_keys=True, default=str)),
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


__all__ = [
    "BacktestRunError",
    "build_request_fingerprint",
    "EngineIdentity",
    "RunnerOptions",
    "RunnerResult",
    "get_engine_identity",
    "run_experiment",
]
