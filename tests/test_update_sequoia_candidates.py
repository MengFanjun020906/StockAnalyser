import sqlite3
import sys
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.update_sequoia_candidates import (
    BENCHMARK_INDEX_SYMBOL,
    INDEX_SYMBOLS,
    REGIME_INDEX_BAOSTOCK_CODE,
    REGIME_INDEX_SYMBOL,
    db_summary,
    filter_resume_symbols,
    get_global_max_date_in_db,
    is_baostock_session_expired_error,
    load_symbol_max_dates,
    init_db,
    main,
    prune_symbol_row_limit,
    prune_to_latest_trading_days,
    resolve_cn_resume_target_date,
    upsert_rows,
)


def test_update_sequoia_candidates_tushare_cli_bridges_existing_price_basis(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    upsert_rows(
        str(db_path),
        [
            ("600001", "2026-01-02", 90, 110, 80, 100, 1_000, 10_000),
            (BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3000, 3050, 2950, 3020, 10_000, 20_000),
            (REGIME_INDEX_SYMBOL, "2026-01-02", 4000, 4050, 3950, 4020, 10_000, 20_000),
        ],
    )

    def fake_tushare_query(api_name, params=None, fields="", timeout=30):
        params = params or {}
        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    {
                        "ts_code": params["ts_code"],
                        "trade_date": "20260103",
                        "open": 3100,
                        "high": 3150,
                        "low": 3050,
                        "close": 3120,
                        "vol": 120,
                        "amount": 340,
                    },
                    {
                        "ts_code": params["ts_code"],
                        "trade_date": "20260102",
                        "open": 3000,
                        "high": 3050,
                        "low": 2950,
                        "close": 3020,
                        "vol": 100,
                        "amount": 200,
                    },
                ]
            )
        if api_name == "daily":
            trade_date = params["trade_date"]
            rows = [
                {
                    "ts_code": "600001.SH",
                    "trade_date": trade_date,
                    "open": 9 if trade_date == "20260102" else 10.5,
                    "high": 11 if trade_date == "20260102" else 11.5,
                    "low": 8 if trade_date == "20260102" else 10,
                    "close": 10 if trade_date == "20260102" else 11,
                    "vol": 10 if trade_date == "20260102" else 12,
                    "amount": 20 if trade_date == "20260102" else 34,
                }
            ]
            if trade_date == "20260103":
                rows.append(
                    {
                        "ts_code": "600002.SH",
                        "trade_date": trade_date,
                        "open": 20,
                        "high": 23,
                        "low": 19,
                        "close": 22,
                        "vol": 5,
                        "amount": 7,
                    }
                )
            return pd.DataFrame(rows)
        if api_name == "adj_factor":
            trade_date = params["trade_date"]
            codes = ["600001.SH"]
            if trade_date == "20260103":
                codes.append("600002.SH")
            return pd.DataFrame(
                [
                    {"ts_code": code, "trade_date": trade_date, "adj_factor": 2.0}
                    for code in codes
                ]
            )
        raise AssertionError(f"unexpected Tushare API: {api_name}")

    argv = [
        "update_sequoia_candidates.py",
        "--source",
        "tushare",
        "--db-path",
        str(db_path),
        "--trading-days",
        "260",
        "--symbols",
        "600001,600002",
    ]
    with patch.object(sys, "argv", argv), patch.dict("os.environ", {"TUSHARE_TOKEN": "test-token"}), patch(
        "scripts.update_sequoia_candidates.resolve_cn_resume_target_date",
        return_value=date(2026, 1, 3),
    ), patch(
        "data_provider.tushare_client.query_tushare_api",
        side_effect=fake_tushare_query,
    ):
        assert main() == 0

    with sqlite3.connect(db_path) as conn:
        bridged = conn.execute(
            "SELECT open, high, low, close, volume, turnover FROM stock_daily "
            "WHERE symbol = '600001' AND date = '2026-01-03'"
        ).fetchone()
        new_stock = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = '600002' AND date = '2026-01-03'"
        ).fetchone()
        completion_metadata = conn.execute(
            "SELECT value FROM stock_daily_metadata WHERE key = 'last_completed_date'"
        ).fetchone()

    assert bridged == (105.0, 115.0, 100.0, 110.0, 1200.0, 34_000.0)
    assert new_stock == (44.0,)
    assert completion_metadata is None


