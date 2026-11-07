#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Update the Sequoia-style candidate SQLite database with a rolling window.

The Agent candidate provider reads a local SQLite table:

    stock_daily(symbol, date, open, high, low, close, volume, turnover)

Data sources
------------
The standalone CLI keeps baostock as its compatibility default.  The daily
pipeline explicitly selects Tushare, which downloads one full-market snapshot
per missing trading day plus adjustment factors instead of opening a baostock
session and querying every symbol.  When an existing baostock database is
migrated, overlap-day scales preserve its historical adjusted-price basis.

Baostock compatibility
----------------------
Baostock rejects concurrent logins from the same account, so that path remains
single-session and sequential.  Its auto-incremental and resume behavior is
kept for callers that still select ``--source baostock``.

Tushare completion metadata is advanced only after an unfiltered full-market
snapshot succeeds.  Partial smoke runs and index-only refreshes therefore
cannot make a later daily run skip missing stock data.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.trading_calendar import get_effective_trading_date

DEFAULT_DB_PATH = "Sequoia-X/data/sequoia_v2.db"
DEFAULT_TRADING_DAYS = 260
DEFAULT_LOOKBACK_CALENDAR_DAYS = 420
DEFAULT_INCREMENTAL_THRESHOLD = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 50
MIN_TUSHARE_SNAPSHOT_COVERAGE_RATIO = 0.8
BENCHMARK_INDEX_SYMBOL = "000001.SH"
BENCHMARK_INDEX_BAOSTOCK_CODE = "sh.000001"
REGIME_INDEX_SYMBOL = "000300.SH"
REGIME_INDEX_BAOSTOCK_CODE = "sh.000300"
INDEX_SYMBOLS = (
    (BENCHMARK_INDEX_SYMBOL, BENCHMARK_INDEX_BAOSTOCK_CODE),
    (REGIME_INDEX_SYMBOL, REGIME_INDEX_BAOSTOCK_CODE),
)
TUSHARE_INDEX_SYMBOLS = (
    (BENCHMARK_INDEX_SYMBOL, "000001.SH"),
    (REGIME_INDEX_SYMBOL, "000300.SH"),
)
BAOSTOCK_SESSION_EXPIRED_MARKERS = (
    "用户未登录",
    "not login",
    "not logged in",
    "please login",
)

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
CREATE_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
CREATE_SOURCE_SCALE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily_source_scale (
    source      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    scale       REAL NOT NULL,
    anchor_date TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, symbol)
);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Sequoia candidate daily-bar SQLite DB.")
    parser.add_argument(
        "--source",
        choices=("baostock", "tushare"),
        default="baostock",
        help="Daily-bar source (default: baostock)",
    )
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
    parser.add_argument("--skip-benchmark-index", action="store_true",
                        help="Skip fetching local index rows (default fetches 000001.SH and 000300.SH)")
    parser.add_argument("--index-only", action="store_true",
                        help="Only fetch local index rows, without updating stock symbols")
    return parser.parse_args()


def resolve_cn_resume_target_date(current_time: Optional[datetime] = None) -> date:
    """Return the latest reusable A-share daily-bar date."""
    return get_effective_trading_date("cn", current_time=current_time)


def init_db(db_path: str) -> None:
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_SQL)
        conn.execute(CREATE_METADATA_SQL)
        conn.execute(CREATE_SOURCE_SCALE_SQL)
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


def is_baostock_session_expired_error(error: Any) -> bool:
    """Return True when baostock reports a dropped login session."""
    text = str(error or "").strip().lower()
    return any(marker.lower() in text for marker in BAOSTOCK_SESSION_EXPIRED_MARKERS)


def refresh_baostock_login(bs: Any) -> Tuple[bool, str]:
    """Best-effort baostock relogin used when a long update loses session state."""
    try:
        bs.logout()
    except Exception:
        pass
    login = bs.login()
    if getattr(login, "error_code", "") == "0":
        return True, ""
    return False, str(getattr(login, "error_msg", "") or "unknown baostock login error")


