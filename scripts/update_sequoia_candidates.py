#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update the Sequoia-style candidate SQLite database with a rolling window.

The Agent candidate provider reads a local SQLite table:

    stock_daily(symbol, date, open, high, low, close, volume, turnover)

This script fills that table from baostock without running the full Sequoia-X
backfill.  It defaults to roughly the latest 260 trading days, which covers the
longest current Sequoia-style strategy window (120 days) with enough buffer for
RPS and breakout calculations.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import pandas as pd

DEFAULT_DB_PATH = "Sequoia-X/data/sequoia_v2.db"
DEFAULT_TRADING_DAYS = 260
DEFAULT_LOOKBACK_CALENDAR_DAYS = 420

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
    parser.add_argument(
        "--db-path",
        default=os.getenv("SEQUOIA_CANDIDATE_DB_PATH", DEFAULT_DB_PATH),
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--trading-days",
        type=int,
        default=DEFAULT_TRADING_DAYS,
        help=f"Rows per symbol to keep after update (default: {DEFAULT_TRADING_DAYS})",
    )
    parser.add_argument(
        "--calendar-days",
        type=int,
        default=DEFAULT_LOOKBACK_CALENDAR_DAYS,
        help=f"Calendar-day lookback fetched from baostock (default: {DEFAULT_LOOKBACK_CALENDAR_DAYS})",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated stock codes for smoke tests. Omit for all A-share stocks.",
    )
    parser.add_argument(
        "--limit-symbols",
        type=int,
        default=0,
        help="Optional max number of symbols to update, useful for smoke tests.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=200,
        help="Print progress every N symbols (default: 200).",
    )
    return parser.parse_args()


def init_db(db_path: str) -> None:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_SQL)
        conn.commit()


def to_baostock_code(symbol: str) -> str:
    prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
    return f"{prefix}.{symbol}"


def get_all_symbols() -> List[str]:
    import baostock as bs

    rs = bs.query_stock_basic(code_name="", code="")
    symbols: List[str] = []
    while rs.next():
        row = rs.get_row_data()
        if len(row) < 6:
            continue
        code = row[0]
        status = row[4]
        stock_type = row[5]
        if status == "1" and stock_type == "1":
            symbols.append(code.split(".")[1])
    return symbols


def fetch_symbol_rows(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
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

    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])

    df = pd.DataFrame(rows, columns=rs.fields)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["volume"].fillna(0) > 0]
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
    df["symbol"] = symbol
    df = df.rename(columns={"amount": "turnover"})
    return df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]


def upsert_rows(db_path: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (
            str(row.symbol),
            str(row.date),
            float(row.open) if pd.notna(row.open) else None,
            float(row.high) if pd.notna(row.high) else None,
            float(row.low) if pd.notna(row.low) else None,
            float(row.close) if pd.notna(row.close) else None,
            float(row.volume) if pd.notna(row.volume) else None,
            float(row.turnover) if pd.notna(row.turnover) else None,
        )
        for row in df.itertuples(index=False)
    ]
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.executemany(
            """
            INSERT INTO stock_daily(symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                turnover=excluded.turnover
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def prune_to_latest_trading_days(db_path: str, trading_days: int) -> Tuple[int, str | None]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT ?",
                (max(1, trading_days),),
            ).fetchall()
        ]
        if not dates:
            return 0, None
        cutoff = min(dates)
        cur = conn.execute("DELETE FROM stock_daily WHERE date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0, cutoff


def prune_symbol_row_limit(db_path: str, trading_days: int) -> int:
    """Keep at most N latest daily rows for each symbol.

    The global date cutoff normally guarantees this already because
    (symbol, date) is unique, but the per-symbol guard keeps the DB compact even
    if a stale or externally-created cache contains irregular dates.
    """

    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        cur = conn.execute(
            """
            DELETE FROM stock_daily
            WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT
                        rowid,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                    FROM stock_daily
                )
                WHERE rn > ?
            )
            """,
            (max(1, trading_days),),
        )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0


def db_summary(db_path: str) -> Tuple[int, int, str | None, str | None]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM stock_daily"
        ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), row[2], row[3]


def parse_symbol_arg(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    args = parse_args()
    db_path = str(Path(args.db_path).expanduser())
    trading_days = max(1, int(args.trading_days))
    calendar_days = max(trading_days, int(args.calendar_days))
    end = date.today()
    start = end - timedelta(days=calendar_days)
    start_text = start.strftime("%Y-%m-%d")
    end_text = end.strftime("%Y-%m-%d")

    init_db(db_path)

    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        print(f"baostock login failed: {login.error_msg}", file=sys.stderr)
        return 2

    try:
        symbols = parse_symbol_arg(args.symbols) or get_all_symbols()
        if args.limit_symbols and args.limit_symbols > 0:
            symbols = symbols[: args.limit_symbols]
        print(
            f"Updating Sequoia candidate DB: db={db_path}, symbols={len(symbols)}, "
            f"range={start_text}..{end_text}, keep_trading_days={trading_days}",
            flush=True,
        )

        success = 0
        empty = 0
        failed = 0
        written_rows = 0
        started = time.time()
        progress_every = max(1, int(args.progress_every or 200))
        for idx, symbol in enumerate(symbols, start=1):
            try:
                df = fetch_symbol_rows(symbol, start_text, end_text)
                if df.empty:
                    empty += 1
                else:
                    written_rows += upsert_rows(db_path, df)
                    success += 1
            except Exception as exc:
                failed += 1
                if failed <= 20:
                    print(f"[WARN] {symbol} failed: {exc}", flush=True)

            if idx % progress_every == 0 or idx == len(symbols):
                elapsed = time.time() - started
                print(
                    f"progress {idx}/{len(symbols)} success={success} empty={empty} "
                    f"failed={failed} written_rows={written_rows} elapsed={elapsed:.1f}s",
                    flush=True,
                )

        deleted_by_date, cutoff = prune_to_latest_trading_days(db_path, trading_days)
        deleted_by_symbol = prune_symbol_row_limit(db_path, trading_days)
        total_rows, symbol_count, min_date, max_date = db_summary(db_path)
        print(
            f"Done. success={success} empty={empty} failed={failed} written_rows={written_rows} "
            f"pruned_by_date={deleted_by_date} pruned_by_symbol={deleted_by_symbol} "
            f"cutoff={cutoff} db_rows={total_rows} "
            f"symbols={symbol_count} date_range={min_date}..{max_date}",
            flush=True,
        )
        return 0 if success > 0 else 3
    finally:
        bs.logout()


if __name__ == "__main__":
    raise SystemExit(main())
