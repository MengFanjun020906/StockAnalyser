#!/usr/bin/env python3
"""Run only news-signal background workers.

This entrypoint is intentionally narrower than ``main.py --schedule``: it does
not register the daily stock-analysis task and therefore cannot send stock or
market-review reports through notification channels.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Config, setup_env  # noqa: E402
from src.scheduler import Scheduler  # noqa: E402
from src.services.news_signal_scheduler import build_news_signal_background_tasks  # noqa: E402


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run configured news background tasks once and exit.",
    )
    args = parser.parse_args()

    setup_env()
    _configure_logging()
    config = Config.get_instance()
    tasks = build_news_signal_background_tasks(config)
    if not tasks:
        logging.warning("No news-signal background tasks are enabled.")
        return 0

    if args.once:
        for entry in tasks:
            logging.info("Running news task once: %s", entry.get("name") or "background_task")
            entry["task"]()
        return 0

    scheduler = Scheduler(schedule_time="23:59")
    for entry in tasks:
        scheduler.add_background_task(
            task=entry["task"],
            interval_seconds=int(entry["interval_seconds"]),
            run_immediately=bool(entry.get("run_immediately", False)),
            name=str(entry.get("name") or "background_task"),
        )
    scheduler.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
