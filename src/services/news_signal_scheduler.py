# -*- coding: utf-8 -*-
"""Scheduled background tasks for news signals and Graphiti projections."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.services.news_signal_service import NewsSignalService
from src.services.graphiti.outbox_worker import GraphitiOutboxWorker

logger = logging.getLogger(__name__)


def build_news_signal_background_tasks(config: Any) -> List[Dict[str, Any]]:
    """Build opt-in scheduler entries from the current runtime configuration."""

    tasks: List[Dict[str, Any]] = []
    if bool(getattr(config, "news_signal_cls_incremental_enabled", False)):
        interval_minutes = max(
            5,
            min(int(getattr(config, "news_signal_cls_incremental_interval_minutes", 10) or 10), 10),
        )
        limit = max(
            1,
            min(int(getattr(config, "news_signal_cls_incremental_limit", 50) or 50), 50),
        )

        def cls_incremental_task() -> None:
            result = NewsSignalService().ingest_cls_incremental(limit=limit)
            status = str(result.get("status") or "unknown")
            if status == "failed":
                raise RuntimeError(str(result.get("errors") or "CLS incremental ingest failed"))
            logger.info(
                "[NewsSignal] CLS 增量完成: status=%s new=%s cursor=%s",
                status,
                result.get("new_raw_episodes", 0),
                result.get("cursor"),
            )

        tasks.append(
            {
                "task": cls_incremental_task,
                "interval_seconds": interval_minutes * 60,
                "run_immediately": True,
                "name": "news_signal_cls_incremental",
            }
        )
    if bool(getattr(config, "graphiti_enabled", False)) and bool(
        getattr(config, "graphiti_outbox_worker_enabled", True)
    ):
        interval_seconds = max(
            30,
            min(int(getattr(config, "graphiti_outbox_interval_seconds", 60) or 60), 3600),
        )
        batch_size = max(
            1,
            min(int(getattr(config, "graphiti_outbox_batch_size", 10) or 10), 100),
        )
        max_attempts = max(
            1,
            min(int(getattr(config, "graphiti_outbox_max_attempts", 5) or 5), 20),
        )
        retry_base_seconds = max(
            1,
            min(int(getattr(config, "graphiti_outbox_retry_base_seconds", 30) or 30), 3600),
        )
        job_timeout_seconds = max(
            10,
            min(int(getattr(config, "graphiti_outbox_job_timeout_seconds", 120) or 120), 600),
        )

        def graphiti_outbox_task() -> None:
            result = GraphitiOutboxWorker(
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                job_timeout_seconds=job_timeout_seconds,
            ).run_once(limit=batch_size)
            logger.info(
                "[GraphitiOutbox] claimed=%s succeeded=%s failed=%s dead=%s",
                result.get("claimed", 0),
                result.get("succeeded", 0),
                result.get("failed", 0),
                result.get("dead", 0),
            )

        tasks.append(
            {
                "task": graphiti_outbox_task,
                "interval_seconds": interval_seconds,
                "run_immediately": True,
                "name": "graphiti_outbox_worker",
            }
        )
    return tasks
