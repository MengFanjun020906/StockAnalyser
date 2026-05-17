#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update local fundamental candidate snapshots from Tushare.

This script intentionally writes a precomputed SQLite table so Agent Trace can
discover fundamental candidates without full-market financial API calls.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import setup_env  # noqa: E402
from data_provider.tushare_client import get_tushare_token, query_tushare_api  # noqa: E402
from src.agent.candidate_providers.fundamental_provider import (  # noqa: E402
    ensure_fundamental_candidate_schema,
    upsert_fundamental_snapshots,
)


setup_env()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Agent fundamental candidate SQLite snapshots.")
    parser.add_argument("--db-path", default=None, help="SQLite DB path. Defaults to AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH or DATABASE_PATH.")
    parser.add_argument("--limit", type=int, default=300, help="Max stock_basic rows to process. Use 0 for all.")
    parser.add_argument("--period", default="", help="Report period YYYYMMDD. Empty means latest rows returned by Tushare.")
    parser.add_argument("--batch-size", type=int, default=80, help="stock_basic query limit; Tushare APIs remain per-stock for fina_indicator.")
    parser.add_argument("--progress-every", type=int, default=10, help="Refresh progress display every N processed stocks.")
    parser.add_argument("--no-progress", action="store_true", help="Disable in-place progress display.")
    parser.add_argument("--resume", action="store_true", help="Skip codes that already exist in fundamental_candidate_snapshot.")
    parser.add_argument("--force", action="store_true", help="Refresh all selected stocks even when snapshot rows already exist.")
    parser.add_argument("--flush-every", type=int, default=20, help="Upsert snapshots every N collected rows so interrupted runs keep progress.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not get_tushare_token():
        print("TUSHARE_TOKEN is not configured", file=sys.stderr)
        return 2
    db_path = ensure_fundamental_candidate_schema(args.db_path)
    basics = _fetch_stock_basic(limit=args.limit)
    if not basics:
        print("No stock_basic rows returned", file=sys.stderr)
        return 1
    existing_codes = _load_existing_codes(db_path) if args.resume and not args.force else set()
    skipped = 0
    if existing_codes:
        filtered_basics = []
        for item in basics:
            ts_code = str(item.get("ts_code") or "").strip()
            code = ts_code.split(".", 1)[0]
            if code in existing_codes:
                skipped += 1
                continue
            filtered_basics.append(item)
        basics = filtered_basics
    if not basics:
        print(f"db_path={db_path}")
        print(f"all selected stock_basic rows already exist; skipped={skipped}")
        return 0

    rows_buffer: List[Dict[str, Any]] = []
    errors: List[str] = []
    written = 0
    flush_every = max(1, int(args.flush_every or 20))
    progress = ProgressReporter(total=len(basics), enabled=not args.no_progress, every=args.progress_every)
    interrupted = False
    try:
        for index, item in enumerate(basics, start=1):
            ts_code = str(item.get("ts_code") or "").strip()
            if not ts_code:
                progress.update(index, written + len(rows_buffer), written, len(errors), current="-")
                continue
            try:
                row = _fetch_latest_fina_indicator(ts_code, name=str(item.get("name") or ""), period=args.period)
            except Exception as exc:
                errors.append(f"{ts_code}:{exc}")
                progress.update(index, written + len(rows_buffer), written, len(errors), current=ts_code)
                continue
            if row:
                rows_buffer.append(row)
            if len(rows_buffer) >= flush_every:
                written += _flush_rows(rows_buffer, db_path)
            progress.update(index, written + len(rows_buffer), written, len(errors), current=ts_code)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; flushing buffered snapshots before exit...", file=sys.stderr)
    finally:
        written += _flush_rows(rows_buffer, db_path)
        progress.finish(written, written, len(errors), interrupted=interrupted)
    print(f"db_path={db_path}")
    print(f"stock_basic={len(basics)} skipped_existing={skipped} snapshots_written={written} errors={len(errors)}")
    if errors:
        print("sample_errors=" + " | ".join(errors[:5]), file=sys.stderr)
    if interrupted:
        return 130
    return 0 if written else 1


class ProgressReporter:
    """Small dependency-free terminal progress reporter."""

    def __init__(self, *, total: int, enabled: bool = True, every: int = 10) -> None:
        self.total = max(0, int(total or 0))
        self.enabled = bool(enabled)
        self.every = max(1, int(every or 10))
        self.started_at = time.time()
        self._last_len = 0

    def update(self, processed: int, collected: int, written: int, errors: int, *, current: str) -> None:
        if not self.enabled:
            return
        if processed < self.total and processed % self.every != 0:
            return
        elapsed = max(0.001, time.time() - self.started_at)
        rate = processed / elapsed
        remaining = max(0, self.total - processed)
        eta = remaining / rate if rate > 0 else 0
        percent = (processed / self.total * 100) if self.total else 0
        bar = _progress_bar(percent)
        message = (
            f"\r{bar} {processed}/{self.total} {percent:5.1f}% "
            f"collected={collected} written={written} errors={errors} "
            f"rate={rate:.2f}/s eta={_format_seconds(eta)} current={current}"
        )
        padding = " " * max(0, self._last_len - len(message))
        print(message + padding, end="", flush=True)
        self._last_len = len(message)

    def finish(self, collected: int, written: int, errors: int, *, interrupted: bool = False) -> None:
        if not self.enabled:
            return
        elapsed = time.time() - self.started_at
        status = "interrupted" if interrupted else "done"
        message = (
            f"\r{_progress_bar(100)} {self.total}/{self.total} 100.0% "
            f"collected={collected} written={written} errors={errors} elapsed={_format_seconds(elapsed)} {status}"
        )
        padding = " " * max(0, self._last_len - len(message))
        print(message + padding, flush=True)


def _progress_bar(percent: float, width: int = 24) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _format_seconds(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _fetch_stock_basic(limit: int) -> List[Dict[str, Any]]:
    df = query_tushare_api(
        "stock_basic",
        params={"exchange": "", "list_status": "L"},
        fields="ts_code,symbol,name,area,industry,list_date",
        timeout=30,
    )
    records = df.to_dict(orient="records") if df is not None and not df.empty else []
    if limit and limit > 0:
        return records[:limit]
    return records


def _load_existing_codes(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT code FROM fundamental_candidate_snapshot").fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def _flush_rows(rows: List[Dict[str, Any]], db_path: str) -> int:
    if not rows:
        return 0
    pending = list(rows)
    rows.clear()
    return upsert_fundamental_snapshots(pending, db_path)


def _fetch_latest_fina_indicator(ts_code: str, *, name: str, period: str = "") -> Optional[Dict[str, Any]]:
    params = {"ts_code": ts_code}
    if period:
        params["period"] = period
    df = query_tushare_api(
        "fina_indicator",
        params=params,
        fields=(
            "ts_code,ann_date,end_date,roe,grossprofit_margin,netprofit_margin,"
            "or_yoy,netprofit_yoy,ocfps,debt_to_assets"
        ),
        timeout=20,
    )
    if df is None or df.empty:
        return None
    df = df.sort_values(["end_date", "ann_date"], ascending=[False, False])
    latest = df.iloc[0].to_dict()
    valuation = _fetch_latest_daily_basic(ts_code)
    code = str(ts_code).split(".", 1)[0]
    return {
        "code": code,
        "name": name,
        "report_period": latest.get("end_date"),
        "ann_date": latest.get("ann_date"),
        "roe": latest.get("roe"),
        "gross_margin": latest.get("grossprofit_margin"),
        "net_margin": latest.get("netprofit_margin"),
        "revenue_growth": latest.get("or_yoy"),
        "profit_growth": latest.get("netprofit_yoy"),
        "operating_cashflow_ratio": _ocfps_proxy(latest.get("ocfps")),
        "debt_ratio": latest.get("debt_to_assets"),
        "pe_ttm": valuation.get("pe_ttm"),
        "pb": valuation.get("pb"),
        "market_cap": valuation.get("market_cap"),
        "dividend_yield": valuation.get("dividend_yield"),
        "source": "tushare:fina_indicator",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _fetch_latest_daily_basic(ts_code: str) -> Dict[str, Any]:
    try:
        df = query_tushare_api(
            "daily_basic",
            params={"ts_code": ts_code},
            fields="ts_code,trade_date,pe_ttm,pb,total_mv,dv_ttm",
            timeout=20,
        )
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    latest = df.sort_values("trade_date", ascending=False).iloc[0].to_dict()
    return {
        "pe_ttm": latest.get("pe_ttm"),
        "pb": latest.get("pb"),
        "market_cap": latest.get("total_mv"),
        "dividend_yield": latest.get("dv_ttm"),
    }


def _ocfps_proxy(value: Any) -> Optional[float]:
    try:
        return float(value) * 100
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
