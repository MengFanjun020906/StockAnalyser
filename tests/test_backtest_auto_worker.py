# -*- coding: utf-8 -*-
"""In-process auto-dispatch lifecycle tests."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from api.app import create_app
from src.backtest.auto_worker import AutoBacktestWorker


class FakeWorker:
    def __init__(self, kind: str) -> None:
        self.worker_kind = kind
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        self.started.set()
        self.stop_event.wait(2)

    def stop(self) -> None:
        self.stop_event.set()
        self.stopped.set()


def test_auto_worker_starts_and_stops_both_queues():
    created: list[FakeWorker] = []

    def factory(kind: str) -> FakeWorker:
        worker = FakeWorker(kind)
        created.append(worker)
        return worker

    service = SimpleNamespace(repo=object(), db=object(), research_llm_adapter=None)
    auto = AutoBacktestWorker(
        service=service,
        kinds=("standard", "research"),
        worker_factory=factory,
    )
    auto.start()
    try:
        assert auto.started is True
        assert {worker.worker_kind for worker in created} == {"standard", "research"}
        for worker in created:
            assert worker.started.wait(1)
    finally:
        auto.stop()
    assert auto.started is False
    for worker in created:
        assert worker.stopped.is_set()


def test_create_app_keeps_workers_off_by_default_and_opt_in():
    assert create_app(auto_worker=False).state.auto_backtest_worker_enabled is False
    opt_in = create_app(auto_worker=True)
    assert opt_in.state.auto_backtest_worker_enabled is True
