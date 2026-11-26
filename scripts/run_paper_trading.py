# -*- coding: utf-8 -*-
"""Manual driver for the paper-trading shadow books.

The shadow account consumes artifacts only; it never runs the selection chain
or an LLM.  Typical use: run a selection / single-stock analysis, then feed it
into the books and look at both NAV curves.

Examples:
    python scripts/run_paper_trading.py --date 2026-11-24
    python scripts/run_paper_trading.py --date 2026-11-24 --book A
    python scripts/run_paper_trading.py --date 2026-11-24 --reingest
    python scripts/run_paper_trading.py --report-only
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.paper_trading.runner import (
    BOOKS,
    build_paper_report,
    ingest_only,
    run_paper_books,
)
from src.config import get_config


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-trading shadow books (A/B)")
    parser.add_argument("--date", type=_parse_date, default=None, help="结算日（默认最近已完成交易日）")
    parser.add_argument("--book", default="A,B", help="要结算的账本，逗号分隔（A,B）")
    parser.add_argument("--trace-dir", default="", help="选股 trace 产物目录（覆盖配置）")
    parser.add_argument("--db-dir", default="", help="影子库目录（覆盖配置）")
    parser.add_argument("--no-ingest", action="store_true", help="只结算已有产物，不扫描新产物")
    parser.add_argument("--reingest", action="store_true", help="强制重新解析已入账的 run_id")
    parser.add_argument("--ingest-only", action="store_true", help="只入账产物，不更新净值")
    parser.add_argument("--report-only", action="store_true", help="只重建报告，不跑会话")
    parser.add_argument("--force", action="store_true", help="忽略 AGENT_PAPER_TRADING_ENABLED 开关")
    args = parser.parse_args()

    config = get_config()
    overrides = {}
    if args.force:
        overrides["agent_paper_trading_enabled"] = True
    if args.trace_dir:
        overrides["agent_paper_trading_trace_dir"] = args.trace_dir
    if args.db_dir:
        overrides["agent_paper_trading_db_dir"] = args.db_dir
    if overrides:
        config = replace(config, **overrides)

    books = [item.strip().upper() for item in args.book.split(",") if item.strip()] or list(BOOKS)
    books = [book for book in books if book in BOOKS] or list(BOOKS)

    if args.report_only:
        report = build_paper_report(config=config, books=books)
        _print(
            {
                "generated_at": report.get("generated_at"),
                "as_of": report.get("as_of"),
                "comparison": report.get("comparison"),
                "books": {
                    book: {
                        "account": data.get("account"),
                        "benchmark": data.get("benchmark"),
                        "close_mode": data.get("close_mode"),
                    }
                    for book, data in report.get("books", {}).items()
                },
                "output_dir": report.get("output_dir"),
            }
        )
        return 0

    if args.ingest_only:
        result = ingest_only(config=config, session_date=args.date, books=books)
        _print(result)
        return 0 if result.get("status") == "ok" else 1

    result = run_paper_books(
        as_of=args.date,
        config=config,
        books=books,
        ingest=not args.no_ingest,
        write_report=True,
        force_reingest=args.reingest,
    )
    printable = {
        "status": result.get("status"),
        "as_of": result.get("as_of"),
        "ingest": result.get("ingest"),
        "books": {
            book: {
                "status": data.get("status"),
                "equity": data.get("equity"),
                "cash": data.get("cash"),
                "positions_count": data.get("positions_count"),
                "trades": data.get("trades"),
            }
            for book, data in (result.get("books") or {}).items()
        },
    }
    report = result.get("report") or {}
    printable["comparison"] = report.get("comparison")
    printable["output_dir"] = report.get("output_dir")
    _print(printable)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
