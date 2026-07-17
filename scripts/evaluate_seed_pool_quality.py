#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate due Seed Pool quality snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.repositories.seed_pool_quality_repo import SeedPoolQualityRepository  # noqa: E402
from src.services.seed_pool_quality_service import (  # noqa: E402
    SeedPoolEvaluationPreconditionError,
    SeedPoolQualityService,
)


def _parse_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid date: {value}")


def _candidate_dates(args: argparse.Namespace) -> List[date]:
    if args.seed_date:
        return [_parse_date(value) for value in args.seed_date]
    repo = SeedPoolQualityRepository()
    return [
        _parse_date(str(row["seed_date"]))
        for row in repo.list_dates(limit=max(1, int(args.days or 30)))
        if row.get("seed_date")
    ]


def evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    dates = _candidate_dates(args)
    service = SeedPoolQualityService()
    results: List[Dict[str, Any]] = []
    errors = 0
    updated = 0
    skipped = 0

    for seed_date in dates:
        if args.dry_run:
            results.append({"seed_date": seed_date.isoformat(), "status": "dry_run"})
            continue
        try:
            payload = service.evaluate_seed_date(seed_date, limit=max(1, int(args.limit or 500)))
            status = "ok"
            if int(payload.get("updated") or 0) == 0 and int(payload.get("requested") or 0) == 0:
                status = "skipped"
                skipped += 1
            else:
                updated += int(payload.get("updated") or 0)
            results.append({"status": status, **payload})
        except SeedPoolEvaluationPreconditionError as exc:
            skipped += 1
            results.append({
                "seed_date": seed_date.isoformat(),
                "status": "skipped",
                "error": exc.error,
                "message": exc.message,
                "details": exc.details,
            })
        except Exception as exc:
            errors += 1
            results.append({
                "seed_date": seed_date.isoformat(),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {
        "status": "ok" if errors == 0 else "partial",
        "dry_run": bool(args.dry_run),
        "date_count": len(dates),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-date", action="append", default=[], help="Seed date to evaluate; can repeat.")
    parser.add_argument("--days", type=int, default=30, help="When --seed-date is absent, inspect latest N seed dates.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum seed items per date.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidate dates without evaluating.")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit non-zero on unexpected errors.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = evaluate(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if args.fail_on_error and result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