def test_update_sequoia_candidates_tushare_bridges_resumed_stock_from_its_last_trade(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    upsert_rows(
        str(db_path),
        [
            ("600001", "2026-01-01", 45, 55, 40, 50, 1_000, 10_000),
            (BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3000, 3050, 2950, 3020, 10_000, 20_000),
            (REGIME_INDEX_SYMBOL, "2026-01-02", 4000, 4050, 3950, 4020, 10_000, 20_000),
        ],
    )

    def stock_row(code, trade_date, close):
        return {
            "ts_code": code,
            "trade_date": trade_date,
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 1,
            "close": close,
            "vol": 10,
            "amount": 20,
        }

    def fake_tushare_query(api_name, params=None, fields="", timeout=30):
        params = params or {}
        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    stock_row(params["ts_code"], "20260103", 3120),
                    stock_row(params["ts_code"], "20260102", 3020),
                ]
            )
        trade_date = params["trade_date"]
        codes_by_date = {
            "20260101": [("600001.SH", 5)],
            "20260102": [("600002.SH", 8)],
            "20260103": [("600001.SH", 6), ("600002.SH", 9)],
        }
        if api_name == "daily":
            return pd.DataFrame(
                [stock_row(code, trade_date, close) for code, close in codes_by_date[trade_date]]
            )
        if api_name == "adj_factor":
            return pd.DataFrame(
                [
                    {"ts_code": code, "trade_date": trade_date, "adj_factor": 2.0}
                    for code, _close in codes_by_date[trade_date]
                ]
            )
        raise AssertionError(f"unexpected Tushare API: {api_name}")

    argv = ["update_sequoia_candidates.py", "--source", "tushare", "--db-path", str(db_path)]
    with patch.object(sys, "argv", argv), patch.dict("os.environ", {"TUSHARE_TOKEN": "test-token"}), patch(
        "scripts.update_sequoia_candidates.resolve_cn_resume_target_date",
        return_value=date(2026, 1, 3),
    ), patch(
        "data_provider.tushare_client.query_tushare_api",
        side_effect=fake_tushare_query,
    ):
        assert main() == 0

    with sqlite3.connect(db_path) as conn:
        close = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = '600001' AND date = '2026-01-03'"
        ).fetchone()[0]
        completion_date = conn.execute(
            "SELECT value FROM stock_daily_metadata WHERE key = 'last_completed_date'"
        ).fetchone()[0]

    assert close == 60.0
    assert completion_date == "2026-01-03"


def test_update_sequoia_candidates_tushare_targeted_bridge_recovers_missing_market_overlap(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    upsert_rows(
        str(db_path),
        [
            ("300333", "2026-01-02", 90, 110, 80, 100, 1_000, 10_000),
            (BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3000, 3050, 2950, 3020, 10_000, 20_000),
            (REGIME_INDEX_SYMBOL, "2026-01-02", 4000, 4050, 3950, 4020, 10_000, 20_000),
        ],
    )

    def stock_row(code, trade_date, close, volume=10):
        return {
            "ts_code": code,
            "trade_date": trade_date,
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 1,
            "close": close,
            "vol": volume,
            "amount": 20,
        }

    def fake_tushare_query(api_name, params=None, fields="", timeout=30):
        params = params or {}
        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    stock_row(params["ts_code"], "20260103", 3120),
                    stock_row(params["ts_code"], "20260102", 3020),
                ]
            )
        if api_name == "daily" and "ts_code" in params:
            # The full-market snapshot omits this symbol, while the targeted
            # query returns its suspended/zero-volume overlap bar.
            return pd.DataFrame([stock_row("300333.SZ", "20260102", 10, volume=0)])
        if api_name == "adj_factor" and "ts_code" in params:
            return pd.DataFrame(
                [{"ts_code": "300333.SZ", "trade_date": "20260102", "adj_factor": 2.0}]
            )
        if api_name == "daily":
            trade_date = params["trade_date"]
            codes = ["600001.SH"] if trade_date == "20260102" else ["300333.SZ", "600001.SH"]
            return pd.DataFrame(
                [
                    stock_row(
                        code,
                        trade_date,
                        11 if code.startswith("300") and trade_date == "20260103" else 10,
                    )
                    for code in codes
                ]
            )
        if api_name == "adj_factor":
            trade_date = params["trade_date"]
            codes = ["600001.SH"] if trade_date == "20260102" else ["300333.SZ", "600001.SH"]
            return pd.DataFrame(
                [{"ts_code": code, "trade_date": trade_date, "adj_factor": 2.0} for code in codes]
            )
        raise AssertionError(f"unexpected Tushare API: {api_name}")

    argv = ["update_sequoia_candidates.py", "--source", "tushare", "--db-path", str(db_path)]
    with patch.object(sys, "argv", argv), patch.dict("os.environ", {"TUSHARE_TOKEN": "test-token"}), patch(
        "scripts.update_sequoia_candidates.resolve_cn_resume_target_date",
        return_value=date(2026, 1, 3),
    ), patch(
        "data_provider.tushare_client.query_tushare_api",
        side_effect=fake_tushare_query,
    ):
        assert main() == 0

    with sqlite3.connect(db_path) as conn:
        close = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = '300333' AND date = '2026-01-03'"
        ).fetchone()[0]

    assert close == 110.0


