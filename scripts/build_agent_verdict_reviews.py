#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build offline Agent verdict review JSONL from Agent Trace artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.agent_verdict_review_service import (
    AgentVerdictReviewService,
    DEFAULT_EVAL_WINDOWS,
)


def _parse_windows(value: str) -> list[int]:
    windows: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        windows.append(int(part))
    return windows or list(DEFAULT_EVAL_WINDOWS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Agent verdict review JSONL from local Agent Trace artifacts.")
    parser.add_argument("--trace-root", type=Path, default=None, help="Agent trace root. Defaults to DB sibling agent_traces/.")
    parser.add_argument("--output", type=Path, default=None, help="Output JSONL path. Defaults to data/agent_reviews/verdict_review.jsonl.")
    parser.add_argument("--windows", default="7,30", help="Comma-separated forward trading-day windows, e.g. 7,30.")
    parser.add_argument("--limit", type=int, default=None, help="Limit most recent trace directories.")
    parser.add_argument("--dry-run", action="store_true", help="Build rows and print summary without writing JSONL.")
    args = parser.parse_args()

    service = AgentVerdictReviewService()
    windows = _parse_windows(args.windows)
    reviews = service.build_reviews(trace_root=args.trace_root, eval_windows=windows, limit=args.limit)
    if args.dry_run:
        print(json.dumps({
            "trace_root": str(args.trace_root or service.default_trace_root()),
            "review_count": len(reviews),
            "windows": windows,
            "sample": reviews[:3],
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    result = service.write_reviews(reviews, output_path=args.output)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
