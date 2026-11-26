# -*- coding: utf-8 -*-
"""Settle news event outcomes (T+1 / T+5 excess return) into the main DB.

Data-only manual entry point; it never touches selection, prompts or thresholds.

Usage:
    python scripts/settle_news_signal_outcomes.py --dry-run
    python scripts/settle_news_signal_outcomes.py --horizons 1,5
    python scripts/settle_news_signal_outcomes.py --recompute --event-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_config
from src.services.news_signal_outcome_service import (
    adj_db_path,
    parse_horizons,
    settle_news_event_outcomes,
)
from src.storage import DatabaseManager


def _parse_args() -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizons", default=str(getattr(config, "news_signal_outcome_horizons", "1,5")))
    parser.add_argument(
        "--benchmark",
        default=str(getattr(config, "news_signal_outcome_benchmark", "000985.CSI")),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(getattr(config, "index_benchmark_cache_dir", "") or ""),
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=float(getattr(config, "news_signal_outcome_cost_bps", 20.0)),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--event-id", action="append", default=None)
    parser.add_argument("--recompute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adj-db", default=str(adj_db_path()))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = settle_news_event_outcomes(
        db=DatabaseManager.get_instance(),
        adj_path=args.adj_db,
        horizons=parse_horizons(args.horizons),
        benchmark=args.benchmark,
        cost_bps=args.cost_bps,
        limit=args.limit or None,
        recompute=args.recompute,
        event_ids=args.event_id,
        cache_dir=args.cache_dir or None,
        dry_run=args.dry_run,
    )
    result["dry_run"] = bool(args.dry_run)
    result["horizons"] = list(parse_horizons(args.horizons))
    result["benchmark"] = args.benchmark
    result["cost_bps"] = args.cost_bps
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