def test_update_sequoia_candidates_tushare_does_not_complete_before_target_date(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    upsert_rows(
        str(db_path),
        [(BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3000, 3050, 2950, 3020, 10_000, 20_000)],
    )

    def fake_tushare_query(api_name, params=None, fields="", timeout=30):
        params = params or {}
        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    {
                        "ts_code": params["ts_code"],
                        "trade_date": "20260102",
                        "open": 3000,
                        "high": 3050,
                        "low": 2950,
                        "close": 3020,
                        "vol": 100,
                        "amount": 200,
                    }
                ]
            )
        if api_name == "daily":
            return pd.DataFrame(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20260102",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "vol": 100,
                        "amount": 200,
                    }
                ]
            )
        if api_name == "adj_factor":
            return pd.DataFrame(
                [{"ts_code": "600001.SH", "trade_date": "20260102", "adj_factor": 2.0}]
            )
        raise AssertionError(f"unexpected Tushare API: {api_name}")

    argv = ["update_sequoia_candidates.py", "--source", "tushare", "--db-path", str(db_path)]
    with patch.object(sys, "argv", argv), patch.dict("os.environ", {"TUSHARE_TOKEN": "test-token"}), patch(
        "scripts.update_sequoia_candidates.resolve_cn_resume_target_date",
        return_value=date(2026, 1, 3),
    ), patch(
        "data_provider.tushare_client.query_tushare_api",
        side_effect=fake_tushare_query,
    ):
        assert main() == 3


def test_update_sequoia_candidates_tushare_rejects_partial_market_snapshot(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    existing_rows = [
        (f"6000{index:02d}", "2026-01-02", 9, 11, 8, 10, 1_000, 10_000)
        for index in range(10)
    ]
    existing_rows.append(
        (BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3000, 3050, 2950, 3020, 10_000, 20_000)
    )
    upsert_rows(str(db_path), existing_rows)

    def daily_row(code, trade_date):
        return {
            "ts_code": f"{code}.SH",
            "trade_date": trade_date,
            "open": 9,
            "high": 11,
            "low": 8,
            "close": 10,
            "vol": 10,
            "amount": 20,
        }

    def fake_tushare_query(api_name, params=None, fields="", timeout=30):
        params = params or {}
        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    {**daily_row(params["ts_code"].split(".")[0], "20260103"), "ts_code": params["ts_code"]},
                    {**daily_row(params["ts_code"].split(".")[0], "20260102"), "ts_code": params["ts_code"]},
                ]
            )
        codes = [f"6000{index:02d}" for index in range(10)] if params["trade_date"] == "20260102" else ["600000"]
        if api_name == "daily":
            return pd.DataFrame([daily_row(code, params["trade_date"]) for code in codes])
        if api_name == "adj_factor":
            return pd.DataFrame(
                [
                    {"ts_code": f"{code}.SH", "trade_date": params["trade_date"], "adj_factor": 1.0}
                    for code in codes
                ]
            )
        raise AssertionError(f"unexpected Tushare API: {api_name}")

    argv = ["update_sequoia_candidates.py", "--source", "tushare", "--db-path", str(db_path)]
    with patch.object(sys, "argv", argv), patch.dict("os.environ", {"TUSHARE_TOKEN": "test-token"}), patch(
        "scripts.update_sequoia_candidates.resolve_cn_resume_target_date",
        return_value=date(2026, 1, 3),
    ), patch(
        "data_provider.tushare_client.query_tushare_api",
        side_effect=fake_tushare_query,
    ):
        assert main() == 3


def test_update_sequoia_candidates_upserts_and_summarizes(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))

    df = pd.DataFrame(
        [
            {
                "symbol": "600001",
                "date": "2026-01-02",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1000,
                "turnover": 10_000,
            },
            {
                "symbol": "600001",
                "date": "2026-01-03",
                "open": 11,
                "high": 12,
                "low": 10,
                "close": 11.5,
                "volume": 1100,
                "turnover": 11_000,
            },
        ]
    )

    assert upsert_rows(str(db_path), df) == 2
    assert db_summary(str(db_path)) == (2, 1, "2026-01-02", "2026-01-03")

    replacement = df.iloc[[0]].copy()
    replacement.loc[:, "close"] = 12.5
    assert upsert_rows(str(db_path), replacement) == 1

    with sqlite3.connect(db_path) as conn:
        close = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = ? AND date = ?",
            ("600001", "2026-01-02"),
        ).fetchone()[0]
    assert close == 12.5
    assert db_summary(str(db_path))[0] == 2


