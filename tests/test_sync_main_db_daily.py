# -*- coding: utf-8 -*-
"""Daily whole-market sync into the main analysis DB."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
from sqlalchemy import create_engine

from scripts.sync_main_db_daily import resolve_candidate_dates, sync_main_db_daily
from src.storage import StockDaily


def _create_main_db(path):
    engine = create_engine(f"sqlite:///{path}")
    StockDaily.__table__.create(engine)
    engine.dispose()


def _insert_main(path, code, day, close):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT OR IGNORE INTO stock_daily "
        "(code,date,open,high,low,close,volume,amount,pct_chg,data_source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, day, close, close, close, close, 1000, 1000.0, 0.0, "test", "t", "t"),
    )
    con.commit()
    con.close()


def _create_sequoia_db(path, dates):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE stock_daily (date TEXT)")
    con.executemany("INSERT INTO stock_daily(date) VALUES (?)", [(d,) for d in dates])
    con.commit()
    con.close()


def _frame(day_compact, items):
    return pd.DataFrame(
        [
            {
                "ts_code": f"{code}.SH",
                "trade_date": day_compact,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "vol": 10.0,
                "amount": 1.0,
                "pct_chg": 0.0,
            }
            for code, px in items
        ]
    )


class FakeClient:
    def __init__(self, daily=None, adj=None, fail=()):
        self.daily_map = daily or {}
        self.adj_map = adj or {}
        self.fail = set(fail)
        self.daily_calls = []
        self.adj_calls = []

    def daily(self, trade_date):
        self.daily_calls.append(trade_date)
        if trade_date in self.fail:
            raise RuntimeError("upstream timeout")
        return self.daily_map.get(trade_date, pd.DataFrame())

    def adj_factor(self, trade_date):
        self.adj_calls.append(trade_date)
        return self.adj_map.get(trade_date, pd.DataFrame())


@pytest.fixture()
def paths(tmp_path):
    main = tmp_path / "main.db"
    adj = tmp_path / "adj.db"
    seq = tmp_path / "seq.db"
    _create_main_db(main)
    return main, adj, seq


def test_sync_is_idempotent_and_skips_complete_days(paths):
    main, adj, seq = paths
    _create_sequoia_db(seq, ["2026-11-20", "2026-11-21"])
    _insert_main(main, "600000", "2026-11-20", 10.0)
    _insert_main(main, "600001", "2026-11-20", 10.0)
    _insert_main(main, "600000", "2026-11-21", 999.0)  # must not be overwritten

    client = FakeClient(
        daily={
            "20261121": _frame("20261121", [("600000", 1.0), ("600002", 2.0), ("600003", 3.0)]),
        },
        adj={
            "20261120": pd.DataFrame([{"ts_code": "600000.SH", "adj_factor": 1.5}]),
            "20261121": pd.DataFrame([{"ts_code": "600000.SH", "adj_factor": 1.6}]),
        },
    )
    first = sync_main_db_daily(
        lookback_sessions=2,
        target_date="2026-11-21",
        main_db=main,
        adj_db=adj,
        sequoia_db=seq,
        client=client,
        full_day_threshold=2,
        logger=lambda _m: None,
    )

    assert client.daily_calls == ["20261121"]
    by_date = {d["date"]: d for d in first["days"]}
    assert by_date["2026-11-20"]["status"] == "skipped_complete"
    assert by_date["2026-11-21"]["status"] == "synced"
    assert by_date["2026-11-21"]["inserted"] == 2
    assert first["inserted_rows"] == 2
    assert first["peak_wal_bytes"] >= 0

    con = sqlite3.connect(main)
    assert con.execute("select close from stock_daily where code='600000' and date='2026-11-21'").fetchone()[0] == 999.0
    assert con.execute("select count(*) from stock_daily where date='2026-11-21'").fetchone()[0] == 3
    con.close()

    second = sync_main_db_daily(
        lookback_sessions=2,
        target_date="2026-11-21",
        main_db=main,
        adj_db=adj,
        sequoia_db=seq,
        client=client,
        full_day_threshold=2,
        logger=lambda _m: None,
    )
    assert client.daily_calls == ["20261121"]  # no new API call on the rerun
    assert all(d["status"] == "skipped_complete" for d in second["days"])
    assert second["inserted_rows"] == 0


def test_single_day_failure_does_not_stop_others(paths):
    main, adj, seq = paths
    _create_sequoia_db(seq, ["2026-11-20", "2026-11-21"])
    client = FakeClient(
        daily={"20261121": _frame("20261121", [("600000", 1.0), ("600002", 2.0)])},
        fail=("20261120",),
    )
    summary = sync_main_db_daily(
        lookback_sessions=2,
        target_date="2026-11-21",
        main_db=main,
        adj_db=adj,
        sequoia_db=seq,
        client=client,
        full_day_threshold=2,
        logger=lambda _m: None,
    )
    by_date = {d["date"]: d for d in summary["days"]}
    assert by_date["2026-11-20"]["status"] == "error"
    assert by_date["2026-11-21"]["status"] == "synced"
    assert len(summary["errors"]) == 1
    assert summary["ok"] is False

    con = sqlite3.connect(main)
    assert con.execute("select count(*) from stock_daily where date='2026-11-21'").fetchone()[0] == 2
    con.close()


def test_unpublished_session_is_reported_not_silent(paths):
    main, adj, seq = paths
    _create_sequoia_db(seq, ["2026-11-20"])
    client = FakeClient()
    messages = []
    summary = sync_main_db_daily(
        lookback_sessions=2,
        target_date="2026-11-24",
        main_db=main,
        adj_db=adj,
        sequoia_db=seq,
        client=client,
        full_day_threshold=2,
        logger=messages.append,
    )
    by_date = {d["date"]: d for d in summary["days"]}
    assert summary["candidate_dates"] == ["2026-11-20", "2026-11-24"]
    assert by_date["2026-11-24"]["status"] == "upstream_not_published"
    assert any("上游未发布 2026-11-24" in m for m in messages)
    assert summary["ok"] is True

    con = sqlite3.connect(main)
    assert con.execute("select count(*) from stock_daily where date='2026-11-24'").fetchone()[0] == 0
    con.close()


def test_adj_factor_backfilled_when_daily_is_complete(paths):
    main, adj, seq = paths
    _create_sequoia_db(seq, ["2026-11-20"])
    _insert_main(main, "600000", "2026-11-20", 10.0)
    _insert_main(main, "600001", "2026-11-20", 10.0)
    client = FakeClient(adj={"20261120": pd.DataFrame([{"ts_code": "600000.SH", "adj_factor": 1.5}])})
    summary = sync_main_db_daily(
        lookback_sessions=1,
        target_date="2026-11-20",
        main_db=main,
        adj_db=adj,
        sequoia_db=seq,
        client=client,
        full_day_threshold=2,
        logger=lambda _m: None,
    )
    assert client.daily_calls == []
    assert client.adj_calls == ["20261120"]
    assert summary["inserted_adj_rows"] == 1
    con = sqlite3.connect(adj)
    assert con.execute("select adj_factor from stock_adj_factors where code='600000' and date='2026-11-20'").fetchone()[0] == 1.5
    con.close()


def test_candidate_dates_include_target_even_when_sequoia_lags(tmp_path):
    seq = tmp_path / "seq.db"
    _create_sequoia_db(seq, ["2026-11-20", "2026-11-23"])
    dates = resolve_candidate_dates(seq, lookback_sessions=5, target_date="2026-11-24")
    assert dates == ["2026-11-20", "2026-11-23", "2026-11-24"]
