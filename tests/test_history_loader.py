# -*- coding: utf-8 -*-
"""Unit tests for src.services.history_loader (Issue #1066)."""
from __future__ import annotations

import unittest
import os
import sqlite3
import tempfile
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd


class HistoryLoaderTestCase(unittest.TestCase):
    """Tests for load_history_df and frozen target date ContextVar."""

    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {
                "SEQUOIA_CANDIDATE_DB_PATH": os.path.join(tempfile.gettempdir(), "missing-sequoia-history-loader.db"),
                "ALPHASIFT_CANDIDATE_DB_PATH": "",
                "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "missing-stock-analysis-history-loader.db"),
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    # ------------------------------------------------------------------
    # ContextVar lifecycle
    # ------------------------------------------------------------------
    def test_frozen_target_date_lifecycle(self):
        from src.services.history_loader import (
            get_frozen_target_date,
            reset_frozen_target_date,
            set_frozen_target_date,
        )

        self.assertIsNone(get_frozen_target_date())
        d = date(2026, 4, 18)
        token = set_frozen_target_date(d)
        self.assertEqual(get_frozen_target_date(), d)
        reset_frozen_target_date(token)
        self.assertIsNone(get_frozen_target_date())

    # ------------------------------------------------------------------
    # DB hit path
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_returns_db_data_when_sufficient(self, mock_get_db):
        from src.services.history_loader import load_history_df

        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {
            "date": "2026-04-18",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        }
        mock_db = MagicMock()
        mock_db.get_data_range.return_value = [fake_bar] * 40
        mock_get_db.return_value = mock_db

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNotNone(df)
        self.assertEqual(source, "db_cache")
        self.assertEqual(len(df), 40)
        mock_db.get_data_range.assert_called_once()

    @patch("src.storage.get_db")
    def test_rebases_db_cache_hfq_prices_from_amount_volume(self, mock_get_db):
        from src.services.history_loader import load_history_df

        factor = 34.0
        bars = []
        for day in range(1, 31):
            cash_close = 57.0 + day * 0.1
            cash_open = cash_close - 0.4
            cash_high = cash_close + 1.2
            cash_low = cash_close - 1.1
            volume = 1000000 + day
            fake_bar = MagicMock()
            fake_bar.to_dict.return_value = {
                "date": date(2026, 6, day).isoformat(),
                "open": cash_open * factor,
                "high": cash_high * factor,
                "low": cash_low * factor,
                "close": cash_close * factor,
                "volume": volume,
                "amount": volume * cash_close,
            }
            bars.append(fake_bar)

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = bars
        mock_get_db.return_value = mock_db

        with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
            df, source = load_history_df("002475", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "db_cache")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 60.0)
        self.assertAlmostEqual(float(df["open"].iloc[-1]), 59.6)
        self.assertAlmostEqual(float(df["high"].iloc[-1]), 61.2)
        self.assertAlmostEqual(float(df["low"].iloc[-1]), 58.9)
        self.assertAlmostEqual(float(df["amount"].iloc[-1]), (1000000 + 30) * 60.0)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), factor)
        self.assertEqual(df["price_adjustment_basis"].iloc[-1], "db_amount_volume_vwap")

    # ------------------------------------------------------------------
    # DB miss → DFM fallback
    # ------------------------------------------------------------------
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_falls_back_to_dfm_when_db_empty(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        fake_df = pd.DataFrame({"close": [1, 2, 3]})
        mock_fm = MagicMock()
        mock_fm.get_daily_data.return_value = (fake_df, "eastmoney")
        mock_get_fm.return_value = mock_fm

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNotNone(df)
        self.assertEqual(source, "eastmoney")
        mock_fm.get_daily_data.assert_called_once_with("600519", days=60)

    @patch("src.services.history_loader._load_sequoia_history_df", return_value=None)
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_strict_minimum_records_rejects_short_cache_and_fetches_network(
        self,
        mock_get_db,
        mock_get_fm,
        _mock_sequoia,
    ):
        from src.services.history_loader import load_history_df

        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {"date": "2026-08-07", "close": 10}
        mock_db = MagicMock()
        mock_db.get_data_range.return_value = [fake_bar] * 148
        mock_get_db.return_value = mock_db

        network_df = pd.DataFrame({"date": pd.bdate_range(end="2026-08-07", periods=520), "close": range(520)})
        mock_fm = MagicMock()
        mock_fm.get_daily_data.return_value = (network_df, "eastmoney")
        mock_get_fm.return_value = mock_fm

        df, source = load_history_df(
            "600487",
            days=520,
            target_date=date(2026, 8, 7),
            minimum_records=520,
        )

        self.assertEqual(source, "eastmoney")
        self.assertEqual(len(df), 520)
        mock_fm.get_daily_data.assert_called_once_with("600487", days=520)

    @patch("src.services.history_loader._load_sequoia_history_df", return_value=None)
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_strict_target_date_rejects_previous_session_cache(
        self,
        mock_get_db,
        mock_get_fm,
        _mock_sequoia,
    ):
        from src.services.history_loader import load_history_df

        bars = []
        for index in range(40):
            fake_bar = MagicMock()
            fake_bar.to_dict.return_value = {
                "date": "2026-08-12",
                "open": 10 + index * 0.01,
                "high": 11 + index * 0.01,
                "low": 9 + index * 0.01,
                "close": 10.5 + index * 0.01,
                "volume": 100,
                "amount": 1050,
            }
            bars.append(fake_bar)
        mock_db = MagicMock()
        mock_db.get_data_range.return_value = bars
        mock_get_db.return_value = mock_db

        network_df = pd.DataFrame(
            {
                "date": pd.bdate_range(end="2026-08-13", periods=40),
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            }
        )
        mock_fm = MagicMock()
        mock_fm.get_daily_data.return_value = (network_df, "tushare")
        mock_get_fm.return_value = mock_fm

        with patch(
            "src.services.history_loader._effective_cache_end",
            return_value=date(2026, 8, 13),
        ):
            df, source = load_history_df(
                "600330",
                days=40,
                target_date=date(2026, 8, 13),
                minimum_records=40,
                require_target_date=True,
            )

        self.assertEqual(source, "tushare")
        self.assertIsNotNone(df)
        self.assertEqual(mock_fm.get_daily_data.call_count, 1)

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_can_skip_network_fallback_when_db_empty(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        df, source = load_history_df(
            "000300",
            days=260,
            target_date=date(2026, 5, 26),
            fallback_to_network=False,
        )

        self.assertIsNone(df)
        self.assertEqual(source, "db_cache_miss")
        mock_get_fm.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_falls_back_to_sequoia_stock_daily_before_network(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "sequoia_v2.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = [
                    (
                        "300243",
                        date(2026, 5, day).isoformat(),
                        10.0,
                        11.0,
                        9.5,
                        10.5,
                        1000000 + day,
                        2000000 + day,
                    )
                    for day in range(1, 31)
                ]
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

            with patch.dict(os.environ, {"SEQUOIA_CANDIDATE_DB_PATH": db_path}):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 5, 30)):
                    df, source = load_history_df("300243", days=30, target_date=date(2026, 5, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertEqual(len(df), 30)
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_rebases_sequoia_hfq_prices_to_main_db_cash_prices(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        early_factor = 7.491968
        late_factor = 7.669257
        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            main_path = os.path.join(tmpdir, "stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            "600519",
                            date(2026, 6, day).isoformat(),
                            (990 + day) * (early_factor if day <= 15 else late_factor),
                            (1010 + day) * (early_factor if day <= 15 else late_factor),
                            (980 + day) * (early_factor if day <= 15 else late_factor),
                            (1000 + day) * (early_factor if day <= 15 else late_factor),
                            1000000 + day,
                            200000000 + day,
                        )
                        for day in range(1, 31)
                    ],
                )
                conn.commit()

            with sqlite3.connect(main_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        code TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        amount REAL
                    )"""
                )
                conn.executemany(
                    "INSERT INTO stock_daily(code,date,open,high,low,close,volume,amount) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (
                            "600519",
                            date(2026, 6, day).isoformat(),
                            990 + day,
                            1010 + day,
                            980 + day,
                            1000 + day,
                            1000000 + day,
                            200000000 + day,
                        )
                        for day in range(10, 26)
                    ],
                )
                conn.commit()

            with patch.dict(os.environ, {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": main_path}):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("600519", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 1030.0)
        self.assertAlmostEqual(float(df["open"].iloc[-1]), 1020.0)
        self.assertAlmostEqual(float(df["amount"].iloc[-1]), 200000030.0)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), late_factor, places=3)
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_rebases_sequoia_hfq_prices_from_amount_volume_when_main_overlap_missing(
        self,
        mock_get_db,
        mock_get_fm,
    ):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        factor = 20.0
        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            missing_main_path = os.path.join(tmpdir, "missing_stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = []
                for day in range(1, 31):
                    cash_close = 70.0 + day * 0.1
                    cash_open = cash_close - 0.5
                    cash_high = cash_close + 1.0
                    cash_low = cash_close - 1.0
                    volume = 1000000 + day
                    rows.append(
                        (
                            "000858",
                            date(2026, 6, day).isoformat(),
                            cash_open * factor,
                            cash_high * factor,
                            cash_low * factor,
                            cash_close * factor,
                            volume,
                            volume * cash_close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with patch.dict(
                os.environ,
                {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": missing_main_path},
            ):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("000858", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 73.0)
        self.assertAlmostEqual(float(df["open"].iloc[-1]), 72.5)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), factor)
        self.assertEqual(df["price_adjustment_basis"].iloc[-1], "amount_volume_vwap")
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_rebases_sequoia_hfq_prices_from_amount_volume_when_main_overlap_is_same_scale(
        self,
        mock_get_db,
        mock_get_fm,
    ):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        factor = 34.0
        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            main_path = os.path.join(tmpdir, "stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = []
                for day in range(1, 31):
                    cash_close = 57.0 + day * 0.1
                    cash_open = cash_close - 0.4
                    cash_high = cash_close + 1.2
                    cash_low = cash_close - 1.1
                    volume = 1000000 + day
                    rows.append(
                        (
                            "002475",
                            date(2026, 6, day).isoformat(),
                            cash_open * factor,
                            cash_high * factor,
                            cash_low * factor,
                            cash_close * factor,
                            volume,
                            volume * cash_close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with sqlite3.connect(main_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        code TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        amount REAL
                    )"""
                )
                rows = []
                for day in range(10, 26):
                    cash_close = 57.0 + day * 0.1
                    volume = 1000000 + day
                    rows.append(
                        (
                            "002475",
                            date(2026, 6, day).isoformat(),
                            (cash_close - 0.4) * factor,
                            (cash_close + 1.2) * factor,
                            (cash_close - 1.1) * factor,
                            cash_close * factor,
                            volume,
                            volume * cash_close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(code,date,open,high,low,close,volume,amount) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with patch.dict(os.environ, {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": main_path}):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("002475", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 60.0)
        self.assertAlmostEqual(float(df["open"].iloc[-1]), 59.6)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), factor)
        self.assertEqual(df["price_adjustment_basis"].iloc[-1], "amount_volume_vwap")
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_rebases_sequoia_hfq_prices_with_rolling_amount_volume_when_recent_factor_jumps(
        self,
        mock_get_db,
        mock_get_fm,
    ):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        early_factor = 2.0
        late_factor = 3.0
        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            missing_main_path = os.path.join(tmpdir, "missing_stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = []
                for day in range(1, 31):
                    cash_close = 50.0 + day * 0.1
                    cash_open = cash_close - 0.5
                    cash_high = cash_close + 1.0
                    cash_low = cash_close - 1.0
                    volume = 1000000 + day
                    factor = early_factor if day <= 15 else late_factor
                    rows.append(
                        (
                            "603585",
                            date(2026, 6, day).isoformat(),
                            cash_open * factor,
                            cash_high * factor,
                            cash_low * factor,
                            cash_close * factor,
                            volume,
                            volume * cash_close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with patch.dict(
                os.environ,
                {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": missing_main_path},
            ):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("603585", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 53.0)
        self.assertAlmostEqual(float(df["open"].iloc[-1]), 52.5)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), late_factor)
        self.assertEqual(df["price_adjustment_basis"].iloc[-1], "amount_volume_vwap_rolling")
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_rebases_sequoia_hfq_prices_with_rolling_amount_volume_near_material_threshold(
        self,
        mock_get_db,
        mock_get_fm,
    ):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        early_factor = 1.3
        late_factor = 1.9
        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            missing_main_path = os.path.join(tmpdir, "missing_stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = []
                for day in range(1, 31):
                    cash_close = 80.0 + day * 0.1
                    volume = 1000000 + day
                    factor = early_factor if day <= 15 else late_factor
                    rows.append(
                        (
                            "688623",
                            date(2026, 6, day).isoformat(),
                            (cash_close - 0.5) * factor,
                            (cash_close + 1.0) * factor,
                            (cash_close - 1.0) * factor,
                            cash_close * factor,
                            volume,
                            volume * cash_close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with patch.dict(
                os.environ,
                {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": missing_main_path},
            ):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("688623", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 83.0)
        self.assertAlmostEqual(float(df["price_adjustment_factor"].iloc[-1]), late_factor)
        self.assertEqual(df["price_adjustment_basis"].iloc[-1], "amount_volume_vwap_rolling")
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_keeps_sequoia_cash_scale_when_amount_volume_factor_is_one(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.return_value = []
        mock_get_db.return_value = mock_db

        with tempfile.TemporaryDirectory() as tmpdir:
            sequoia_path = os.path.join(tmpdir, "sequoia_v2.db")
            missing_main_path = os.path.join(tmpdir, "missing_stock_analysis.db")
            with sqlite3.connect(sequoia_path) as conn:
                conn.execute(
                    """CREATE TABLE stock_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        turnover REAL
                    )"""
                )
                rows = []
                for day in range(1, 31):
                    close = 120.0 + day
                    volume = 1000000 + day
                    rows.append(
                        (
                            "688981",
                            date(2026, 6, day).isoformat(),
                            close - 1.0,
                            close + 1.0,
                            close - 2.0,
                            close,
                            volume,
                            volume * close,
                        )
                    )
                conn.executemany(
                    "INSERT INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()

            with patch.dict(
                os.environ,
                {"SEQUOIA_CANDIDATE_DB_PATH": sequoia_path, "DATABASE_PATH": missing_main_path},
            ):
                with patch("src.services.history_loader._effective_cache_end", return_value=date(2026, 6, 30)):
                    df, source = load_history_df("688981", days=30, target_date=date(2026, 6, 30))

        self.assertIsNotNone(df)
        self.assertEqual(source, "sequoia_stock_daily")
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 150.0)
        self.assertNotIn("price_adjustment_basis", df.columns)
        mock_get_fm.return_value.get_daily_data.assert_not_called()

    # ------------------------------------------------------------------
    # ContextVar integration
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_uses_frozen_target_date_from_contextvar(self, mock_get_db):
        from src.services.history_loader import (
            load_history_df,
            reset_frozen_target_date,
            set_frozen_target_date,
        )

        frozen_date = date(2026, 4, 15)
        token = set_frozen_target_date(frozen_date)
        try:
            mock_db = MagicMock()
            fake_bar = MagicMock()
            fake_bar.to_dict.return_value = {"date": "2026-04-15", "close": 10}
            mock_db.get_data_range.return_value = [fake_bar] * 30
            mock_get_db.return_value = mock_db

            df, source = load_history_df("600519", days=30)

            self.assertEqual(source, "db_cache")
            call_args = mock_db.get_data_range.call_args
            _code, _start, end = call_args[0]
            self.assertEqual(end, frozen_date)
        finally:
            reset_frozen_target_date(token)

    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_weekend_accepts_latest_completed_trading_day_cache(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        latest_trade_date = date(2026, 5, 15)
        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {"date": latest_trade_date.isoformat(), "close": 10}
        mock_db = MagicMock()
        mock_db.get_data_range.return_value = [fake_bar] * 40
        mock_get_db.return_value = mock_db
        mock_fm = MagicMock()
        mock_get_fm.return_value = mock_fm

        with patch("src.services.history_loader._effective_cache_end", return_value=latest_trade_date):
            df, source = load_history_df("000300", days=260, target_date=date(2026, 5, 17))

        self.assertIsNotNone(df)
        self.assertEqual(source, "db_cache")
        mock_fm.get_daily_data.assert_not_called()

    # ------------------------------------------------------------------
    # normalize_stock_code fallback for prefixed codes
    # ------------------------------------------------------------------
    @patch("src.storage.get_db")
    def test_uses_normalize_fallback_for_prefixed_code(self, mock_get_db):
        from src.services.history_loader import load_history_df

        fake_bar = MagicMock()
        fake_bar.to_dict.return_value = {"date": "2026-04-18", "close": 10}

        mock_db = MagicMock()

        def side_effect(code, start, end):
            if code == "SH600519":
                return []
            return [fake_bar] * 30

        mock_db.get_data_range.side_effect = side_effect
        mock_get_db.return_value = mock_db

        df, source = load_history_df("SH600519", days=30, target_date=date(2026, 4, 18))

        self.assertEqual(source, "db_cache")
        self.assertEqual(mock_db.get_data_range.call_count, 2)

    # ------------------------------------------------------------------
    # Both paths fail gracefully
    # ------------------------------------------------------------------
    @patch("src.services.history_loader._get_fetcher_manager")
    @patch("src.storage.get_db")
    def test_graceful_when_both_fail(self, mock_get_db, mock_get_fm):
        from src.services.history_loader import load_history_df

        mock_db = MagicMock()
        mock_db.get_data_range.side_effect = Exception("DB down")
        mock_get_db.return_value = mock_db

        mock_fm = MagicMock()
        mock_fm.get_daily_data.side_effect = Exception("API down")
        mock_get_fm.return_value = mock_fm

        df, source = load_history_df("600519", days=60, target_date=date(2026, 4, 18))

        self.assertIsNone(df)
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
