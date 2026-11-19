#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the unified factor hit-evaluation protocol (OBS-03b/OBS-05).

Examples
--------
# Main window (2022-01-01 onward), end defaults to today
python scripts/evaluate_factor.py --factor momentum_5d \
    --output data/factor_eval_reports/momentum_5d.json

# Short pipeline-check window (not for promotion decisions)
python scripts/evaluate_factor.py --factor momentum_5d \
    --start 20260701 --end 20261031 \
    --output data/factor_eval_reports/momentum_5d_short.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.factor_eval import evaluate_factor, load_registry
from src.backtest.factor_eval.evaluator import write_report
from src.config import setup_env

# Main evaluation window agreed with the protocol: 2022-01-01 onward, so the
# SW2021 industry snapshot (effective 2021-12-13) is point-in-time valid.
MAIN_WINDOW_START = "20220101"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one factor with the unified protocol")
    parser.add_argument("--factor", default="momentum_5d", help="factor_id from factors.yaml")
    parser.add_argument("--start", default=MAIN_WINDOW_START, help="first decision date, YYYYMMDD")
    parser.add_argument("--end", default=None, help="last decision date, YYYYMMDD (default: today)")
    parser.add_argument("--output", default=None, help="report JSON path")
    parser.add_argument("--cache-dir", default="data/factor_eval_cache", help="raw snapshot cache")
    parser.add_argument("--config", default=None, help="override factors.yaml")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds between Tushare calls")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.end:
        args.end = date.today().strftime("%Y%m%d")
    setup_env()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    registry = load_registry(Path(args.config)) if args.config else load_registry()
    report = evaluate_factor(
        args.factor,
        args.start,
        args.end,
        registry=registry,
        cache_dir=Path(args.cache_dir),
        sleep_seconds=args.sleep,
    )
    output = Path(args.output) if args.output else (
        Path("data/factor_eval_reports") / f"{args.factor}_{args.start}_{args.end}.json"
    )
    write_report(report, output)
    print(json.dumps(_summary(report), ensure_ascii=False, indent=2))
    print(f"report: {output}")
    return 0


def _summary(report: dict) -> dict:
    sample = report.get("sample") or {}
    primary = report.get("primary") or {}
    return {
        "factor_id": report.get("factor_id"),
        "factor_version": report.get("factor_version"),
        "verdict": report.get("verdict"),
        "reasons": report.get("reasons"),
        "promotion_eligible": report.get("promotion_eligible"),
        "survivorship_bias": report.get("survivorship_bias"),
        "decision_dates": sample.get("decision_dates"),
        "cohorts": sample.get("cohorts"),
        "observations": sample.get("observations"),
        "coverage": sample.get("coverage"),
        "executability": sample.get("executability"),
        "industry_unmatched_rate": sample.get("industry_unmatched_rate"),
        "delisted_sample_count": sample.get("delisted_sample_count"),
        "delisted_in_window_count": sample.get("delisted_in_window_count"),
        "delisted_exit_count": sample.get("delisted_exit_count"),
        "delisted_skipped_count": sample.get("delisted_skipped_count"),
        "mean_rank_ic": primary.get("mean_rank_ic"),
        "icir": primary.get("icir"),
        "newey_west_t": primary.get("newey_west_t"),
        "mde": primary.get("mde"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
