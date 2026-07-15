#!/usr/bin/env python3
"""Run bounded daily maintenance for news signals and Graphiti projections."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402
from src.services.news_signal_service import NewsSignalService  # noqa: E402
from src.services.graphiti.outbox_worker import GraphitiOutboxWorker  # noqa: E402


setup_env()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = str(os.getenv(name, default) or default).strip().lower()
    return value if value in choices else default


def _incomplete_phases(result: dict[str, object]) -> list[dict[str, str]]:
    incomplete: list[dict[str, str]] = []
    for phase in ("mapping_repair", "cluster_reconcile", "event_backfill", "graph_repair", "outbox_worker"):
        payload = result.get(phase)
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "unknown").strip().lower()
        if status != "ok":
            incomplete.append({"phase": phase, "status": status})
    return incomplete


@contextmanager
def _time_limit(seconds: int) -> Iterator[None]:
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _raise_timeout(_signum, _frame) -> None:
        raise TimeoutError(f"news signal maintenance exceeded {seconds}s")

    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", default="", help="Signal date in YYYY-MM-DD format")
    parser.add_argument("--skip-rebuild", action="store_true")
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--skip-mapping-repair", action="store_true")
    parser.add_argument("--skip-cluster-reconcile", action="store_true")
    parser.add_argument("--skip-outcomes", action="store_true")
    parser.add_argument("--skip-outbox", action="store_true")
    parser.add_argument(
        "--graph-repair",
        choices=("off", "edges", "episodes"),
        default=_env_choice(
            "NEWS_SIGNAL_GRAPH_REPAIR_MODE",
            "edges",
            ("off", "edges", "episodes"),
        ),
    )
    parser.add_argument(
        "--graph-limit",
        type=int,
        default=_env_int("NEWS_SIGNAL_GRAPH_REPAIR_LIMIT", 100, minimum=1, maximum=500),
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=_env_int("NEWS_SIGNAL_BACKFILL_LIMIT", 500, minimum=1, maximum=500),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_env_int("NEWS_SIGNAL_MAINTENANCE_TIMEOUT_SECONDS", 300, minimum=30, maximum=3600),
    )
    parser.add_argument(
        "--outbox-limit",
        type=int,
        default=_env_int("GRAPHITI_OUTBOX_BATCH_SIZE", 10, minimum=1, maximum=100),
    )
    parser.add_argument(
        "--include-semantic-edges",
        action="store_true",
        default=_env_bool("NEWS_SIGNAL_INCLUDE_SEMANTIC_EDGES", False),
    )
    args = parser.parse_args()

    service = NewsSignalService()
    result: dict[str, object] = {
        "target_date": args.target_date or None,
        "graph_repair_mode": args.graph_repair,
    }
    try:
        with _time_limit(args.timeout_seconds):
            if not args.skip_rebuild:
                result["rebuild"] = service.rebuild(
                    target_date=args.target_date,
                    sync_graphiti=False,
                    include_semantic_edges=args.include_semantic_edges,
                )
            if not args.skip_mapping_repair:
                result["mapping_repair"] = service.repair_company_mapping_gates(
                    signal_date=args.target_date,
                    limit=args.backfill_limit,
                )
            if not args.skip_cluster_reconcile:
                result["cluster_reconcile"] = service.reconcile_same_event_clusters(
                    signal_date=args.target_date,
                    limit=args.backfill_limit,
                )
            if not args.skip_backfill:
                result["event_backfill"] = service.backfill_extracted_events(
                    signal_date=args.target_date,
                    limit=args.backfill_limit,
                )
            if not args.skip_outcomes:
                result["outcomes"] = service.refresh_outcomes()
            if args.graph_repair != "off":
                result["graph_repair"] = service.sync_graphiti(
                    signal_date=args.target_date,
                    limit=args.graph_limit,
                    include_semantic_edges=args.include_semantic_edges,
                    include_episodes=args.graph_repair == "episodes",
                )
            if not args.skip_outbox and args.graph_repair != "off":
                result["outbox_worker"] = GraphitiOutboxWorker().run_once(
                    limit=args.outbox_limit,
                )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1

    incomplete = _incomplete_phases(result)
    if incomplete:
        result["status"] = "partial"
        result["incomplete_phases"] = incomplete
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1

    result["status"] = "ok"
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