def test_update_sequoia_candidates_keeps_benchmark_index_distinct_from_stock_code(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))

    rows = [
        ("000001", "2026-01-02", 10, 11, 9, 10.5, 1000, 10_000),
        (BENCHMARK_INDEX_SYMBOL, "2026-01-02", 3200, 3210, 3190, 3205, 1_000_000, 100_000_000),
    ]

    assert upsert_rows(str(db_path), rows) == 2

    with sqlite3.connect(db_path) as conn:
        stored = dict(
            conn.execute(
                "SELECT symbol, close FROM stock_daily WHERE date = ?",
                ("2026-01-02",),
            ).fetchall()
        )

    assert stored["000001"] == 10.5
    assert stored[BENCHMARK_INDEX_SYMBOL] == 3205
    assert db_summary(str(db_path)) == (2, 2, "2026-01-02", "2026-01-02")


def test_update_sequoia_candidates_refreshes_distinct_benchmark_and_regime_indices():
    assert BENCHMARK_INDEX_SYMBOL != REGIME_INDEX_SYMBOL
    assert REGIME_INDEX_SYMBOL == "000300.SH"
    assert (REGIME_INDEX_SYMBOL, REGIME_INDEX_BAOSTOCK_CODE) in INDEX_SYMBOLS
    assert len({symbol for symbol, _code in INDEX_SYMBOLS}) == len(INDEX_SYMBOLS)


def test_update_sequoia_candidates_prunes_to_latest_dates_and_per_symbol_limit(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    rows = []
    for symbol in ("600001", "600002"):
        for day in range(1, 6):
            rows.append(
                {
                    "symbol": symbol,
                    "date": f"2026-01-0{day}",
                    "open": 10 + day,
                    "high": 11 + day,
                    "low": 9 + day,
                    "close": 10.5 + day,
                    "volume": 1000 + day,
                    "turnover": 10_000 + day,
                }
            )
    upsert_rows(str(db_path), pd.DataFrame(rows))

    deleted, cutoff = prune_to_latest_trading_days(str(db_path), 3)

    assert deleted == 4
    assert cutoff == "2026-01-03"
    assert db_summary(str(db_path)) == (6, 2, "2026-01-03", "2026-01-05")

    deleted_by_symbol = prune_symbol_row_limit(str(db_path), 2)

    assert deleted_by_symbol == 2
    assert db_summary(str(db_path)) == (4, 2, "2026-01-04", "2026-01-05")


def test_update_sequoia_candidates_resume_filters_completed_symbols(tmp_path):
    db_path = tmp_path / "sequoia_v2.db"
    init_db(str(db_path))
    upsert_rows(
        str(db_path),
        [
            ("600001", "2026-01-03", 10, 11, 9, 10.5, 1000, 10_000),
            ("600002", "2026-01-02", 10, 11, 9, 10.5, 1000, 10_000),
        ],
    )

    symbol_max_dates = load_symbol_max_dates(str(db_path))
    pending, skipped = filter_resume_symbols(
        ["600001", "600002", "600003"],
        symbol_max_dates,
        get_global_max_date_in_db(str(db_path)),
    )

    assert skipped == 1
    assert pending == ["600002", "600003"]


def test_update_sequoia_candidates_resume_target_uses_effective_cn_trading_date():
    class _FakeCalendar:
        def is_session(self, check_date):
            return check_date == date(2026, 6, 12)

        def date_to_session(self, check_date, direction="previous"):
            assert check_date == date(2026, 6, 13)
            assert direction == "previous"
            return pd.Timestamp(date(2026, 6, 12))

    current_time = datetime(2026, 6, 13, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with patch("src.core.trading_calendar._XCALS_AVAILABLE", True), patch(
        "src.core.trading_calendar.xcals",
        SimpleNamespace(get_calendar=lambda _ex: _FakeCalendar()),
        create=True,
    ):
        assert resolve_cn_resume_target_date(current_time) == date(2026, 6, 12)


def test_update_sequoia_candidates_detects_baostock_session_expired_errors():
    assert is_baostock_session_expired_error("用户未登录")
    assert is_baostock_session_expired_error("please login first")
    assert not is_baostock_session_expired_error("无数据")
