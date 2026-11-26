# -*- coding: utf-8 -*-
"""Isolated SQLite ledger for one paper-trading book (A or B).

Each book owns its own database file so that the two ledgers can never
contaminate each other.  The schema is intentionally independent from the
real ``portfolio_*`` tables: nothing here reads or writes production
positions.

Two concerns live here:

* ``paper_ingest`` — the normalized artifact feed.  ``(source, run_id)`` is
  the primary key, which makes re-running the same artifact a no-op.
* ``paper_plans`` / ``paper_trades`` / ``paper_positions`` / ``paper_nav`` /
  ``paper_decisions`` — the simulated account state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_DB_DIRNAME = "paper_trading"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    account_id INTEGER PRIMARY KEY CHECK (account_id = 1),
    book TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_ingest (
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    artifact_path TEXT,
    ingested_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (source, run_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_ingest_date ON paper_ingest(decision_date);

CREATE TABLE IF NOT EXISTS paper_plans (
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    run_id TEXT,
    source TEXT,
    plan_id TEXT,
    scenario_id TEXT,
    generation_status TEXT DEFAULT 'generated',
    entry_low REAL,
    entry_high REAL,
    stop_loss REAL,
    target_1 REAL,
    target_2 REAL,
    target_1_exit_ratio REAL,
    valid_from TEXT,
    valid_until TEXT,
    ai_action TEXT,
    ai_position_pct REAL,
    engine_position_pct REAL,
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    PRIMARY KEY (as_of, symbol)
);

CREATE INDEX IF NOT EXISTS idx_paper_plans_window ON paper_plans(status, valid_from, valid_until);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL DEFAULT 0,
    amount REAL DEFAULT 0,
    reason TEXT,
    decision_id TEXT,
    run_id TEXT,
    source TEXT,
    close_price REAL,
    close_in_zone INTEGER
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_date ON paper_trades(as_of);

CREATE TABLE IF NOT EXISTS paper_positions (
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    available_quantity INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    last_price REAL,
    stop_loss REAL,
    target_1 REAL,
    target_2 REAL,
    target_1_exit_ratio REAL,
    target_1_done INTEGER DEFAULT 0,
    decision_id TEXT,
    PRIMARY KEY (as_of, symbol)
);

CREATE TABLE IF NOT EXISTS paper_nav (
    as_of TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    equity REAL NOT NULL,
    benchmark_close REAL,
    benchmark_nav REAL,
    positions_count INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_decisions (
    decision_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    source TEXT,
    run_id TEXT,
    entry_as_of TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    entry_fees REAL DEFAULT 0,
    status TEXT NOT NULL,
    exit_as_of TEXT,
    exit_price REAL,
    realized_return REAL,
    t5_date TEXT,
    t5_return REAL,
    t20_date TEXT,
    t20_return REAL,
    close_in_zone INTEGER,
    created_at TEXT,
    updated_at TEXT
);
"""


