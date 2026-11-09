# -*- coding: utf-8 -*-
"""In-process auto-dispatch workers for Web-created backtest runs.

The API process owns the lifecycle of these threads.  They use the same
lease-based repository/worker path as the CLI workers, so running both is safe:
each run is claimed exactly once and the per-run snapshot/ledger remain isolated.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

from .service import BacktestRunService
from .worker import BacktestWorker

logger = logging.getLogger(__name__)


class AutoBacktestWorker:
    def __init__(
        self,
        *,
        service: BacktestRunService,
        kinds: Iterable[str] = ("standard", "research"),
        poll_seconds: float = 1.0,
        research_llm_adapter=None,
        worker_factory: Callable[..., BacktestWorker] | None = None,
    ) -> None:
        self.service = service
        self.kinds = tuple(kinds)
        self.poll_seconds = poll_seconds
        self.research_llm_adapter = research_llm_adapter
        self._worker_factory = worker_factory
        self._workers: list[BacktestWorker] = []
        self._threads: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for kind in self.kinds:
                worker = self._build_worker(kind)
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(worker,),
                    name=f"backtest-auto-{kind}",
                    daemon=True,
                )
                self._workers.append(worker)
                self._threads.append(thread)
                thread.start()
            self._started = True
            logger.info("auto backtest workers started: kinds=%s", ",".join(self.kinds))

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            for worker in self._workers:
                worker.stop()
            self._started = False
        for thread in self._threads:
            thread.join(timeout=5)
        self._workers.clear()
        self._threads.clear()
        logger.info("auto backtest workers stopped")

    def _build_worker(self, kind: str) -> BacktestWorker:
        if self._worker_factory is not None:
            return self._worker_factory(kind)
        return BacktestWorker(
            service=self.service,
            repository=self.service.repo,
            db_manager=self.service.db,
            worker_kind=kind,
            poll_seconds=self.poll_seconds,
            research_llm_adapter=self.research_llm_adapter,
        )

    def _run_worker(self, worker: BacktestWorker) -> None:
        try:
            worker.run_forever()
        except Exception:
            logger.exception("auto backtest worker crashed: kind=%s", worker.worker_kind)


__all__ = ["AutoBacktestWorker"]
