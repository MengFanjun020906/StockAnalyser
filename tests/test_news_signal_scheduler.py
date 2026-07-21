"""Tests for scheduled news-signal maintenance tasks."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.news_signal_scheduler import build_news_signal_background_tasks


def test_build_news_signal_background_tasks_registers_bounded_cls_incremental_poll() -> None:
    config = SimpleNamespace(
        news_signal_cls_incremental_enabled=True,
        news_signal_cls_incremental_interval_minutes=7,
        news_signal_cls_incremental_limit=40,
    )
    service = MagicMock()
    service.ingest_cls_incremental.return_value = {"status": "ok", "new_raw_episodes": 2}

    with patch("src.services.news_signal_scheduler.NewsSignalService", return_value=service):
        tasks = build_news_signal_background_tasks(config)
        tasks[0]["task"]()

    assert len(tasks) == 1
    assert tasks[0]["name"] == "news_signal_cls_incremental"
    assert tasks[0]["interval_seconds"] == 7 * 60
    assert tasks[0]["run_immediately"] is True
    service.ingest_cls_incremental.assert_called_once_with(limit=40)


def test_cls_incremental_interval_is_clamped_to_five_to_ten_minutes() -> None:
    fast = build_news_signal_background_tasks(SimpleNamespace(
        news_signal_cls_incremental_enabled=True,
        news_signal_cls_incremental_interval_minutes=1,
        news_signal_cls_incremental_limit=50,
    ))
    slow = build_news_signal_background_tasks(SimpleNamespace(
        news_signal_cls_incremental_enabled=True,
        news_signal_cls_incremental_interval_minutes=30,
        news_signal_cls_incremental_limit=50,
    ))

    assert fast[0]["interval_seconds"] == 5 * 60
    assert slow[0]["interval_seconds"] == 10 * 60


def test_build_news_signal_background_tasks_registers_graphiti_outbox_worker() -> None:
    config = SimpleNamespace(
        news_signal_cls_incremental_enabled=False,
        graphiti_enabled=True,
        graphiti_outbox_worker_enabled=True,
        graphiti_outbox_interval_seconds=75,
        graphiti_outbox_batch_size=12,
        graphiti_outbox_max_attempts=4,
        graphiti_outbox_retry_base_seconds=20,
        graphiti_outbox_job_timeout_seconds=90,
    )
    worker = MagicMock()
    worker.run_once.return_value = {"status": "ok", "claimed": 3, "succeeded": 3}

    with patch("src.services.news_signal_scheduler.GraphitiOutboxWorker", return_value=worker) as worker_cls:
        tasks = build_news_signal_background_tasks(config)
        tasks[0]["task"]()

    assert tasks[0]["name"] == "graphiti_outbox_worker"
    assert tasks[0]["interval_seconds"] == 75
    assert tasks[0]["run_immediately"] is True
    worker_cls.assert_called_once_with(
        max_attempts=4,
        retry_base_seconds=20,
        job_timeout_seconds=90,
    )
    worker.run_once.assert_called_once_with(limit=12)

def test_build_news_signal_background_tasks_registers_news_event_sentinel() -> None:
    config = SimpleNamespace(
        news_signal_cls_incremental_enabled=False,
        news_event_sentinel_enabled=True,
        news_event_sentinel_interval_minutes=3,
        news_event_sentinel_run_immediately=True,
        graphiti_enabled=False,
    )
    sentinel = MagicMock()
    sentinel.run_once.return_value = {
        "status": "ok",
        "triggered": 1,
        "suppressed_by_cooldown": 0,
        "cards_scanned": 2,
    }

    with patch("src.services.news_signal_scheduler.NewsEventSentinel", return_value=sentinel) as sentinel_cls:
        tasks = build_news_signal_background_tasks(config)
        tasks[0]["task"]()

    assert len(tasks) == 1
    assert tasks[0]["name"] == "news_event_sentinel"
    assert tasks[0]["interval_seconds"] == 5 * 60
    assert tasks[0]["run_immediately"] is True
    sentinel_cls.assert_called_once_with(config=config)
    sentinel.run_once.assert_called_once_with()