class PaperTradingStore:
    """Ledger for a single book (``A`` = strict chain, ``B`` = price-first)."""

    def __init__(self, db_path: str | Path, book: str = "A"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.book = str(book or "A").upper()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------
    def ensure_account(self, initial_cash: float) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT initial_cash FROM paper_account WHERE account_id = 1"
            ).fetchone()
            if row is not None:
                return float(row["initial_cash"])
            conn.execute(
                "INSERT INTO paper_account(account_id, book, initial_cash, created_at) "
                "VALUES (1, ?, ?, datetime('now'))",
                (self.book, float(initial_cash)),
            )
            return float(initial_cash)

    def initial_cash(self) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT initial_cash FROM paper_account WHERE account_id = 1"
            ).fetchone()
        return float(row["initial_cash"]) if row else None

    # ------------------------------------------------------------------
    # artifact feed
    # ------------------------------------------------------------------
    def record_ingest(self, record: Dict[str, Any]) -> bool:
        """Insert one artifact; returns ``False`` when the run was already booked."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO paper_ingest("
                "source, run_id, decision_date, artifact_path, ingested_at, payload_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record["source"],
                    record["run_id"],
                    record["decision_date"],
                    record.get("artifact_path") or "",
                    record.get("ingested_at") or "",
                    json.dumps(record.get("payload") or {}, ensure_ascii=False),
                ),
            )
            return cursor.rowcount > 0

    def ingests(self, on_or_before: Optional[date] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM paper_ingest"
        params: Sequence[Any] = ()
        if on_or_before is not None:
            query += " WHERE decision_date <= ?"
            params = (on_or_before.isoformat(),)
        query += " ORDER BY decision_date ASC, ingested_at ASC, run_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "source": row["source"],
                "run_id": row["run_id"],
                "decision_date": row["decision_date"],
                "artifact_path": row["artifact_path"],
                "ingested_at": row["ingested_at"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    def has_ingest(self, source: str, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM paper_ingest WHERE source = ? AND run_id = ?",
                (source, run_id),
            ).fetchone()
        return row is not None

    def ingested_run_ids(self) -> set[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT source, run_id FROM paper_ingest").fetchall()
        return {(row["source"], row["run_id"]) for row in rows}

    def delete_ingest(self, source: str, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM paper_ingest WHERE source = ? AND run_id = ?",
                (source, run_id),
            )

    # ------------------------------------------------------------------
    # plans
    # ------------------------------------------------------------------
    def save_plans(self, as_of: date | str, plans: Iterable[Dict[str, Any]]) -> None:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            for plan in plans:
                conn.execute(
                    "INSERT OR REPLACE INTO paper_plans("
                    "as_of, symbol, run_id, source, plan_id, scenario_id, generation_status,"
                    "entry_low, entry_high, stop_loss, target_1, target_2, target_1_exit_ratio,"
                    "valid_from, valid_until, ai_action, ai_position_pct, engine_position_pct,"
                    "priority, status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        day,
                        plan["symbol"],
                        plan.get("run_id"),
                        plan.get("source"),
                        plan.get("plan_id"),
                        plan.get("scenario_id"),
                        plan.get("generation_status") or "generated",
                        plan.get("entry_low"),
                        plan.get("entry_high"),
                        plan.get("stop_loss"),
                        plan.get("target_1"),
                        plan.get("target_2"),
                        plan.get("target_1_exit_ratio"),
                        plan.get("valid_from"),
                        plan.get("valid_until"),
                        plan.get("ai_action"),
                        plan.get("ai_position_pct"),
                        plan.get("engine_position_pct"),
                        int(plan.get("priority") or 0),
                        plan.get("status") or "active",
                    ),
                )

    def plan_for(self, symbol: str, as_of: date | str) -> Optional[Dict[str, Any]]:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_plans WHERE symbol = ? AND as_of <= ? "
                "ORDER BY as_of DESC LIMIT 1",
                (symbol, day),
            ).fetchone()
        return dict(row) if row else None

    def active_plans(self, as_of: date | str) -> List[Dict[str, Any]]:
        """Plans whose execution window contains ``as_of`` and still open."""
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_plans WHERE status = 'active' "
                "AND (valid_from IS NULL OR valid_from <= ?) "
                "AND (valid_until IS NULL OR valid_until >= ?) "
                "ORDER BY as_of ASC, priority ASC, symbol ASC",
                (day, day),
            ).fetchall()
        return [dict(row) for row in rows]

    def deactivate_plan(self, as_of: date | str, symbol: str) -> None:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            conn.execute(
                "UPDATE paper_plans SET status = 'consumed' WHERE as_of = ? AND symbol = ?",
                (day, symbol),
            )

    def expire_plans(self, as_of: date | str) -> int:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE paper_plans SET status = 'expired' "
                "WHERE status = 'active' AND valid_until IS NOT NULL AND valid_until < ?",
                (day,),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # trades / positions / nav
    # ------------------------------------------------------------------
    def save_trades(self, as_of: date | str, trades: Iterable[Dict[str, Any]]) -> None:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            for trade in trades:
                conn.execute(
                    "INSERT OR REPLACE INTO paper_trades("
                    "trade_id, as_of, symbol, side, quantity, price, fees, amount, reason,"
                    "decision_id, run_id, source, close_price, close_in_zone"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        trade["trade_id"],
                        day,
                        trade["symbol"],
                        trade["side"],
                        int(trade["quantity"]),
                        float(trade["price"]),
                        float(trade.get("fees") or 0.0),
                        float(trade.get("amount") or 0.0),
                        trade.get("reason"),
                        trade.get("decision_id"),
                        trade.get("run_id"),
                        trade.get("source"),
                        trade.get("close_price"),
                        None if trade.get("close_in_zone") is None else int(bool(trade["close_in_zone"])),
                    ),
                )

    def trades_on(self, as_of: date | str) -> List[Dict[str, Any]]:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE as_of = ? ORDER BY trade_id",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def trades_for_decision(self, decision_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE decision_id = ? ORDER BY as_of, trade_id",
                (decision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_positions(self, as_of: date | str, positions: Iterable[Dict[str, Any]]) -> None:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            for position in positions:
                conn.execute(
                    "INSERT OR REPLACE INTO paper_positions("
                    "as_of, symbol, quantity, available_quantity, avg_price, last_price,"
                    "stop_loss, target_1, target_2, target_1_exit_ratio, target_1_done, decision_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        day,
                        position["symbol"],
                        int(position["quantity"]),
                        int(position.get("available_quantity") or 0),
                        float(position.get("avg_price") or 0.0),
                        position.get("last_price"),
                        position.get("stop_loss"),
                        position.get("target_1"),
                        position.get("target_2"),
                        position.get("target_1_exit_ratio"),
                        int(position.get("target_1_done") or 0),
                        position.get("decision_id"),
                    ),
                )

    def positions_on(self, as_of: date | str) -> List[Dict[str, Any]]:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE as_of = ? ORDER BY symbol",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_nav(self, as_of: date | str, nav: Dict[str, Any]) -> None:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO paper_nav("
                "as_of, cash, market_value, equity, benchmark_close, benchmark_nav,"
                "positions_count, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    day,
                    float(nav["cash"]),
                    float(nav["market_value"]),
                    float(nav["equity"]),
                    nav.get("benchmark_close"),
                    nav.get("benchmark_nav"),
                    int(nav.get("positions_count") or 0),
                ),
            )

    def latest_nav_before(self, as_of: date | str) -> Optional[Dict[str, Any]]:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_nav WHERE as_of < ? ORDER BY as_of DESC LIMIT 1",
                (day,),
            ).fetchone()
        return dict(row) if row else None

    def latest_nav_date(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(as_of) AS day FROM paper_nav").fetchone()
        return row["day"] if row and row["day"] else None

    def nav_curve(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM paper_nav ORDER BY as_of ASC").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------
    def upsert_decision(self, decision: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_decisions("
                "decision_id, symbol, source, run_id, entry_as_of, entry_price, quantity,"
                "entry_fees, status, exit_as_of, exit_price, realized_return, t5_date,"
                "t5_return, t20_date, t20_return, close_in_zone, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now')) "
                "ON CONFLICT(decision_id) DO UPDATE SET "
                "status=excluded.status, exit_as_of=excluded.exit_as_of, "
                "exit_price=excluded.exit_price, realized_return=excluded.realized_return, "
                "t5_date=excluded.t5_date, t5_return=excluded.t5_return, "
                "t20_date=excluded.t20_date, t20_return=excluded.t20_return, "
                "close_in_zone=excluded.close_in_zone, updated_at=datetime('now')",
                (
                    decision["decision_id"],
                    decision["symbol"],
                    decision.get("source"),
                    decision.get("run_id"),
                    decision["entry_as_of"],
                    float(decision["entry_price"]),
                    int(decision["quantity"]),
                    float(decision.get("entry_fees") or 0.0),
                    decision.get("status") or "open",
                    decision.get("exit_as_of"),
                    decision.get("exit_price"),
                    decision.get("realized_return"),
                    decision.get("t5_date"),
                    decision.get("t5_return"),
                    decision.get("t20_date"),
                    decision.get("t20_return"),
                    None if decision.get("close_in_zone") is None else int(bool(decision["close_in_zone"])),
                ),
            )

    def list_decisions(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_decisions ORDER BY entry_as_of ASC, decision_id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def decisions_on(self, as_of: date | str) -> List[Dict[str, Any]]:
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_decisions WHERE entry_as_of = ?", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # idempotency
    # ------------------------------------------------------------------
    def delete_day(self, as_of: date | str) -> None:
        """Remove the session's rows so a rerun replaces instead of appends."""
        day = as_of.isoformat() if isinstance(as_of, date) else str(as_of)
        with self._connect() as conn:
            conn.execute("DELETE FROM paper_trades WHERE as_of = ?", (day,))
            conn.execute("DELETE FROM paper_positions WHERE as_of = ?", (day,))
            conn.execute("DELETE FROM paper_nav WHERE as_of = ?", (day,))
            conn.execute("DELETE FROM paper_decisions WHERE entry_as_of = ?", (day,))


def resolve_store_dir(config: Any) -> Path:
    configured = str(getattr(config, "agent_paper_trading_db_dir", "") or "").strip()
    if configured:
        return Path(configured)
    database_path = str(getattr(config, "database_path", "") or "").strip()
    base = Path(database_path).parent if database_path else Path("data")
    return base / DEFAULT_DB_DIRNAME


def store_path_for(config: Any, book: str) -> Path:
    return resolve_store_dir(config) / f"{str(book).lower()}.db"
