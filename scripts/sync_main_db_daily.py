#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sync whole-market A-share daily bars into the main analysis DB.

Background: ``scripts/update_sequoia_candidates.py`` (daily_run step 1) only
writes the Sequoia candidate DB. The main DB (``DATABASE_PATH``) used by
analysis and by the decision-outcome settlement therefore used to rely on a
one-off stage0 backfill. This script turns that by-trade-date path into a
recurring daily step:

  - one Tushare ``daily(trade_date=...)`` call per missing session (never a
    per-symbol loop);
  - ``INSERT OR IGNORE`` so existing rows are never overwritten;
  - complete sessions are skipped without calling the API;
  - ``adj_factor`` for the same sessions is filled into ``adj_factors.db``;
  - upstream-not-published sessions are reported and retried on the next run;
  - PASSIVE WAL checkpoint after each session, TRUNCATE at the end.

A single failing session never aborts the others and never blocks the caller.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FULL_DAY_THRESHOLD = 4000
DEFAULT_LOOKBACK_SESSIONS = 30
DATA_SOURCE = "tushare_main_db_sync"

ADJ_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_adj_factors (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    adj_factor REAL NOT NULL,
    data_source TEXT,
    created_at TEXT,
    PRIMARY KEY (code, date)
)
"""


def _resolve_path(value: Optional[str], default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def default_main_db_path() -> Path:
    from src.config import get_config

    return _resolve_path(getattr(get_config(), "database_path", ""), ROOT / "data/stock_analysis.db")


def default_sequoia_db_path() -> Path:
    import os

    return _resolve_path(
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH"),
        ROOT / "vendor/Sequoia-X/data/sequoia_v2.db",
    )


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60)
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _wal_bytes(path: Path) -> int:
    wal = Path(str(path) + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def resolve_candidate_dates(
    sequoia_db: Path,
    *,
    lookback_sessions: int,
    target_date: Optional[str] = None,
) -> List[str]:
    """Return the recent sessions to check, newest target included.

    The Sequoia DB is refreshed by daily_run step 1 and is the local record of
    published whole-market sessions. ``target_date`` (the calendar target) is
    appended even when Sequoia has not seen it yet, so a publish-lag session is
    surfaced as ``upstream_not_published`` instead of being silently skipped.
    """
    if not sequoia_db.exists():
        raise RuntimeError(f"Sequoia candidate DB not found: {sequoia_db}")
    conn = _conn(sequoia_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM stock_daily WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (target_date or "9999-12-31", max(1, lookback_sessions)),
        ).fetchall()
    finally:
        conn.close()
    dates = sorted({str(row[0]) for row in rows if row and row[0]})
    if target_date and target_date not in dates:
        dates.append(target_date)
    return dates


def normalize_daily_frame(df: Any) -> Tuple[List[Dict[str, Any]], int]:
    """Normalize a Tushare ``daily`` frame into main-DB rows (A-share units)."""
    if df is None or getattr(df, "empty", True):
        return [], 0
    rows: List[Dict[str, Any]] = []
    dropped = 0
    for rec in df.to_dict("records"):
        code = str(rec.get("ts_code") or "").split(".")[0].strip()
        close = rec.get("close")
        trade_date = str(rec.get("trade_date") or "")
        if len(code) != 6 or not code.isdigit() or close is None or len(trade_date) != 8:
            dropped += 1
            continue
        rows.append(
            {
                "code": code,
                "date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}",
                "open": rec.get("open"),
                "high": rec.get("high"),
                "low": rec.get("low"),
                "close": close,
                "volume": None if rec.get("vol") is None else rec["vol"] * 100,
                "amount": None if rec.get("amount") is None else rec["amount"] * 1000,
                "pct_chg": rec.get("pct_chg"),
            }
        )
    return rows, dropped


def _count(conn: sqlite3.Connection, table: str, day: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE date=?", (day,)).fetchone()[0])


def _insert_daily(conn: sqlite3.Connection, rows: Sequence[Dict[str, Any]], day: str, stamp: str) -> int:
    conn.execute("BEGIN IMMEDIATE")
    before = conn.total_changes
    payload = [
        (
            r["code"], r["date"], r["open"], r["high"], r["low"], r["close"],
            r["volume"], r["amount"], r["pct_chg"], DATA_SOURCE, stamp, stamp,
        )
        for r in rows
    ]
    if payload:
        conn.executemany(
            "INSERT OR IGNORE INTO stock_daily "
            "(code,date,open,high,low,close,volume,amount,pct_chg,data_source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
    inserted = conn.total_changes - before
    conn.execute("COMMIT")
    return inserted


def _insert_adj(conn: sqlite3.Connection, rows: Sequence[Tuple[str, str, float]], stamp: str) -> int:
    conn.execute("BEGIN IMMEDIATE")
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO stock_adj_factors (code,date,adj_factor,data_source,created_at) VALUES (?,?,?,?,?)",
        [(code, day, factor, DATA_SOURCE, stamp) for code, day, factor in rows],
    )
    inserted = conn.total_changes - before
    conn.execute("COMMIT")
    return inserted


def _normalize_adj_frame(df: Any, day: str) -> List[Tuple[str, str, float]]:
    if df is None or getattr(df, "empty", True):
        return []
    out: List[Tuple[str, str, float]] = []
    for rec in df.to_dict("records"):
        code = str(rec.get("ts_code") or "").split(".")[0].strip()
        factor = rec.get("adj_factor")
        if len(code) == 6 and code.isdigit() and factor is not None:
            out.append((code, day, float(factor)))
    return out


def sync_main_db_daily(
    *,
    lookback_sessions: int = DEFAULT_LOOKBACK_SESSIONS,
    target_date: Optional[str] = None,
    main_db: Optional[Path] = None,
    adj_db: Optional[Path] = None,
    sequoia_db: Optional[Path] = None,
    client: Any = None,
    full_day_threshold: int = FULL_DAY_THRESHOLD,
    logger: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Backfill missing whole-market sessions into the main DB. Never raises per day."""
    from datetime import datetime

    if target_date is None:
        from src.core.trading_calendar import get_effective_trading_date

        target_date = get_effective_trading_date("cn").isoformat()

    main_path = Path(main_db) if main_db else default_main_db_path()
    adj_path = Path(adj_db) if adj_db else (main_path.parent / "adj_factors.db")
    seq_path = Path(sequoia_db) if sequoia_db else default_sequoia_db_path()
    if client is None:
        from data_provider.tushare_client import build_tushare_http_client

        client = build_tushare_http_client(timeout=60)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    summary: Dict[str, Any] = {
        "target_date": target_date,
        "lookback_sessions": lookback_sessions,
        "main_db": str(main_path),
        "adj_db": str(adj_path),
        "days": [],
        "errors": [],
        "peak_wal_bytes": 0,
        "inserted_rows": 0,
        "inserted_adj_rows": 0,
    }

    try:
        dates = resolve_candidate_dates(seq_path, lookback_sessions=lookback_sessions, target_date=target_date)
    except Exception as exc:
        # The main-DB sync must not depend on the Sequoia DB being readable.
        logger(f"[WARN] 无法读取 Sequoia 交易日历（{exc}），本次只检查目标日 {target_date}")
        dates = [target_date]
    summary["candidate_dates"] = dates

    main_conn = _conn(main_path)
    adj_conn = _conn(adj_path)
    adj_conn.execute(ADJ_TABLE_SQL)
    try:
        for day in dates:
            result: Dict[str, Any] = {"date": day}
            try:
                main_count = _count(main_conn, "stock_daily", day)
                adj_count = _count(adj_conn, "stock_adj_factors", day)
                need_daily = main_count < full_day_threshold
                need_adj = adj_count < full_day_threshold
                result.update({"rows_before": main_count, "adj_rows_before": adj_count})
                result["status"] = "skipped_complete"
                result["adj_status"] = "skipped_complete"

                if not need_daily and not need_adj:
                    pass
                else:
                    daily_rows: Optional[List[Dict[str, Any]]] = None
                    if need_daily:
                        frame = client.daily(trade_date=day.replace("-", ""))
                        daily_rows, dropped = normalize_daily_frame(frame)
                        result["dropped_rows"] = dropped
                        if not daily_rows:
                            result["status"] = "upstream_not_published"
                        else:
                            result["inserted"] = _insert_daily(main_conn, daily_rows, day, stamp)
                            summary["inserted_rows"] += result["inserted"]
                            result["status"] = "synced"

                    if need_adj and result.get("status") != "upstream_not_published":
                        adj_frame = client.adj_factor(trade_date=day.replace("-", ""))
                        adj_rows = _normalize_adj_frame(adj_frame, day)
                        if adj_rows:
                            result["adj_inserted"] = _insert_adj(adj_conn, adj_rows, stamp)
                            summary["inserted_adj_rows"] += result["adj_inserted"]
                            result["adj_status"] = "synced"
                        else:
                            result["adj_status"] = "upstream_not_published"

                    if result.get("status") == "upstream_not_published":
                        logger(f"[WARN] 上游未发布 {day} 的日线，留待下次运行补齐")

                result["rows_after"] = _count(main_conn, "stock_daily", day)
                result["adj_rows_after"] = _count(adj_conn, "stock_adj_factors", day)
            except Exception as exc:  # one bad session must not stop the rest
                try:
                    main_conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                try:
                    adj_conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                result["status"] = "error"
                result["error"] = f"{type(exc).__name__}: {exc}"
                summary["errors"].append({"date": day, "error": result["error"]})
                logger(f"[ERROR] {day} 同步失败: {result['error']}")

            result["wal_bytes"] = max(_wal_bytes(main_path), _wal_bytes(adj_path))
            summary["peak_wal_bytes"] = max(summary["peak_wal_bytes"], result["wal_bytes"])
            main_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            adj_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            summary["days"].append(result)
            logger(json.dumps(result, ensure_ascii=False))

        main_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        adj_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        main_conn.close()
        adj_conn.close()

    summary["wal_bytes_final"] = max(_wal_bytes(main_path), _wal_bytes(adj_path))
    summary["ok"] = not summary["errors"]
    return summary


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync whole-market daily bars into the main analysis DB.")
    parser.add_argument("--lookback-sessions", type=int, default=DEFAULT_LOOKBACK_SESSIONS)
    parser.add_argument("--target-date", default=None, help="YYYY-MM-DD; defaults to the effective A-share session")
    parser.add_argument("--main-db", default=None)
    parser.add_argument("--adj-db", default=None)
    parser.add_argument("--sequoia-db", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    from src.config import setup_env

    setup_env()

    try:
        summary = sync_main_db_daily(
            lookback_sessions=args.lookback_sessions,
            target_date=args.target_date,
            main_db=Path(args.main_db) if args.main_db else None,
            adj_db=Path(args.adj_db) if args.adj_db else None,
            sequoia_db=Path(args.sequoia_db) if args.sequoia_db else None,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    print(json.dumps({k: v for k, v in summary.items() if k != "days"}, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
