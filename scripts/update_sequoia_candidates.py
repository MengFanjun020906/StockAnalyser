#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update the Sequoia-style candidate SQLite database with a rolling window.

The Agent candidate provider reads a local SQLite table:

    stock_daily(symbol, date, open, high, low, close, volume, turnover)

Parallelism note
----------------
baostock rejects concurrent logins from the same account, so fetching is
always done in a single baostock session (one login, sequential per-symbol
calls, one logout).  The primary speedup comes from auto-incremental mode.

Auto-incremental mode
---------------------
If the DB already has data within --incremental-threshold calendar days of
today, only the missing days are fetched.  For a daily refresh this cuts
the per-stock response from ~260 rows down to ~13 rows, reducing wall time
from 30+ minutes to roughly 10-15 minutes.

Resume behavior
---------------
Rows are committed after each symbol.  On restart, the script skips symbols
whose local max date already reaches the latest date seen in the DB unless
--no-resume is set, so a disconnected full run does not restart from symbol 0.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_DB_PATH = "Sequoia-X/data/sequoia_v2.db"
DEFAULT_TRADING_DAYS = 260
DEFAULT_LOOKBACK_CALENDAR_DAYS = 420
DEFAULT_INCREMENTAL_THRESHOLD = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 50

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""
CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Sequoia candidate daily-bar SQLite DB.")
    parser.add_argument("--db-path", default=os.getenv("SEQUOIA_CANDIDATE_DB_PATH", DEFAULT_DB_PATH))
    parser.add_argument("--trading-days", type=int, default=DEFAULT_TRADING_DAYS)
    parser.add_argument("--calendar-days", type=int, default=DEFAULT_LOOKBACK_CALENDAR_DAYS)
    parser.add_argument("--no-incremental", action="store_true",
                        help="Force full re-fetch even if DB is recent")
    parser.add_argument("--no-resume", action="store_true",
                        help="Do not skip symbols that already have the latest date in the local DB")
    parser.add_argument("--incremental-threshold", type=int, default=DEFAULT_INCREMENTAL_THRESHOLD,
                        help="Use incremental mode if DB is within N calendar days of today")
    parser.add_argument("--max-consecutive-failures", type=int, default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
                        help="Abort after N consecutive symbol fetch failures; 0 disables the guard")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated stock codes for smoke tests")
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=200,
                        help="Print progress every N symbols (default: 200)")
    return parser.parse_args()


def init_db(db_path: str) -> None:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_SQL)
        conn.commit()


def get_max_date_in_db(db_path: str) -> Optional[str]:
    # Use the minimum of per-symbol max dates so that any lagging symbol
    # triggers a backfill rather than the globally-latest symbol masking the gap.
    try:
        with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
            row = conn.execute(
                "SELECT MIN(max_date) FROM (SELECT MAX(date) AS max_date FROM stock_daily GROUP BY symbol)"
            ).fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def get_global_max_date_in_db(db_path: str) -> Optional[str]:
    try:
        with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def load_symbol_max_dates(db_path: str) -> Dict[str, str]:
    try:
        with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()
    except Exception:
        return {}
    return {str(symbol): str(max_date) for symbol, max_date in rows if symbol and max_date}


def filter_resume_symbols(
    symbols: Iterable[str],
    symbol_max_dates: Dict[str, str],
    resume_target_date: Optional[str],
) -> Tuple[List[str], int]:
    if not resume_target_date:
        return list(symbols), 0
    pending: List[str] = []
    skipped = 0
    for symbol in symbols:
        if symbol_max_dates.get(symbol, "") >= resume_target_date:
            skipped += 1
        else:
            pending.append(symbol)
    return pending, skipped


def to_baostock_code(symbol: str) -> str:
    return ("sh" if symbol.startswith(("6", "9")) else "sz") + "." + symbol


