#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build offline Agent entry-execution backtest JSONL from Agent Trace artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.agent_entry_execution_backtest_service import AgentEntryExecutionBacktestService


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Agent entry-execution backtest JSONL from local Agent Trace artifacts.")
    parser.add_argument("--trace-root", type=Path, default=None, help="Agent trace root. Defaults to DB sibling agent_traces/.")
    parser.add_argument("--output", type=Path, default=None, help="Output JSONL path. Defaults to data/agent_reviews/entry_execution_backtest.jsonl.")
    parser.add_argument("--limit", type=int, default=None, help="Limit most recent trace directories.")
    parser.add_argument("--dry-run", action="store_true", help="Build rows and print summary without writing JSONL.")
    args = parser.parse_args()

    service = AgentEntryExecutionBacktestService()
    rows = service.build_backtests(trace_root=args.trace_root, limit=args.limit)
    if args.dry_run:
        print(json.dumps({
            "trace_root": str(args.trace_root or service.default_trace_root()),
            "review_count": len(rows),
            "sample": rows[:3],
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    result = service.write_backtests(rows, output_path=args.output)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
