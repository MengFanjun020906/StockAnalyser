# -*- coding: utf-8 -*-
"""Background registration for the paper-trading shadow books.

The task is deliberately weak: it never runs the selection chain or an LLM.
It only (1) books newly persisted artifacts and (2) records the daily close
NAV.  Both steps are idempotent, so a missed tick is harmless.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.agent.paper_trading.runner import (
    STATUS_NO_BAR,
    STATUS_OK,
    ingest_only,
    run_paper_books,
)
from src.config import get_config

logger = logging.getLogger(__name__)

DEFAULT_RUN_AFTER = "18:00"
SETTLE_INTERVAL_SECONDS = 1800


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour, minute = str(value or DEFAULT_RUN_AFTER).split(":", 1)
        return int(hour), int(minute)
    except (TypeError, ValueError):
        return 18, 0


def run_paper_trading_once(
    *,
    config: Any = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Book new artifacts at any time; record the close NAV after ``run_after``."""
    config = config or get_config()
    if not bool(getattr(config, "agent_paper_trading_enabled", False)):
        return None
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return None

    hour, minute = _parse_hhmm(str(getattr(config, "agent_paper_trading_run_after", DEFAULT_RUN_AFTER)))
    if (moment.hour, moment.minute) < (hour, minute):
        result = ingest_only(config=config, session_date=moment.date())
        if result.get("ingested"):
            logger.info("[PaperTrading] 入账新产物 %s 个，等待收盘后记净值", result.get("ingested"))
        return {"status": "ingested_only", **result}

    result = run_paper_books(as_of=moment.date(), config=config, write_report=True)
    if result.get("status") == STATUS_OK:
        for book, book_result in (result.get("books") or {}).items():
            if book_result.get("status") == STATUS_NO_BAR:
                logger.debug("[PaperTrading] %s 跳过 %s：本地无当日日线", book, book_result.get("as_of"))
                continue
            logger.info(
                "[PaperTrading] %s settled as_of=%s equity=%s trades=%s positions=%s",
                book,
                book_result.get("as_of"),
                book_result.get("equity"),
                book_result.get("trades"),
                book_result.get("positions_count"),
            )
    else:
        logger.warning("[PaperTrading] 会话未完成: %s", result)
    return result


def build_paper_trading_background_tasks(config: Any) -> List[Dict[str, Any]]:
    """Register the ingest/settle task; empty when the feature is disabled."""
    if not bool(getattr(config, "agent_paper_trading_enabled", False)):
        return []

    def _paper_task() -> None:
        run_paper_trading_once(config=config)

    return [
        {
            "task": _paper_task,
            "interval_seconds": SETTLE_INTERVAL_SECONDS,
            "run_immediately": True,
            "name": "paper_trading_shadow",
        }
    ]


__all__ = ["build_paper_trading_background_tasks", "run_paper_trading_once"]