def get_all_symbols() -> List[str]:
    import baostock as bs
    rs = bs.query_stock_basic(code_name="", code="")
    symbols: List[str] = []
    while rs.next():
        row = rs.get_row_data()
        if len(row) < 6:
            continue
        if row[4] == "1" and row[5] == "1":
            symbols.append(row[0].split(".")[1])
    return symbols


def fetch_symbol_rows(symbol: str, start_date: str, end_date: str):
    import baostock as bs
    rs = bs.query_history_k_data_plus(
        to_baostock_code(symbol),
        "date,open,high,low,close,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="1",
    )
    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg)

    rows: List[Tuple] = []
    while rs.next():
        r = rs.get_row_data()
        try:
            close_v = float(r[4]) if r[4] else None
            vol_v = float(r[5]) if r[5] else None
            if close_v is None or (vol_v is not None and vol_v <= 0):
                continue
            rows.append((
                symbol, r[0],
                float(r[1]) if r[1] else None,
                float(r[2]) if r[2] else None,
                float(r[3]) if r[3] else None,
                close_v, vol_v,
                float(r[6]) if r[6] else None,
            ))
        except (ValueError, IndexError):
            continue
    return rows


def _to_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _normalize_daily_rows(rows: Any) -> Iterable[Tuple]:
    if rows is None:
        return []
    if hasattr(rows, "empty") and hasattr(rows, "itertuples"):
        if rows.empty:
            return []
        return (
            (
                str(row.symbol),
                str(row.date),
                _to_optional_float(row.open),
                _to_optional_float(row.high),
                _to_optional_float(row.low),
                _to_optional_float(row.close),
                _to_optional_float(row.volume),
                _to_optional_float(row.turnover),
            )
            for row in rows.itertuples(index=False)
        )
    return (
        (
            str(row["symbol"]),
            str(row["date"]),
            _to_optional_float(row.get("open")),
            _to_optional_float(row.get("high")),
            _to_optional_float(row.get("low")),
            _to_optional_float(row.get("close")),
            _to_optional_float(row.get("volume")),
            _to_optional_float(row.get("turnover")),
        )
        if isinstance(row, dict)
        else tuple(row)
        for row in rows
    )


def upsert_rows(db_path: str, rows: Any) -> int:
    normalized_rows = list(_normalize_daily_rows(rows))
    if not normalized_rows:
        return 0
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.executemany(
            """INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume, turnover=excluded.turnover""",
            normalized_rows,
        )
        conn.commit()
    return len(normalized_rows)


def prune_to_latest_trading_days(db_path: str, trading_days: int) -> Tuple[int, Optional[str]]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
            (max(1, trading_days),),
        ).fetchall()]
        if not dates:
            return 0, None
        cutoff = min(dates)
        cur = conn.execute("DELETE FROM stock_daily WHERE date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0, cutoff


def prune_symbol_row_limit(db_path: str, trading_days: int) -> int:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        cur = conn.execute(
            """DELETE FROM stock_daily WHERE rowid IN (
                 SELECT rowid FROM (
                   SELECT rowid,
                     ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                   FROM stock_daily)
                 WHERE rn > ?)""",
            (max(1, trading_days),),
        )
        conn.commit()
        return cur.rowcount or 0


def db_summary(db_path: str) -> Tuple[int, int, Optional[str], Optional[str]]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM stock_daily"
        ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), row[2], row[3]