def fetch_index_rows(
    *,
    symbol: str = BENCHMARK_INDEX_SYMBOL,
    baostock_code: str = BENCHMARK_INDEX_BAOSTOCK_CODE,
    start_date: str,
    end_date: str,
) -> List[Tuple]:
    """Fetch benchmark index daily bars and store them under an unambiguous symbol.

    上证指数和深市个股平安银行都会显示成 000001；SQLite 里必须保存为
    000001.SH，避免覆盖个股 000001 的日线。
    """
    import baostock as bs

    rs = bs.query_history_k_data_plus(
        baostock_code,
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
            if close_v is None:
                continue
            rows.append((
                symbol, r[0],
                float(r[1]) if r[1] else None,
                float(r[2]) if r[2] else None,
                float(r[3]) if r[3] else None,
                close_v,
                float(r[5]) if r[5] else None,
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


def _format_tushare_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    raise ValueError(f"invalid Tushare trade_date: {value!r}")


def _tushare_number(value: Any, *, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    return float(value)


def _normalize_tushare_stock_rows(
    daily_df: Any,
    factor_df: Any,
    *,
    include_zero_volume: bool = False,
) -> Tuple[List[Tuple], List[str]]:
    if daily_df is None or getattr(daily_df, "empty", True):
        return [], []

    factor_by_key: Dict[Tuple[str, str], float] = {}
    if factor_df is not None and not getattr(factor_df, "empty", True):
        for item in factor_df.to_dict("records"):
            code = str(item.get("ts_code") or "").strip().upper()
            trade_date = _format_tushare_date(item.get("trade_date"))
            factor = _tushare_number(item.get("adj_factor"))
            if code and factor is not None and factor > 0:
                factor_by_key[(code, trade_date)] = factor

    rows: List[Tuple] = []
    missing_factors: List[str] = []
    for item in daily_df.to_dict("records"):
        ts_code = str(item.get("ts_code") or "").strip().upper()
        trade_date = _format_tushare_date(item.get("trade_date"))
        factor = factor_by_key.get((ts_code, trade_date))
        if factor is None:
            missing_factors.append(f"{ts_code}@{trade_date}")
            continue

        close_value = _tushare_number(item.get("close"))
        volume_lots = _tushare_number(item.get("vol"), default=0.0)
        if not ts_code or close_value is None:
            continue
        if not include_zero_volume and volume_lots is not None and volume_lots <= 0:
            continue

        symbol = ts_code.split(".", 1)[0]
        rows.append(
            (
                symbol,
                trade_date,
                _tushare_number(item.get("open")) * factor if item.get("open") not in (None, "") else None,
                _tushare_number(item.get("high")) * factor if item.get("high") not in (None, "") else None,
                _tushare_number(item.get("low")) * factor if item.get("low") not in (None, "") else None,
                close_value * factor,
                volume_lots * 100.0 if volume_lots is not None else None,
                (_tushare_number(item.get("amount"), default=0.0) or 0.0) * 1000.0,
            )
        )
    return rows, missing_factors


def _normalize_tushare_index_rows(index_df: Any, symbol: str) -> List[Tuple]:
    if index_df is None or getattr(index_df, "empty", True):
        return []
    rows: List[Tuple] = []
    for item in index_df.to_dict("records"):
        close_value = _tushare_number(item.get("close"))
        volume_lots = _tushare_number(item.get("vol"), default=0.0)
        if close_value is None:
            continue
        rows.append(
            (
                symbol,
                _format_tushare_date(item.get("trade_date")),
                _tushare_number(item.get("open")),
                _tushare_number(item.get("high")),
                _tushare_number(item.get("low")),
                close_value,
                volume_lots * 100.0 if volume_lots is not None else None,
                (_tushare_number(item.get("amount"), default=0.0) or 0.0) * 1000.0,
            )
        )
    return rows


def _load_metadata(db_path: str) -> Dict[str, str]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        rows = conn.execute("SELECT key, value FROM stock_daily_metadata").fetchall()
    return {str(key): str(value) for key, value in rows}


def _load_tushare_scales(db_path: str) -> Dict[str, Tuple[float, str]]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        rows = conn.execute(
            "SELECT symbol, scale, anchor_date FROM stock_daily_source_scale WHERE source = 'tushare'"
        ).fetchall()
    return {str(symbol): (float(scale), str(anchor_date)) for symbol, scale, anchor_date in rows}


def _load_latest_symbol_rows(db_path: str) -> Dict[str, Tuple[str, float]]:
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        rows = conn.execute(
            """
            SELECT current.symbol, current.date, current.close
            FROM stock_daily AS current
            JOIN (
                SELECT symbol, MAX(date) AS max_date
                FROM stock_daily
                WHERE symbol NOT LIKE '%.%'
                GROUP BY symbol
            ) AS latest
              ON latest.symbol = current.symbol AND latest.max_date = current.date
            WHERE current.close IS NOT NULL
            """
        ).fetchall()
    return {str(symbol): (str(trade_date), float(close)) for symbol, trade_date, close in rows}


def _load_symbol_history(
    db_path: str,
    symbols: Iterable[str],
) -> Dict[str, List[Tuple[str, float]]]:
    """Load local close history used to find a source overlap for a symbol."""
    requested = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
    if not requested:
        return {}

    history: Dict[str, List[Tuple[str, float]]] = {}
    # Keep each IN clause below SQLite's default host-parameter limit.
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        for offset in range(0, len(requested), 900):
            batch = requested[offset : offset + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT symbol, date, close
                FROM stock_daily
                WHERE symbol IN ({placeholders})
                  AND symbol NOT LIKE '%.%'
                  AND close IS NOT NULL
                ORDER BY symbol, date DESC
                """,
                batch,
            ).fetchall()
            for symbol, trade_date, close in rows:
                history.setdefault(str(symbol), []).append((str(trade_date), float(close)))
    return history


def _to_tushare_stock_code(symbol: str) -> str:
    """Convert a local six-digit A-share code to a Tushare ``ts_code``."""
    normalized = str(symbol or "").strip().upper()
    if normalized.startswith(("6", "9")):
        exchange = "SH"
    elif normalized.startswith(("4", "8")):
        exchange = "BJ"
    else:
        exchange = "SZ"
    return f"{normalized}.{exchange}"


def _apply_price_scales(rows: Iterable[Tuple], scales: Dict[str, Tuple[float, str]]) -> List[Tuple]:
    scaled_rows: List[Tuple] = []
    for row in rows:
        symbol = str(row[0])
        scale = scales[symbol][0]
        scaled_rows.append(
            (
                symbol,
                row[1],
                row[2] * scale if row[2] is not None else None,
                row[3] * scale if row[3] is not None else None,
                row[4] * scale if row[4] is not None else None,
                row[5] * scale if row[5] is not None else None,
                row[6],
                row[7],
            )
        )
    return scaled_rows


def _save_tushare_batch(
    db_path: str,
    rows: Iterable[Tuple],
    scales: Dict[str, Tuple[float, str]],
    completed_date: str,
    *,
    mark_complete: bool,
) -> int:
    normalized_rows = list(rows)
    with sqlite3.connect(str(Path(db_path).expanduser())) as conn:
        conn.executemany(
            """INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume, turnover=excluded.turnover""",
            normalized_rows,
        )
        conn.executemany(
            """INSERT INTO stock_daily_source_scale(source,symbol,scale,anchor_date,updated_at)
               VALUES('tushare',?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(source,symbol) DO UPDATE SET
                 scale=excluded.scale,
                 anchor_date=excluded.anchor_date,
                 updated_at=CURRENT_TIMESTAMP""",
            [(symbol, scale, anchor_date) for symbol, (scale, anchor_date) in scales.items()],
        )
        if mark_complete:
            conn.executemany(
                """INSERT INTO stock_daily_metadata(key,value,updated_at)
                   VALUES(?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    ("active_source", "tushare"),
                    ("price_basis", "legacy_hfq_bridge"),
                    ("last_completed_date", completed_date),
                ),
            )
        conn.commit()
    return len(normalized_rows)


def _run_tushare_update(args: argparse.Namespace, db_path: str, end: date) -> int:
    from data_provider.tushare_client import get_tushare_token, query_tushare_api

    if not get_tushare_token():
        print("TUSHARE_TOKEN is not configured", file=sys.stderr)
        return 2

    end_text = end.strftime("%Y-%m-%d")
    end_compact = end.strftime("%Y%m%d")
    metadata = _load_metadata(db_path)
    latest_local = _load_latest_symbol_rows(db_path)
    scales = _load_tushare_scales(db_path)
    completed_date = metadata.get("last_completed_date") if metadata.get("active_source") == "tushare" else None
    legacy_stock_date = max((item[0] for item in latest_local.values()), default=None)
    resume_date = completed_date or (legacy_stock_date if latest_local and not scales else None)
    rolling_start = (end - timedelta(days=max(args.trading_days, args.calendar_days))).strftime("%Y-%m-%d")
    query_start = rolling_start if args.no_incremental or args.no_resume or not resume_date else resume_date
    query_start_compact = query_start.replace("-", "")

    print(
        f"Updating Sequoia candidate DB: source=tushare db={db_path}, "
        f"range={query_start}..{end_text} resume_date={resume_date}",
        flush=True,
    )

    index_rows_by_symbol: Dict[str, List[Tuple]] = {}
    for index_symbol, ts_code in TUSHARE_INDEX_SYMBOLS:
        try:
            frame = query_tushare_api(
                "index_daily",
                params={
                    "ts_code": ts_code,
                    "start_date": query_start_compact,
                    "end_date": end_compact,
                },
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
                timeout=60,
            )
        except Exception as exc:
            print(f"Tushare index_daily failed for {ts_code}: {exc}", file=sys.stderr)
            return 5
        rows = _normalize_tushare_index_rows(frame, index_symbol)
        if not rows:
            print(f"Tushare index_daily returned no rows for {ts_code}", file=sys.stderr)
            return 5
        index_rows_by_symbol[index_symbol] = rows

    benchmark_rows = index_rows_by_symbol[BENCHMARK_INDEX_SYMBOL]
    available_dates = sorted({str(row[1]) for row in benchmark_rows if str(row[1]) <= end_text})
    if not available_dates:
        print("Tushare returned no completed trading dates", file=sys.stderr)
        return 3
    if available_dates[-1] != end_text:
        print(
            f"Tushare index data is not ready for target date {end_text}; latest={available_dates[-1]}",
            file=sys.stderr,
        )
        return 3

    force_rolling_refresh = args.no_incremental or args.no_resume or not resume_date
    if force_rolling_refresh:
        dates_to_fetch = available_dates[-max(1, args.trading_days):]
        anchor_date = None
    else:
        anchor_candidates = [item for item in available_dates if item <= resume_date]
        anchor_date = anchor_candidates[-1] if anchor_candidates else None
        dates_to_fetch = ([anchor_date] if anchor_date else []) + [
            item for item in available_dates if item > resume_date
        ]

    if args.index_only:
        dates_to_fetch = []

    selected_symbols = set(parse_symbol_arg(args.symbols))
    snapshots: Dict[str, List[Tuple]] = {}
    raw_snapshots: Dict[str, Tuple[List[Tuple], List[str]]] = {}

    def fetch_stock_snapshot(trade_date: str) -> Tuple[List[Tuple], List[str]]:
        cached = raw_snapshots.get(trade_date)
        if cached is not None:
            return cached
        compact_date = trade_date.replace("-", "")
        daily_frame = query_tushare_api(
            "daily",
            params={"trade_date": compact_date},
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
            timeout=60,
        )
        factor_frame = query_tushare_api(
            "adj_factor",
            params={"trade_date": compact_date},
            fields="ts_code,trade_date,adj_factor",
            timeout=60,
        )
        rows, missing_factors = _normalize_tushare_stock_rows(daily_frame, factor_frame)
        raw_snapshots[trade_date] = (rows, missing_factors)
        return rows, missing_factors

    targeted_snapshots: Dict[str, List[Tuple]] = {}

    def fetch_targeted_stock_history(
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> List[Tuple]:
        """Fetch one symbol when a full-market snapshot omitted its overlap."""
        cached = targeted_snapshots.get(symbol)
        if cached is not None:
            return cached
        params = {
            "ts_code": _to_tushare_stock_code(symbol),
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
        }
        daily_frame = query_tushare_api(
            "daily",
            params=params,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
            timeout=60,
        )
        factor_frame = query_tushare_api(
            "adj_factor",
            params=params,
            fields="ts_code,trade_date,adj_factor",
            timeout=60,
        )
        rows, _missing_factors = _normalize_tushare_stock_rows(
            daily_frame,
            factor_frame,
            # A zero-volume bar can still provide the close needed to bridge
            # an existing adjusted-price basis (for example, a suspension).
            include_zero_volume=True,
        )
        targeted_snapshots[symbol] = rows
        return rows

    missing_factor_count = 0
    for trade_date in dates_to_fetch:
        try:
            rows, missing_factors = fetch_stock_snapshot(trade_date)
        except Exception as exc:
            print(f"Tushare daily snapshot failed for {trade_date}: {exc}", file=sys.stderr)
            return 3
        if selected_symbols:
            rows = [row for row in rows if row[0] in selected_symbols]
        if args.limit_symbols > 0:
            allowed = {row[0] for row in rows[: args.limit_symbols]}
            rows = [row for row in rows if row[0] in allowed]
        if not rows:
            print(f"Tushare daily snapshot returned no rows for {trade_date}", file=sys.stderr)
            return 3
        snapshots[trade_date] = rows
        missing_factor_count += len(missing_factors)

    if missing_factor_count:
        print(f"Tushare snapshots missing {missing_factor_count} adjustment factors", file=sys.stderr)
        return 3
    if not selected_symbols and args.limit_symbols <= 0 and len(dates_to_fetch) > 1:
        previous_count = len(snapshots[dates_to_fetch[0]])
        for trade_date in dates_to_fetch[1:]:
            current_count = len(snapshots[trade_date])
            minimum_count = max(1, int(previous_count * MIN_TUSHARE_SNAPSHOT_COVERAGE_RATIO))
            if current_count < minimum_count:
                print(
                    f"Tushare daily snapshot coverage is incomplete for {trade_date}: "
                    f"rows={current_count}, expected_at_least={minimum_count}",
                    file=sys.stderr,
                )
                return 3
            previous_count = current_count

    if anchor_date and anchor_date in snapshots:
        for row in snapshots[anchor_date]:
            symbol = str(row[0])
            local = latest_local.get(symbol)
            source_close = float(row[5]) if row[5] is not None else 0.0
            if local and local[0] == anchor_date and source_close > 0:
                scales[symbol] = (local[1] / source_close, anchor_date)

    write_dates = dates_to_fetch if force_rolling_refresh else [item for item in dates_to_fetch if item != anchor_date]
    missing_scale_symbols = {
        str(row[0])
        for trade_date in write_dates
        for row in snapshots.get(trade_date, [])
        if str(row[0]) not in scales and str(row[0]) in latest_local
    }
    fallback_dates: Dict[str, List[str]] = {}
    for symbol in missing_scale_symbols:
        fallback_dates.setdefault(latest_local[symbol][0], []).append(symbol)

    for fallback_date, symbols in fallback_dates.items():
        try:
            fallback_rows, fallback_missing_factors = fetch_stock_snapshot(fallback_date)
        except Exception as exc:
            print(f"Tushare source bridge failed for {fallback_date}: {exc}", file=sys.stderr)
            return 3
        if fallback_missing_factors:
            print(
                f"Tushare source bridge missing adjustment factors for {fallback_date}",
                file=sys.stderr,
            )
            return 3
        fallback_by_symbol = {str(row[0]): row for row in fallback_rows}
        for symbol in symbols:
            source_row = fallback_by_symbol.get(symbol)
            if source_row is None or source_row[5] in (None, 0):
                continue
            scales[symbol] = (latest_local[symbol][1] / float(source_row[5]), fallback_date)

    # A full-market endpoint can omit an otherwise valid symbol because of
    # pagination, suspension, or source-side filtering.  Recover only the
    # symbols still missing a scale with a bounded per-symbol history query.
    bridge_candidates = missing_scale_symbols.difference(scales)
    if bridge_candidates:
        local_history = _load_symbol_history(db_path, bridge_candidates)
        for symbol in sorted(bridge_candidates):
            history = local_history.get(symbol, [])
            if not history:
                continue
            try:
                targeted_rows = fetch_targeted_stock_history(
                    symbol,
                    min(trade_date for trade_date, _close in history),
                    max(trade_date for trade_date, _close in history),
                )
            except Exception as exc:
                print(f"Tushare targeted source bridge failed for {symbol}: {exc}", file=sys.stderr)
                continue
            source_by_date = {
                str(row[1]): row
                for row in targeted_rows
                if row[5] not in (None, 0)
            }
            for local_date, local_close in history:
                source_row = source_by_date.get(local_date)
                if source_row is None:
                    continue
                scales[symbol] = (local_close / float(source_row[5]), local_date)
                print(
                    f"Tushare targeted source bridge recovered {symbol} using overlap={local_date}",
                    flush=True,
                )
                break

    rows_to_write: List[Tuple] = []
    unresolved: List[str] = []
    for trade_date in write_dates:
        for row in snapshots.get(trade_date, []):
            symbol = str(row[0])
            if symbol not in scales:
                if symbol in latest_local:
                    unresolved.append(symbol)
                    continue
                scales[symbol] = (1.0, trade_date)
            rows_to_write.extend(_apply_price_scales((row,), scales))

    if unresolved:
        examples = ",".join(sorted(set(unresolved))[:10])
        print(
            f"Tushare source bridge is missing overlap for {len(set(unresolved))} existing symbols: {examples}",
            file=sys.stderr,
        )
        return 3

    if not args.skip_benchmark_index:
        for rows in index_rows_by_symbol.values():
            rows_to_write.extend(
                row for row in rows if force_rolling_refresh or resume_date is None or str(row[1]) > resume_date
            )

    final_date = available_dates[-1]
    mark_complete = not args.index_only and not selected_symbols and args.limit_symbols <= 0
    written_rows = _save_tushare_batch(
        db_path,
        rows_to_write,
        scales,
        final_date,
        mark_complete=mark_complete,
    )
    deleted_by_date, cutoff = prune_to_latest_trading_days(db_path, max(1, args.trading_days))
    deleted_by_symbol = prune_symbol_row_limit(db_path, max(1, args.trading_days))
    total_rows, symbol_count, min_date, max_date = db_summary(db_path)
    print(
        f"Done. source=tushare written_rows={written_rows} scales={len(scales)} "
        f"pruned_by_date={deleted_by_date} pruned_by_symbol={deleted_by_symbol} cutoff={cutoff} "
        f"db_rows={total_rows} symbols={symbol_count} date_range={min_date}..{max_date}",
        flush=True,
    )
    return 0


def main() -> int:
    args = parse_args()
    db_path = str(Path(args.db_path).expanduser())
    trading_days = max(1, args.trading_days)
    end = resolve_cn_resume_target_date()
    end_text = end.strftime("%Y-%m-%d")

    init_db(db_path)

    if args.source == "tushare":
        return _run_tushare_update(args, db_path, end)

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

    # Resume target is the latest completed A-share session, not the natural
    # date. Weekend/holiday runs cannot write bars for "today" and must still
    # be considered complete once symbols reach the previous trading day.
    # It is also not the DB's current max date, because that caused all symbols
    # to be skipped on re-runs of the same day.
    resume_target_date = end_text

    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        print(f"baostock login failed: {login.error_msg}", file=sys.stderr)
        return 2

    try:
        symbols = [] if args.index_only else (parse_symbol_arg(args.symbols) or get_all_symbols())
        if args.limit_symbols > 0:
            symbols = symbols[: args.limit_symbols]

        skipped_complete = 0
        if not args.no_resume and resume_target_date:
            symbol_max_dates = load_symbol_max_dates(db_path)
            symbols, skipped_complete = filter_resume_symbols(symbols, symbol_max_dates, resume_target_date)

        print(
            f"Updating Sequoia candidate DB: db={db_path}, symbols={len(symbols)}, "
            f"skipped_complete={skipped_complete}, mode={mode}, resume_target={resume_target_date} "
            f"index_only={args.index_only}",
            flush=True,
        )

        success = empty = failed = written_rows = 0
        index_written_rows = 0
        index_status = "skipped" if args.skip_benchmark_index else "pending"
        aborted = False
        consecutive_failures = 0
        relogin_count = 0
        started = time.time()
        progress_every = max(1, args.progress_every)
        max_consecutive_failures = max(0, args.max_consecutive_failures)

        for idx, symbol in enumerate(symbols, start=1):
            session_retry_used = False
            try:
                while True:
                    try:
                        rows = fetch_symbol_rows(symbol, start_text, end_text)
                        break
                    except Exception as exc:
                        if session_retry_used or not is_baostock_session_expired_error(exc):
                            raise
                        session_retry_used = True
                        relogin_count += 1
                        print(
                            f"[WARN] baostock session expired while fetching {symbol}: {exc}; "
                            "relogin and retry once",
                            flush=True,
                        )
                        ok, relogin_error = refresh_baostock_login(bs)
                        if not ok:
                            raise RuntimeError(f"baostock relogin failed after session expired: {relogin_error}") from exc
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
                    f"written_rows={written_rows} relogin={relogin_count} elapsed={elapsed:.1f}s "
                    f"rate={rate:.1f}/s eta={eta:.0f}s",
                    flush=True,
                )

        if not aborted and not args.skip_benchmark_index:
            index_statuses = []
            for index_symbol, baostock_code in INDEX_SYMBOLS:
                try:
                    index_rows = fetch_index_rows(
                        symbol=index_symbol,
                        baostock_code=baostock_code,
                        start_date=start_text,
                        end_date=end_text,
                    )
                    written = upsert_rows(db_path, index_rows)
                    status = "ok" if written > 0 else "empty"
                    index_statuses.append(status)
                    index_written_rows += written
                    print(
                        f"benchmark_index {index_symbol} status={status} written_rows={written}",
                        flush=True,
                    )
                except Exception as exc:
                    index_statuses.append("failed")
                    failed += 1
                    print(f"[WARN] benchmark_index {index_symbol} failed: {exc}", flush=True)
            index_status = "ok" if index_statuses and all(item == "ok" for item in index_statuses) else "partial"
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
        f"baostock_relogin={relogin_count} "
        f"benchmark_indexes={','.join(symbol for symbol, _code in INDEX_SYMBOLS)} index_status={index_status} "
        f"index_written_rows={index_written_rows} "
        f"pruned_by_date={deleted_by_date} pruned_by_symbol={deleted_by_symbol} "
        f"cutoff={cutoff} db_rows={total_rows} "
        f"symbols={symbol_count} date_range={min_date}..{max_date} "
        f"total_elapsed={elapsed_total:.1f}s",
        flush=True,
    )
    if aborted:
        return 4
    if index_status == "failed":
        return 5
    return 0 if success > 0 or index_written_rows > 0 or (skipped_complete > 0 and failed == 0) else 3


if __name__ == "__main__":
    raise SystemExit(main())
