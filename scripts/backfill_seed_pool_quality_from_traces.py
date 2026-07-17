#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill Seed Pool quality snapshots from historical agent traces."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.seed_pool_quality_service import (  # noqa: E402
    CN_TZ,
    SeedPoolQualityService,
    infer_seed_pool_snapshot_date,
)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _trace_generated_at(trace_dir: Path) -> datetime:
    prefix = trace_dir.name[:15]
    try:
        return datetime.strptime(prefix, "%Y%m%d-%H%M%S").replace(tzinfo=CN_TZ)
    except Exception:
        return datetime.fromtimestamp(trace_dir.stat().st_mtime, tz=CN_TZ)


def _candidate_payload_from_trace(trace_dir: Path) -> Optional[Dict[str, Any]]:
    candidate = _load_json(trace_dir / "candidate_discovery.json")
    if candidate:
        full = candidate.get("full")
        if isinstance(full, dict) and isinstance(full.get("seed_pool_summary"), dict):
            return full
        if isinstance(candidate.get("seed_pool_summary"), dict):
            return candidate

    seed_pool = _load_json(trace_dir / "seed_pool.json")
    if seed_pool and isinstance(seed_pool.get("seed_pool_summary"), dict):
        return {
            "status": "ok",
            "market": "cn",
            "candidate_source": "trace_seed_pool",
            "seed_pool_summary": seed_pool.get("seed_pool_summary"),
            "seed_pool_diagnostics": seed_pool.get("seed_pool_diagnostics") or [],
            "seed_pool_hard_exclusion": seed_pool.get("seed_pool_hard_exclusion") or {},
            "seed_source_quality": seed_pool.get("seed_source_quality") or {},
            "seed_market_regime": seed_pool.get("seed_market_regime") or {},
        }
    return None


def _iter_trace_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )


def _parse_date_arg(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Invalid date: {value}")


def _in_range(seed_date: Optional[date], since: Optional[date], until: Optional[date]) -> bool:
    if seed_date is None:
        return True
    if since and seed_date < since:
        return False
    if until and seed_date > until:
        return False
    return True


def backfill(args: argparse.Namespace) -> Dict[str, Any]:
    trace_root = Path(args.trace_root)
    since = _parse_date_arg(args.since)
    until = _parse_date_arg(args.until)
    service = SeedPoolQualityService()
    processed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    trace_dirs = list(_iter_trace_dirs(trace_root))
    if args.limit:
        trace_dirs = trace_dirs[-max(1, int(args.limit)) :]

    for trace_dir in trace_dirs:
        payload = _candidate_payload_from_trace(trace_dir)
        if payload is None:
            skipped.append({"trace_id": trace_dir.name, "reason": "missing_seed_pool_payload"})
            continue

        seed_date = infer_seed_pool_snapshot_date(payload)
        if not _in_range(seed_date, since, until):
            skipped.append({
                "trace_id": trace_dir.name,
                "seed_date": seed_date.isoformat() if seed_date else None,
                "reason": "outside_date_range",
            })
            continue

        generated_at = _trace_generated_at(trace_dir)
        run_id = str(payload.get("run_id") or trace_dir.name)
        trace_id = str(payload.get("trace_id") or trace_dir.name)
        candidate_mode = str(payload.get("candidate_discovery_mode") or payload.get("candidate_source") or "trace_backfill")
        if args.dry_run:
            processed.append({
                "trace_id": trace_id,
                "run_id": run_id,
                "seed_date": seed_date.isoformat() if seed_date else None,
                "status": "dry_run",
            })
            continue

        try:
            saved = service.persist_candidate_discovery_snapshot(
                candidate_discovery=payload,
                run_id=run_id,
                trace_id=trace_id,
                seed_date=seed_date,
                generated_at=generated_at,
                market=str(payload.get("market") or "cn"),
                candidate_discovery_mode=candidate_mode,
            )
            processed.append({
                "trace_id": trace_id,
                "run_id": run_id,
                "seed_date": seed_date.isoformat() if seed_date else None,
                **saved,
            })
        except Exception as exc:
            errors.append({
                "trace_id": trace_id,
                "seed_date": seed_date.isoformat() if seed_date else None,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {
        "status": "ok" if not errors else "partial",
        "trace_root": str(trace_root),
        "dry_run": bool(args.dry_run),
        "processed": len(processed),
        "skipped": len(skipped),
        "errors": len(errors),
        "processed_items": processed,
        "skipped_items": skipped[:50],
        "error_items": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", default="data/agent_traces", help="Agent trace directory root.")
    parser.add_argument("--since", default="", help="Optional seed_date lower bound, YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--until", default="", help="Optional seed_date upper bound, YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--limit", type=int, default=0, help="Only scan the latest N trace directories.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect traces without writing snapshots.")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit non-zero if any trace fails.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = backfill(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if args.fail_on_error and result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