def parse_symbol_arg(raw: str) -> List[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def main() -> int:
    args = parse_args()
    db_path = str(Path(args.db_path).expanduser())
    trading_days = max(1, args.trading_days)
    end = date.today()
    end_text = end.strftime("%Y-%m-%d")

    init_db(db_path)

    max_db_date = get_max_date_in_db(db_path)
    if (
        not args.no_incremental
        and max_db_date is not None
        and (end - date.fromisoformat(max_db_date)).days <= args.incremental_threshold
    ):
        start_text = max_db_date
        mode = f"incremental (DB max={max_db_date}, fetching {start_text}..{end_text})"
    else:
        start = end - timedelta(days=max(trading_days, args.calendar_days))
        start_text = start.strftime("%Y-%m-%d")
        mode = f"full ({start_text}..{end_text})"

    resume_target_date = get_global_max_date_in_db(db_path)
    if resume_target_date is not None:
        try:
            if (end - date.fromisoformat(resume_target_date)).days > args.incremental_threshold:
                resume_target_date = None
        except ValueError:
            resume_target_date = None

    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        print(f"baostock login failed: {login.error_msg}", file=sys.stderr)
        return 2

    try:
        symbols = parse_symbol_arg(args.symbols) or get_all_symbols()
        if args.limit_symbols > 0:
            symbols = symbols[: args.limit_symbols]

        skipped_complete = 0
        if not args.no_resume and resume_target_date:
            symbol_max_dates = load_symbol_max_dates(db_path)
            symbols, skipped_complete = filter_resume_symbols(symbols, symbol_max_dates, resume_target_date)

        print(
            f"Updating Sequoia candidate DB: db={db_path}, symbols={len(symbols)}, "
            f"skipped_complete={skipped_complete}, mode={mode}, resume_target={resume_target_date}",
            flush=True,
        )

        success = empty = failed = written_rows = 0
        aborted = False
        consecutive_failures = 0
        started = time.time()
        progress_every = max(1, args.progress_every)
        max_consecutive_failures = max(0, args.max_consecutive_failures)

        for idx, symbol in enumerate(symbols, start=1):
            try:
                rows = fetch_symbol_rows(symbol, start_text, end_text)
                if not rows:
                    empty += 1
                else:
                    written_rows += upsert_rows(db_path, rows)
                    success += 1
                consecutive_failures = 0
            except Exception as exc:
                failed += 1
                consecutive_failures += 1
                if failed <= 10:
                    print(f"[WARN] {symbol} failed: {exc}", flush=True)
                if max_consecutive_failures and consecutive_failures >= max_consecutive_failures:
                    aborted = True
                    print(
                        f"[ERROR] aborting after {consecutive_failures} consecutive failures; "
                        "written rows are kept, pruning is skipped so the DB remains resumable",
                        flush=True,
                    )
                    break

            if idx % progress_every == 0 or idx == len(symbols):
                elapsed = time.time() - started
                rate = idx / elapsed if elapsed > 0 else 0
                eta = (len(symbols) - idx) / rate if rate > 0 else 0
                print(
                    f"progress {idx}/{len(symbols)} "
                    f"success={success} empty={empty} failed={failed} "
                    f"written_rows={written_rows} elapsed={elapsed:.1f}s "
                    f"rate={rate:.1f}/s eta={eta:.0f}s",
                    flush=True,
                )
    finally:
        bs.logout()

    if aborted:
        deleted_by_date, cutoff, deleted_by_symbol = 0, None, 0
    else:
        deleted_by_date, cutoff = prune_to_latest_trading_days(db_path, trading_days)
        deleted_by_symbol = prune_symbol_row_limit(db_path, trading_days)
    total_rows, symbol_count, min_date, max_date = db_summary(db_path)
    elapsed_total = time.time() - started
    print(
        f"Done. success={success} empty={empty} failed={failed} skipped_complete={skipped_complete} "
        f"aborted={aborted} written_rows={written_rows} "
        f"pruned_by_date={deleted_by_date} pruned_by_symbol={deleted_by_symbol} "
        f"cutoff={cutoff} db_rows={total_rows} "
        f"symbols={symbol_count} date_range={min_date}..{max_date} "
        f"total_elapsed={elapsed_total:.1f}s",
        flush=True,
    )
    if aborted:
        return 4
    return 0 if success > 0 or (skipped_complete > 0 and failed == 0) else 3


if __name__ == "__main__":
    raise SystemExit(main())
