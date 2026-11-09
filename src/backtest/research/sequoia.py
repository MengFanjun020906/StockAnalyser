# -*- coding: utf-8 -*-
"""Full-market Sequoia daily bars, rebased to cash prices when needed.

The Sequoia snapshot uses ``legacy_hfq_bridge`` OHLC while volume/turnover are
raw.  For research-mode selection/planning we rebase OHLC to the cash-price
scale inferred from turnover/volume (or main-DB overlap when available).  This
is a best-effort research basis and is always labelled as such.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from src.services.sequoia_price_rebaser import resolve_amount_volume_cash_price_rebase

from src.backtest.entities import DailyBar

_A_SHARE_PREFIX = re.compile(r"^(600|601|603|605|688|689|000|001|002|003|300|301)")
_DEFAULT_PATH = "vendor/Sequoia-X/data/sequoia_v2.db"


@dataclass(frozen=True)
class SequoiaCoverage:
    path: str
    available: bool
    last_completed_date: date | None = None
    price_basis: str = ""
    symbol_count: int = 0
    min_date: date | None = None
    max_date: date | None = None


class SequoiaDailySource:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = path or os.getenv("SEQUOIA_CANDIDATE_DB_PATH") or _DEFAULT_PATH
        self.path = Path(raw).expanduser()
        self._available = self.path.exists()
        self._last_completed_date: date | None = None
        self._price_basis = ""
        if self._available:
            try:
                with self._connect() as conn:
                    rows = conn.execute("SELECT key,value FROM stock_daily_metadata").fetchall()
                    metadata = {str(row[0]): str(row[1]) for row in rows}
                    self._price_basis = metadata.get("price_basis", "")
                    last = metadata.get("last_completed_date")
                    if last:
                        self._last_completed_date = date.fromisoformat(last)
            except Exception:
                self._available = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def available(self) -> bool:
        return self._available

    def coverage(self) -> SequoiaCoverage:
        if not self._available:
            return SequoiaCoverage(path=str(self.path), available=False)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM stock_daily"
                ).fetchone()
            return SequoiaCoverage(
                path=str(self.path),
                available=True,
                last_completed_date=self._last_completed_date,
                price_basis=self._price_basis,
                symbol_count=int(row[0] or 0),
                min_date=date.fromisoformat(row[1]) if row[1] else None,
                max_date=date.fromisoformat(row[2]) if row[2] else None,
            )
        except Exception:
            return SequoiaCoverage(path=str(self.path), available=False)

    def list_symbols(
        self,
        *,
        start_date: date,
        end_date: date,
        min_bars: int,
        limit: int,
    ) -> list[str]:
        if not self._available:
            return []
        lookback_start = start_date - timedelta(days=max(90, min_bars * 4))
        sql = """
            SELECT symbol,
                   COUNT(*) AS bar_count,
                   SUM(COALESCE(turnover, 0)) AS turnover
            FROM stock_daily
            WHERE date >= ? AND date <= ?
            GROUP BY symbol
            HAVING bar_count >= ?
            ORDER BY turnover DESC, bar_count DESC, symbol ASC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(
                sql,
                (
                    lookback_start.isoformat(),
                    end_date.isoformat(),
                    int(min_bars),
                    int(max(limit * 3, limit)),
                ),
            ).fetchall()
        result = []
        for row in rows:
            raw_symbol = str(row[0] or "").strip()
            if not raw_symbol.isdigit() or len(raw_symbol) != 6:
                continue
            symbol = _normalize_symbol(raw_symbol)
            if symbol and symbol not in result:
                result.append(symbol)
            if len(result) >= limit:
                break
        return result

    def daily_bars(
        self,
        symbols: Iterable[str],
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[list[DailyBar], list[str]]:
        symbols = sorted({_normalize_symbol(item) for item in symbols if _normalize_symbol(item)})
        if not self._available or not symbols:
            return [], []
        rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as conn:
            for offset in range(0, len(symbols), 200):
                chunk = symbols[offset : offset + 200]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT symbol,date,open,high,low,close,volume,turnover "
                    f"FROM stock_daily WHERE symbol IN ({placeholders}) AND date >= ? AND date <= ? "
                    f"ORDER BY symbol,date",
                    (*chunk, start_date.isoformat(), end_date.isoformat()),
                ).fetchall()
                for row in rows:
                    symbol = _normalize_symbol(row["symbol"])
                    if not symbol:
                        continue
                    rows_by_symbol.setdefault(symbol, []).append(dict(row))
        result: list[DailyBar] = []
        rebased: list[str] = []
        for symbol in symbols:
            rows = rows_by_symbol.get(symbol, [])
            if not rows:
                continue
            rebase = resolve_amount_volume_cash_price_rebase(rows)
            if rebase is not None and rebase.adjusted:
                rebased.append(symbol)
            for row in rows:
                try:
                    factor = rebase.factor_for_date(row["date"]) if rebase is not None else None
                    open_price = _cash_price(row["open"], factor)
                    high = _cash_price(row["high"], factor)
                    low = _cash_price(row["low"], factor)
                    close = _cash_price(row["close"], factor)
                    high = max(high, open_price, close)
                    low = min(low, open_price, close)
                    volume = Decimal(str(row["volume"] or 0))
                    amount = Decimal(str(row["turnover"] or 0))
                    session = date.fromisoformat(str(row["date"]))
                except Exception:
                    continue
                if min(open_price, high, low, close) <= 0 or high < low:
                    continue
                result.append(
                    DailyBar(
                        symbol=symbol,
                        session=session,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        amount=amount,
                        source="sequoia_cash_rebased" if factor else "sequoia_cash",
                    )
                )
        result.sort(key=lambda item: (item.symbol, item.session))
        return result, rebased

    def benchmark(
        self,
        symbol: str,
        *,
        start_date: date,
        end_date: date,
    ) -> dict[date, Decimal]:
        if not self._available:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date,close FROM stock_daily WHERE symbol=? AND date >= ? AND date <= ? ORDER BY date",
                (symbol, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        result = {}
        for row in rows:
            try:
                close = Decimal(str(row["close"]))
                if close > 0:
                    result[date.fromisoformat(str(row["date"]))] = close
            except Exception:
                continue
        return result


def _cash_price(value: Any, factor: float | None) -> Decimal:
    raw = Decimal(str(value))
    if factor and factor > 0:
        raw = raw / Decimal(str(factor))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    digits = "".join(ch for ch in symbol if ch.isdigit())[-6:]
    if len(digits) != 6 or not _A_SHARE_PREFIX.match(digits):
        return ""
    return digits


__all__ = ["SequoiaCoverage", "SequoiaDailySource"]
