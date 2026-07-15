"""Behavior tests for the relational Graphiti outbox worker."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.config import Config
from src.repositories.graphiti_outbox_repo import GraphitiOutboxRepository
from src.repositories.news_signal_repo import NewsSignalRepository
from src.services.graphiti.outbox_worker import GraphitiOutboxWorker
from src.storage import DatabaseManager


def _setup_db(tmp_path: Path) -> DatabaseManager:
    env_path = tmp_path / ".env"
    db_path = tmp_path / "outbox.db"
    env_path.write_text(f"DATABASE_PATH={db_path}\n", encoding="utf-8")
    os.environ["ENV_FILE"] = str(env_path)
    os.environ["DATABASE_PATH"] = str(db_path)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    return DatabaseManager.get_instance()


def _cleanup_db() -> None:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    os.environ.pop("ENV_FILE", None)
    os.environ.pop("DATABASE_PATH", None)


def _card(card_id: str = "card:outbox:1") -> dict:
    return {
        "card_id": card_id,
        "signal_date": date(2026, 7, 11),
        "summary_short": "Graphiti outbox 测试卡片",
        "status": "active",
        "signal_score": 80.0,
        "graph_sync_status": "pending",
        "raw_episode_ids": [],
    }


def test_outbox_worker_ingests_card_episode_and_marks_job_succeeded() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            news_repo = NewsSignalRepository(db)
            saved = news_repo.upsert_cards([_card()])[0]
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_episode",
                aggregate_id=saved["card_id"],
                payload={"card_id": saved["card_id"]},
            )
            graphiti = MagicMock()
            graphiti.is_available.return_value = True
            graphiti.ingest_news_signal_card_sync.return_value = {"status": "synced"}

            result = GraphitiOutboxWorker(
                outbox_repo=outbox,
                news_repo=news_repo,
                graphiti=graphiti,
            ).run_once(limit=10)

            assert result["succeeded"] == 1
            assert result["failed"] == 0
            assert outbox.metrics()["status_counts"]["succeeded"] == 1
            assert news_repo.get_card(saved["card_id"])["graph_sync_status"] == "synced"
        finally:
            _cleanup_db()


def test_outbox_worker_requeues_failed_job_with_backoff() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            news_repo = NewsSignalRepository(db)
            saved = news_repo.upsert_cards([_card()])[0]
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_episode",
                aggregate_id=saved["card_id"],
                payload={"card_id": saved["card_id"]},
            )
            graphiti = MagicMock()
            graphiti.is_available.return_value = True
            graphiti.ingest_news_signal_card_sync.return_value = {
                "status": "failed",
                "error": "provider timeout",
            }
            worker = GraphitiOutboxWorker(
                outbox_repo=outbox,
                news_repo=news_repo,
                graphiti=graphiti,
                retry_base_seconds=60,
            )

            first = worker.run_once(limit=10)
            second = worker.run_once(limit=10)

            assert first["failed"] == 1
            assert second["claimed"] == 0
            assert outbox.metrics()["status_counts"]["retry"] == 1
            card = news_repo.get_card(saved["card_id"])
            assert card["graph_sync_status"] == "failed"
            assert card["graph_retry_count"] == 1
        finally:
            _cleanup_db()


def test_outbox_worker_reports_timeout_type_and_retries() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            news_repo = NewsSignalRepository(db)
            saved = news_repo.upsert_cards([_card()])[0]
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_episode",
                aggregate_id=saved["card_id"],
                payload={"card_id": saved["card_id"]},
            )
            graphiti = MagicMock()
            graphiti.ingest_news_signal_card_sync.side_effect = TimeoutError()

            result = GraphitiOutboxWorker(
                outbox_repo=outbox,
                news_repo=news_repo,
                graphiti=graphiti,
                job_timeout_seconds=10,
            ).run_once(limit=1)

            assert result["failed"] == 1
            assert result["details"][0]["status"] == "retry"
            assert result["details"][0]["error"] == "TimeoutError"
            graphiti.ingest_news_signal_card_sync.assert_called_once()
            call = graphiti.ingest_news_signal_card_sync.call_args.kwargs
            assert call["card"]["card_id"] == saved["card_id"]
            assert call["market"] == "cn"
            assert call["timeout_seconds"] == 10
        finally:
            _cleanup_db()


def test_outbox_worker_skips_episode_when_card_was_suppressed_after_enqueue() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            news_repo = NewsSignalRepository(db)
            saved = news_repo.upsert_cards([{**_card(), "status": "suppressed"}])[0]
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_episode",
                aggregate_id=saved["card_id"],
                payload={"card_id": saved["card_id"]},
            )
            graphiti = MagicMock()

            result = GraphitiOutboxWorker(
                outbox_repo=outbox,
                news_repo=news_repo,
                graphiti=graphiti,
            ).run_once(limit=10)

            assert result["succeeded"] == 1
            assert result["details"][0]["result"]["reason"] == "card_not_active"
            graphiti.ingest_news_signal_card_sync.assert_not_called()
        finally:
            _cleanup_db()


def test_outbox_worker_removes_suppressed_card_episode() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            news_repo = NewsSignalRepository(db)
            saved = news_repo.upsert_cards([{**_card(), "status": "suppressed"}])[0]
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_delete",
                aggregate_id=saved["card_id"],
                payload={"card_id": saved["card_id"]},
            )
            graphiti = MagicMock()
            graphiti.remove_news_signal_card_sync.return_value = {"status": "removed", "removed": 1}

            result = GraphitiOutboxWorker(
                outbox_repo=outbox,
                news_repo=news_repo,
                graphiti=graphiti,
            ).run_once(limit=10)

            assert result["succeeded"] == 1
            graphiti.remove_news_signal_card_sync.assert_called_once_with(
                card_id=saved["card_id"],
                market="cn",
            )
        finally:
            _cleanup_db()


def test_outbox_claims_delete_before_older_episode_write() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _setup_db(Path(temp_dir))
        try:
            outbox = GraphitiOutboxRepository(db)
            outbox.enqueue(
                event_type="news_signal_card_episode",
                aggregate_id="card:active",
                payload={"card_id": "card:active"},
            )
            outbox.enqueue(
                event_type="news_signal_card_delete",
                aggregate_id="card:suppressed",
                payload={"card_id": "card:suppressed"},
            )

            claimed = outbox.claim_batch(limit=1)

            assert claimed[0]["event_type"] == "news_signal_card_delete"
        finally:
            _cleanup_db()
