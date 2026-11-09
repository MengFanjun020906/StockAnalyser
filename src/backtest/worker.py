# -*- coding: utf-8 -*-
"""Persistent multi-process backtest worker.

Run with::

    python -m src.backtest.worker --workers 4

Each process claims one run at a time from the central SQLite metadata table and
writes only its own per-run ledger/snapshot files.  The central database is used
for leases, progress and cancellation, not for hot-path fill writes.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from src.config import get_config
from src.repositories.backtest_run_repo import BacktestRunRepository
from src.storage import DatabaseManager

from .runner import (
    BacktestRunError,
    EngineIdentity,
    RunnerOptions,
    RunnerResult,
    get_engine_identity,
    run_experiment,
)
from .schemas import BacktestMetrics, ExperimentConfig, RunStatus, SnapshotManifest
from .service import BacktestRunService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    try:
        from src.logging_config import setup_logging

        setup_logging()
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )


class BacktestWorker:
    def __init__(
        self,
        *,
        worker_id: str | None = None,
        repository: BacktestRunRepository | None = None,
        service: BacktestRunService | None = None,
        db_manager: DatabaseManager | None = None,
        lease_seconds: int | None = None,
        poll_seconds: float | None = None,
        engine_identity: EngineIdentity | None = None,
        worker_kind: str = "standard",
        research_llm_adapter: Any | None = None,
        once: bool = False,
    ) -> None:
        self.config = get_config()
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = repository or BacktestRunRepository(self.db)
        self.service = service or BacktestRunService(
            db_manager=self.db,
            repository=self.repo,
        )
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_seconds = int(
            lease_seconds or getattr(self.config, "backtest_worker_lease_seconds", 180) or 180
        )
        self.poll_seconds = float(
            poll_seconds or getattr(self.config, "backtest_worker_poll_seconds", 1.0) or 1.0
        )
        self.engine_identity = engine_identity or get_engine_identity()
        self.worker_kind = worker_kind
        self.research_llm_adapter = research_llm_adapter
        self.once = once
        self._stop = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        self.repo.reap_stale()
        logger.info("backtest worker started: %s (lease=%ss)", self.worker_id, self.lease_seconds)
        last_reap = time.time()
        while not self._stop:
            if time.time() - last_reap >= 60:
                self.repo.reap_stale()
                last_reap = time.time()
            processed = self.run_once()
            if not processed:
                self._sleep(self.poll_seconds)
        logger.info("backtest worker stopped: %s", self.worker_id)

    def run_once(self) -> bool:
        row = self.repo.claim_next(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            worker_kind=self.worker_kind,
        )
        if row is None:
            return False
        self._execute(row)
        return True

    def _execute(self, row: dict[str, Any]) -> None:
        run_id = row["run_id"]
        lease_lost = {"value": False}
        state = {"progress": 0.02, "phase": "validating"}
        heartbeat_stop = threading.Event()

        def progress(value: float, phase: str) -> None:
            state["progress"] = value
            state["phase"] = phase
            ok = self.repo.heartbeat(
                run_id=run_id,
                worker_id=self.worker_id,
                progress=value,
                phase=phase,
                lease_seconds=self.lease_seconds,
            )
            if not ok:
                lease_lost["value"] = True
                logger.warning("backtest worker lost lease for %s", run_id)

        def heartbeat_loop() -> None:
            interval = max(10.0, self.lease_seconds / 3.0)
            while not heartbeat_stop.wait(interval):
                try:
                    ok = self.repo.heartbeat(
                        run_id=run_id,
                        worker_id=self.worker_id,
                        progress=float(state["progress"]),
                        phase=str(state["phase"]),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    logger.warning("backtest heartbeat failed for %s", run_id, exc_info=True)
                    continue
                if not ok:
                    lease_lost["value"] = True

        def cancel() -> bool:
            return lease_lost["value"] or self.repo.is_cancel_requested(run_id)

        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"backtest-heartbeat-{run_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            config = ExperimentConfig.model_validate_json(row["config_json"])
            if not self.repo.set_running(run_id=run_id, worker_id=self.worker_id):
                logger.warning("backtest worker could not start %s (lease lost)", run_id)
                return
            result = run_experiment(
                run_id=run_id,
                config=config,
                options=RunnerOptions(
                    runs_root=self.service.runs_root(),
                    calendar=self.service.calendar,
                    db_manager=self.db,
                    engine_identity=self.engine_identity,
                    reuse_lookup=self._reuse_lookup,
                    progress_callback=progress,
                    cancel_callback=cancel,
                    research_llm_adapter=self.research_llm_adapter,
                ),
            )
            self._finalize(result)
        except BacktestRunError as exc:
            logger.error("backtest run %s failed: [%s] %s", run_id, exc.code, exc.message)
            self.repo.finalize(
                run_id=run_id,
                status=RunStatus.FAILED.value,
                error_code=exc.code,
                error_message=exc.message,
                progress=0.0,
                phase="failed",
            )
        except Exception as exc:
            logger.exception("backtest run %s failed unexpectedly", run_id)
            self.repo.finalize(
                run_id=run_id,
                status=RunStatus.FAILED.value,
                error_code="internal_error",
                error_message=str(exc),
                progress=0.0,
                phase="failed",
            )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)

    def _finalize(self, result: RunnerResult) -> None:
        metrics_json = result.metrics.model_dump_json() if result.metrics else None
        manifest_json = result.manifest.model_dump_json() if result.manifest else None
        summary_json = json.dumps(result.summary, ensure_ascii=True, default=str)
        quality_flags = (
            result.summary.get("quality_flags")
            or (result.manifest.quality_flags if result.manifest else [])
            or []
        )
        counts = result.summary.get("counts") or {}
        data_quality = (
            result.manifest.quality.value
            if result.manifest is not None
            else "unavailable"
        )
        if result.summary.get("research"):
            data_quality = "partial"
        if result.status == RunStatus.FAILED.value:
            self.repo.save_inputs(result.run_id, result.inputs)
            self.repo.finalize(
                run_id=result.run_id,
                status=result.status,
                error_code=result.error_code or "no_included_plans",
                error_message=result.error_message or "run failed",
                progress=0.0,
                phase="failed",
            )
            return
        self.repo.finalize(
            run_id=result.run_id,
            status=result.status,
            experiment_fingerprint=result.experiment_fingerprint,
            manifest_json=manifest_json,
            metrics_json=metrics_json,
            summary_json=summary_json,
            quality_flags=quality_flags,
            counts=counts,
            result_dir=str(result.result_dir) if result.result_dir else None,
            data_quality=data_quality,
            reused_from_run_id=result.reused_from_run_id,
            progress=1.0,
            phase="completed",
        )
        self.repo.save_inputs(result.run_id, result.inputs)

    def _reuse_lookup(self, experiment_fingerprint: str) -> RunnerResult | None:
        if self.worker_kind != "standard":
            return None
        row = self.repo.find_succeeded_by_fingerprint(experiment_fingerprint)
        if row is None or not row.get("result_dir"):
            return None
        result_dir = Path(row["result_dir"])
        if not result_dir.exists():
            return None
        metrics = None
        manifest = None
        if row.get("metrics_json"):
            try:
                metrics = BacktestMetrics.model_validate(row["metrics_json"])
            except Exception:
                metrics = None
        if row.get("manifest_json"):
            try:
                manifest = SnapshotManifest.model_validate(row["manifest_json"])
            except Exception:
                manifest = None
        return RunnerResult(
            run_id=row["run_id"],
            status=row["status"],
            request_fingerprint=row.get("request_fingerprint") or "",
            experiment_fingerprint=row.get("experiment_fingerprint") or experiment_fingerprint,
            manifest=manifest,
            metrics=metrics,
            summary=row.get("summary_json") or {},
            diagnostics={},
            inputs=[],
            plans=[],
            result_dir=result_dir,
            engine_identity=self.engine_identity,
        )

    def _sleep(self, seconds: float) -> None:
        deadline = time.time() + max(0.05, seconds)
        while not self._stop and time.time() < deadline:
            time.sleep(0.1)


# ----------------------------------------------------------------------
# CLI / process supervision
# ----------------------------------------------------------------------
def _worker_main(worker_index: int, once: bool, poll_seconds: float | None, worker_kind: str = "standard") -> None:
    _configure_logging()
    worker = BacktestWorker(
        worker_id=f"{socket.gethostname()}:{os.getpid()}:{worker_index}:{uuid.uuid4().hex[:4]}",
        once=once,
        poll_seconds=poll_seconds,
        worker_kind=worker_kind,
    )
    signal.signal(signal.SIGTERM, lambda *_args: worker.stop())
    signal.signal(signal.SIGINT, lambda *_args: worker.stop())
    if once:
        worker.run_once()
    else:
        worker.run_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StockAnalyser portfolio backtest worker")
    parser.add_argument("--workers", type=int, default=None, help="number of worker processes")
    parser.add_argument("--poll", type=float, default=None, help="queue poll interval seconds")
    parser.add_argument("--once", action="store_true", help="process at most one run and exit")
    parser.add_argument("--worker-id", type=str, default=None, help="override worker id")
    parser.add_argument("--kind", type=str, default="standard", choices=["standard", "research"], help="queue kind")
    args = parser.parse_args(argv)
    _configure_logging()
    config = get_config()
    workers = int(args.workers or getattr(config, "backtest_worker_concurrency", 4) or 4)
    if args.once:
        worker = BacktestWorker(
            worker_id=args.worker_id,
            once=True,
            poll_seconds=args.poll,
            worker_kind=args.kind,
        )
        worker.run_once()
        return 0
    if workers <= 1:
        worker = BacktestWorker(
            worker_id=args.worker_id,
            once=args.once,
            poll_seconds=args.poll,
            worker_kind=args.kind,
        )
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
        return 0
    context = multiprocessing.get_context("spawn")
    processes: dict[int, multiprocessing.Process] = {}
    stopping = {"value": False}

    def _handle_signal(_signum, _frame) -> None:
        stopping["value"] = True
        for process in list(processes.values()):
            if process.is_alive():
                process.terminate()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        while not stopping["value"]:
            for index in range(workers):
                process = processes.get(index)
                if process is None or not process.is_alive():
                    child = context.Process(
                        target=_worker_main,
                        args=(index, False, args.poll, args.kind),
                        daemon=False,
                    )
                    child.start()
                    processes[index] = child
            time.sleep(1.0)
    finally:
        for process in processes.values():
            if process.is_alive():
                process.terminate()
        for process in processes.values():
            process.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
