# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _STOCKAPI_RESPONSE_CACHE,
    _akshare_fund_flow_market,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
    _stockapi_code_flow_completed_date,
    _stockapi_default_completed_date,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_akshare_fund_flow_market_is_derived_from_code(self) -> None:
        self.assertEqual(_akshare_fund_flow_market("300456"), "sz")
        self.assertEqual(_akshare_fund_flow_market("000001"), "sz")
        self.assertEqual(_akshare_fund_flow_market("600519"), "sh")
        self.assertEqual(_akshare_fund_flow_market("688001"), "sh")
        self.assertEqual(_akshare_fund_flow_market("BJ920748"), "bj")

    def test_stockapi_default_completed_date_uses_1600_cutoff_for_hot_sectors(self) -> None:
        class _MorningDateTime(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 6, 2, 15, 59, 0)

        class _AfternoonDateTime(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 6, 2, 16, 0, 0)

        with patch("data_provider.fundamental_adapter.datetime", _MorningDateTime):
            self.assertEqual(_stockapi_default_completed_date(), "2026-06-01")
        with patch("data_provider.fundamental_adapter.datetime", _AfternoonDateTime):
            self.assertEqual(_stockapi_default_completed_date(), "2026-06-02")

    def test_capital_flow_fails_when_tushare_and_stockapi_both_fail(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch.object(adapter, "_get_tushare_moneyflow_dc_capital_flow", return_value=({}, None, ["tushare_moneyflow_dc:failed"])), \
                patch.object(adapter, "_get_tushare_moneyflow_ths_capital_flow", return_value=({}, None, ["tushare_moneyflow_ths:failed"])), \
                patch.object(adapter, "_get_tushare_capital_flow", return_value=({}, None, ["tushare_moneyflow:failed"])), \
                patch.object(adapter, "_get_stockapi_capital_flow", return_value=({}, None, ["stockapi_codeFlow:failed"])), \
                patch.object(adapter, "_call_df_candidates") as mock_akshare:
            result = adapter.get_capital_flow("300456")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stock_flow"], {})
        self.assertEqual(
            result["errors"],
            ["tushare_moneyflow_dc:failed", "tushare_moneyflow_ths:failed", "tushare_moneyflow:failed", "stockapi_codeFlow:failed"],
        )
        mock_akshare.assert_not_called()

    def test_capital_flow_not_supported_for_non_a_share_without_eastmoney(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch.object(
            adapter,
            "_get_tushare_moneyflow_dc_capital_flow",
            return_value=({}, None, ["tushare_moneyflow_dc:not_supported:HK.00700"]),
        ), patch.object(
            adapter,
            "_get_tushare_moneyflow_ths_capital_flow",
            return_value=({}, None, ["tushare_moneyflow_ths:not_supported:HK.00700"]),
        ), patch.object(
            adapter,
            "_get_tushare_capital_flow",
            return_value=({}, None, ["tushare_moneyflow:not_supported:HK.00700"]),
        ), patch.object(
            adapter,
            "_get_stockapi_capital_flow",
            return_value=({}, None, ["stockapi_codeFlow:not_supported:HK.00700"]),
        ), patch.object(adapter, "_call_df_candidates") as mock_akshare:
            result = adapter.get_capital_flow("HK.00700")

        self.assertEqual(result["status"], "not_supported")
        self.assertEqual(result["stock_flow"], {})
        self.assertEqual(
            result["errors"],
            [
                "tushare_moneyflow_dc:not_supported:HK.00700",
                "tushare_moneyflow_ths:not_supported:HK.00700",
                "tushare_moneyflow:not_supported:HK.00700",
                "stockapi_codeFlow:not_supported:HK.00700",
            ],
        )
        mock_akshare.assert_not_called()

    def test_capital_flow_prefers_tushare_moneyflow_dc_and_keeps_sources(self) -> None:
        adapter = AkshareFundamentalAdapter()
        dc_df = pd.DataFrame([
            {
                "trade_date": "20260507",
                "ts_code": "600004.SH",
                "name": "样例股份",
                "pct_change": 1.2,
                "close": 10.5,
                "net_amount": 200.0,
                "net_amount_rate": 3.5,
                "buy_elg_amount": 80.0,
                "buy_elg_amount_rate": 1.4,
                "buy_lg_amount": 60.0,
                "buy_lg_amount_rate": 1.1,
                "buy_md_amount": 20.0,
                "buy_md_amount_rate": 0.3,
                "buy_sm_amount": -10.0,
                "buy_sm_amount_rate": -0.2,
            },
            {
                "trade_date": "20260508",
                "ts_code": "600004.SH",
                "name": "样例股份",
                "pct_change": 2.0,
                "close": 10.8,
                "net_amount": 300.0,
                "net_amount_rate": 4.5,
                "buy_elg_amount": 100.0,
                "buy_elg_amount_rate": 1.7,
                "buy_lg_amount": 50.0,
                "buy_lg_amount_rate": 0.9,
                "buy_md_amount": 30.0,
                "buy_md_amount_rate": 0.5,
                "buy_sm_amount": -20.0,
                "buy_sm_amount_rate": -0.4,
            },
        ])
        ths_df = pd.DataFrame([
            {
                "trade_date": "20260508",
                "ts_code": "600004.SH",
                "name": "样例股份",
                "pct_change": 2.0,
                "latest": 10.8,
                "net_amount": 88.0,
                "net_d5_amount": 288.0,
                "buy_lg_amount": 66.0,
                "buy_lg_amount_rate": 1.0,
                "buy_md_amount": 11.0,
                "buy_md_amount_rate": 0.2,
                "buy_sm_amount": -3.0,
                "buy_sm_amount_rate": -0.1,
            }
        ])
        legacy_df = pd.DataFrame([
            {
                "trade_date": "20260506",
                "net_mf_amount": 100.0,
                "buy_lg_amount": 20.0,
                "sell_lg_amount": 15.0,
                "buy_elg_amount": 8.0,
                "sell_elg_amount": 3.0,
            },
            {
                "trade_date": "20260507",
                "net_mf_amount": -20.0,
                "buy_lg_amount": 10.0,
                "sell_lg_amount": 12.0,
                "buy_elg_amount": 1.0,
                "sell_elg_amount": 4.0,
            },
            {
                "trade_date": "20260508",
                "net_mf_amount": 30.0,
                "buy_lg_amount": 7.0,
                "sell_lg_amount": 8.0,
                "buy_elg_amount": 5.0,
                "sell_elg_amount": 1.0,
            },
        ])

        calls = []

        def _query(api_name, **_kwargs):
            calls.append(api_name)
            if api_name == "moneyflow_dc":
                return dc_df
            if api_name == "moneyflow_ths":
                return ths_df
            if api_name == "moneyflow":
                return legacy_df
            raise AssertionError(api_name)

        with patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=_query), \
                patch.object(adapter, "_get_stockapi_capital_flow") as stockapi_mock, \
                patch.object(adapter, "_call_df_candidates") as mock_akshare:
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["selected_flow_source"], "tushare_moneyflow_dc")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 3000000.0)
        self.assertEqual(result["stock_flow"]["main_inflow_5d"], 5000000.0)
        self.assertEqual(result["stock_flow"]["main_inflow_10d"], 5000000.0)
        self.assertEqual(result["stock_flow"]["net_inflow"], 1500000.0)
        self.assertEqual(result["stock_flow"]["net_inflow_5d"], 2900000.0)
        self.assertEqual(result["stock_flow"]["net_inflow_10d"], 2900000.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 5000000.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 5000000.0)
        self.assertEqual(result["stock_flow"]["extra_large_net_inflow"], 1000000.0)
        self.assertEqual(result["stock_flow"]["large_net_inflow"], 500000.0)
        self.assertEqual(result["stock_flow"]["amount_unit"], "CNY")
        self.assertEqual(result["stock_flow"]["raw_amount_unit"], "10k CNY")
        self.assertEqual(result["stock_flow"]["latest_date"], "2026-05-08")
        self.assertEqual(result["stock_flow"]["source_update"], "tushare_moneyflow_dc_after_market_close")
        self.assertIn("moneyflow_dc.net_amount", result["stock_flow"]["main_inflow_definition"])
        self.assertIn("tushare_moneyflow_dc", result["stock_flow"]["flow_sources"])
        self.assertNotIn("tushare_moneyflow_ths", result["stock_flow"]["flow_sources"])
        self.assertNotIn("tushare_moneyflow", result["stock_flow"]["flow_sources"])
        self.assertIn("capital_stock:tushare_moneyflow_dc", result["source_chain"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(calls, ["moneyflow_dc"])
        stockapi_mock.assert_not_called()
        mock_akshare.assert_not_called()

    def test_capital_flow_uses_ths_when_dc_is_unavailable(self) -> None:
        adapter = AkshareFundamentalAdapter()
        ths_df = pd.DataFrame([
            {
                "trade_date": "20260508",
                "ts_code": "600004.SH",
                "name": "样例股份",
                "pct_change": 2.0,
                "latest": 10.8,
                "net_amount": 88.0,
                "net_d5_amount": 288.0,
                "buy_lg_amount": 66.0,
                "buy_lg_amount_rate": 1.0,
                "buy_md_amount": 11.0,
                "buy_md_amount_rate": 0.2,
                "buy_sm_amount": -3.0,
                "buy_sm_amount_rate": -0.1,
            }
        ])

        calls = []

        def _query(api_name, **_kwargs):
            calls.append(api_name)
            if api_name == "moneyflow_dc":
                raise RuntimeError("no dc permission")
            if api_name == "moneyflow_ths":
                return ths_df
            if api_name == "moneyflow":
                raise RuntimeError("legacy unavailable")
            raise AssertionError(api_name)

        with patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=_query), \
                patch.object(adapter, "_get_stockapi_capital_flow") as stockapi_mock:
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["selected_flow_source"], "tushare_moneyflow_ths")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 660000.0)
        self.assertEqual(result["stock_flow"]["net_inflow"], 880000.0)
        self.assertEqual(result["stock_flow"]["net_inflow_5d"], 2880000.0)
        self.assertIn("moneyflow_ths.buy_lg_amount", result["stock_flow"]["main_inflow_definition"])
        self.assertIn("tushare_moneyflow_dc:RuntimeError:no dc permission", result["errors"])
        self.assertEqual(calls, ["moneyflow_dc", "moneyflow_ths"])
        stockapi_mock.assert_not_called()

    def test_capital_flow_audit_marks_direction_conflict_without_overriding_selected_source(self) -> None:
        adapter = AkshareFundamentalAdapter()
        selected_flow = {
            "latest_date": "2026-05-08",
            "main_net_inflow": 3_000_000.0,
            "net_inflow": 1_500_000.0,
            "main_inflow_definition": "moneyflow_dc.net_amount * 10000",
            "net_inflow_definition": "(moneyflow_dc.buy_elg_amount + moneyflow_dc.buy_lg_amount) * 10000",
        }
        ths_flow = {
            "latest_date": "2026-05-08",
            "main_net_inflow": -2_000_000.0,
            "net_inflow": -1_200_000.0,
            "main_inflow_definition": "moneyflow_ths.buy_lg_amount * 10000",
            "net_inflow_definition": "moneyflow_ths.net_amount * 10000",
        }

        with patch.object(
            adapter,
            "_get_tushare_moneyflow_ths_capital_flow",
            return_value=(ths_flow, "tushare_moneyflow_ths", []),
        ), patch.object(
            adapter,
            "_get_tushare_capital_flow",
            return_value=({}, None, ["tushare_moneyflow:empty_data"]),
        ), patch.object(adapter, "_get_tushare_moneyflow_dc_capital_flow") as dc_mock:
            result = adapter.audit_capital_flow_sources(
                "600004",
                selected_source="tushare_moneyflow_dc",
                selected_flow=selected_flow,
            )

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["selected_flow_source"], "tushare_moneyflow_dc")
        self.assertIn("tushare_moneyflow_ths", result["checked_sources"])
        self.assertTrue(result["warnings"])
        self.assertTrue(any(item["type"] == "direction_conflict" for item in result["source_conflicts"]))
        self.assertEqual(result["source_summaries"]["tushare_moneyflow_dc"]["main_net_inflow"], 3_000_000.0)
        dc_mock.assert_not_called()

    def test_capital_flow_uses_legacy_moneyflow_when_dc_and_ths_unavailable(self) -> None:
        adapter = AkshareFundamentalAdapter()
        legacy_df = pd.DataFrame([
            {
                "trade_date": "20260506",
                "net_mf_amount": 100.0,
                "buy_lg_amount": 20.0,
                "sell_lg_amount": 15.0,
                "buy_elg_amount": 8.0,
                "sell_elg_amount": 3.0,
            },
            {
                "trade_date": "20260507",
                "net_mf_amount": -20.0,
                "buy_lg_amount": 10.0,
                "sell_lg_amount": 12.0,
                "buy_elg_amount": 1.0,
                "sell_elg_amount": 4.0,
            },
            {
                "trade_date": "20260508",
                "net_mf_amount": 30.0,
                "buy_lg_amount": 7.0,
                "sell_lg_amount": 8.0,
                "buy_elg_amount": 5.0,
                "sell_elg_amount": 1.0,
            },
        ])

        def _query(api_name, **_kwargs):
            if api_name == "moneyflow_dc":
                raise RuntimeError("no dc permission")
            if api_name == "moneyflow_ths":
                raise RuntimeError("no ths permission")
            if api_name == "moneyflow":
                return legacy_df
            raise AssertionError(api_name)

        with patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=_query), \
                patch.object(adapter, "_get_stockapi_capital_flow") as stockapi_mock:
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["selected_flow_source"], "tushare_moneyflow")
        self.assertEqual(result["stock_flow"]["net_inflow"], 300000.0)
        self.assertEqual(result["stock_flow"]["net_inflow_5d"], 1100000.0)
        self.assertEqual(result["stock_flow"]["net_inflow_10d"], 1100000.0)
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 30000.0)
        self.assertEqual(result["stock_flow"]["main_inflow_5d"], 80000.0)
        self.assertEqual(result["stock_flow"]["main_inflow_10d"], 80000.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 80000.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 80000.0)
        self.assertEqual(result["stock_flow"]["amount_unit"], "CNY")
        self.assertEqual(result["stock_flow"]["raw_amount_unit"], "10k CNY")
        self.assertEqual(result["stock_flow"]["latest_date"], "2026-05-08")
        self.assertEqual(result["stock_flow"]["source_update"], "tushare_moneyflow_after_market_close")
        self.assertIn("capital_stock:tushare_moneyflow", result["source_chain"])
        stockapi_mock.assert_not_called()

    def test_capital_flow_falls_back_to_stockapi_when_tushare_fails(self) -> None:
        adapter = AkshareFundamentalAdapter()
        stockapi_payload = {
            "code": 20000,
            "msg": "success",
            "data": [
                {"date": "2026-05-06", "mainAmount": "100.0", "code": "600004"},
                {"date": "2026-05-07", "mainAmount": "-20.0", "code": "600004"},
                {"date": "2026-05-08", "mainAmount": "30.0", "code": "600004"},
            ],
        }

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return stockapi_payload

        with patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch.object(adapter, "_call_df_candidates") as mock_akshare, \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 30.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 110.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 110.0)
        self.assertEqual(result["stock_flow"]["latest_date"], "2026-05-08")
        self.assertIn("capital_stock:stockapi_codeFlow", result["source_chain"])
        self.assertIn("tushare_moneyflow_dc:RuntimeError:sdk timeout", result["errors"])
        self.assertIn("tushare_moneyflow_ths:RuntimeError:sdk timeout", result["errors"])
        self.assertIn("tushare_moneyflow:RuntimeError:sdk timeout", result["errors"])
        mock_akshare.assert_not_called()
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["code"], "600004")
        self.assertEqual(params["pageSize"], "50")

    def test_budgeted_capital_flow_runs_tushare_sources_concurrently_before_stockapi(self) -> None:
        adapter = AkshareFundamentalAdapter()

        def _slow_failure(label):
            def _getter(*_args, **_kwargs):
                time.sleep(0.2)
                return {}, None, [f"{label}:timeout"]
            return _getter

        with patch.object(
            adapter,
            "_get_tushare_moneyflow_dc_capital_flow",
            side_effect=_slow_failure("tushare_moneyflow_dc"),
        ), patch.object(
            adapter,
            "_get_tushare_moneyflow_ths_capital_flow",
            side_effect=_slow_failure("tushare_moneyflow_ths"),
        ), patch.object(
            adapter,
            "_get_tushare_capital_flow",
            side_effect=_slow_failure("tushare_moneyflow"),
        ), patch.object(
            adapter,
            "_get_stockapi_capital_flow",
            return_value=(
                {
                    "main_net_inflow": 30.0,
                    "latest_date": "2026-05-08",
                    "amount_unit": "CNY",
                },
                "stockapi_codeFlow",
                [],
            ),
        ):
            started_at = time.monotonic()
            result = adapter.get_capital_flow("600004", budget_seconds=1.0)
            elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.45)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 30.0)
        self.assertIn("capital_stock:stockapi_codeFlow", result["source_chain"])
        self.assertIn("tushare_moneyflow_dc:timeout", result["errors"])
        self.assertIn("tushare_moneyflow_ths:timeout", result["errors"])
        self.assertIn("tushare_moneyflow:timeout", result["errors"])

    def test_budgeted_capital_flow_passes_remaining_budget_to_stockapi(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch.object(adapter, "_get_tushare_moneyflow_dc_capital_flow", return_value=({}, None, ["dc failed"])), \
                patch.object(adapter, "_get_tushare_moneyflow_ths_capital_flow", return_value=({}, None, ["ths failed"])), \
                patch.object(adapter, "_get_tushare_capital_flow", return_value=({}, None, ["legacy failed"])), \
                patch.object(adapter, "_get_stockapi_capital_flow", return_value=({}, None, ["stockapi timeout"])) as stockapi_mock:
            adapter.get_capital_flow("600004", budget_seconds=5.0)

        self.assertIn("budget_seconds", stockapi_mock.call_args.kwargs)
        self.assertGreaterEqual(stockapi_mock.call_args.kwargs["budget_seconds"], 0.0)
        self.assertLessEqual(stockapi_mock.call_args.kwargs["budget_seconds"], 5.0)

    def test_stockapi_capital_flow_stops_when_budget_is_exhausted(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch("data_provider.fundamental_adapter.requests.get") as mock_get:
            flow, source, errors = adapter._get_stockapi_capital_flow("600004", budget_seconds=0.0)

        self.assertEqual(flow, {})
        self.assertIsNone(source)
        self.assertIn("stockapi_codeFlow:timeout:budget_exhausted", errors)
        mock_get.assert_not_called()

    def test_capital_flow_stockapi_honors_explicit_window_and_page(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [{"date": "2026-05-15", "mainAmount": "100.0", "code": "600004"}],
                }

        with patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_capital_flow(
                "600004",
                start_date="2026-05-15",
                end_date="20260515",
                page_no=2,
                page_size=80,
            )

        self.assertEqual(result["status"], "partial")
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["code"], "600004")
        self.assertEqual(params["startDate"], "2026-05-15")
        self.assertEqual(params["endDate"], "2026-05-15")
        self.assertEqual(params["pageNo"], "2")
        self.assertEqual(params["pageSize"], "80")

    def test_capital_flow_stockapi_rejects_invalid_explicit_window(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch("data_provider.fundamental_adapter.requests.get") as mock_get:
            result = adapter.get_capital_flow(
                "600004",
                start_date="2026-05-16",
                end_date="2026-05-15",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["errors"],
            [
                "tushare_moneyflow_dc:invalid_date:start_date_after_end_date",
                "tushare_moneyflow_ths:invalid_date:start_date_after_end_date",
                "tushare_moneyflow:invalid_date:start_date_after_end_date",
                "stockapi_codeFlow:invalid_date:start_date_after_end_date",
            ],
        )
        mock_get.assert_not_called()

    def test_stockapi_code_flow_completed_date_uses_previous_day_before_1530(self) -> None:
        self.assertEqual(
            _stockapi_code_flow_completed_date(datetime(2026, 5, 15, 15, 29)).isoformat(),
            "2026-05-14",
        )
        self.assertEqual(
            _stockapi_code_flow_completed_date(datetime(2026, 5, 15, 15, 30)).isoformat(),
            "2026-05-15",
        )
        self.assertEqual(
            _stockapi_code_flow_completed_date(datetime(2026, 5, 15, 16, 0)).isoformat(),
            "2026-05-15",
        )
        self.assertEqual(
            _stockapi_code_flow_completed_date(datetime(2026, 5, 17, 20, 0)).isoformat(),
            "2026-05-15",
        )
        self.assertEqual(
            _stockapi_code_flow_completed_date(datetime(2026, 5, 18, 15, 29)).isoformat(),
            "2026-05-15",
        )

    def test_capital_flow_stockapi_token_uses_previous_day_before_1530(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [{"date": "2026-05-14", "mainAmount": "100.0", "code": "301028"}],
                }

        with patch.dict(os.environ, {"STOCKAPI_TOKEN": "test-token"}, clear=False), \
                patch("data_provider.fundamental_adapter.datetime") as mock_datetime, \
                patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            mock_datetime.now.return_value = datetime(2026, 5, 15, 15, 29)
            result = adapter.get_capital_flow("301028")

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(params["endDate"], "2026-05-14")
        self.assertEqual(params["startDate"], "2026-04-24")
        self.assertEqual(params["token"], "test-token")

    def test_capital_flow_stockapi_token_uses_today_at_1530_or_later(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [{"date": "2026-05-15", "mainAmount": "120.0", "code": "301028"}],
                }

        with patch.dict(os.environ, {"STOCKAPI_TOKEN": "test-token"}, clear=False), \
                patch("data_provider.fundamental_adapter.datetime") as mock_datetime, \
                patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            mock_datetime.now.return_value = datetime(2026, 5, 15, 15, 30)
            result = adapter.get_capital_flow("301028")

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(params["endDate"], "2026-05-15")
        self.assertEqual(params["startDate"], "2026-04-25")

    def test_capital_flow_stockapi_token_retries_older_windows_when_latest_empty(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        responses = [
            _Resp({"code": 20000, "msg": "success", "data": []}),
            _Resp({
                "code": 20000,
                "msg": "success",
                "data": [{"date": "2026-04-20", "mainAmount": "88.0", "code": "603418"}],
            }),
        ]

        with patch.dict(os.environ, {"STOCKAPI_TOKEN": "test-token"}, clear=False), \
                patch("data_provider.fundamental_adapter.datetime") as mock_datetime, \
                patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch("data_provider.fundamental_adapter.requests.get", side_effect=responses) as mock_get:
            mock_datetime.now.return_value = datetime(2026, 5, 15, 16, 0)
            result = adapter.get_capital_flow("603418")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["latest_date"], "2026-04-20")
        self.assertEqual(mock_get.call_count, 2)
        windows = [
            (call.kwargs["params"]["startDate"], call.kwargs["params"]["endDate"])
            for call in mock_get.call_args_list
        ]
        self.assertEqual(windows[0], ("2026-04-25", "2026-05-15"))
        self.assertEqual(windows[1], ("2026-04-04", "2026-04-24"))

    def test_capital_flow_splits_stockapi_free_quota_windows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 20000, "msg": "success", "data": []}

        with patch.dict(os.environ, {"STOCKAPI_TOKEN": ""}, clear=False), \
                patch("data_provider.fundamental_adapter.datetime") as mock_datetime, \
                patch("data_provider.fundamental_adapter.query_tushare_api", side_effect=RuntimeError("sdk timeout")), \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            mock_datetime.now.return_value = datetime(2026, 5, 10, 16, 0)
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["errors"],
            [
                "tushare_moneyflow_dc:RuntimeError:sdk timeout",
                "tushare_moneyflow_ths:RuntimeError:sdk timeout",
                "tushare_moneyflow:RuntimeError:sdk timeout",
                "stockapi_codeFlow:empty_data",
            ],
        )
        self.assertEqual(mock_get.call_count, 3)
        windows = [
            (call.kwargs["params"]["startDate"], call.kwargs["params"]["endDate"])
            for call in mock_get.call_args_list
        ]
        self.assertEqual(
            windows,
            [
                ("2026-04-30", "2026-05-04"),
                ("2026-04-25", "2026-04-29"),
                ("2026-04-20", "2026-04-24"),
            ],
        )

    def test_stockapi_limit_up_pool_normalizes_rows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "code": "600001",
                            "name": "测试股",
                            "changeRatio": "10.01",
                            "lastPrice": "12.3",
                            "ceilingAmount": "123456.0",
                            "lbNum": "2",
                            "stock_reason": "机器人订单",
                            "plate_name": "机器人",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_limit_up_pool(date="2026-05-15", limit=5)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["code"], "600001")
        self.assertEqual(result["items"][0]["limit_up_streak"], 2.0)
        self.assertEqual(result["items"][0]["stock_reason"], "机器人订单")
        self.assertEqual(mock_get.call_args.kwargs["params"]["date"], "2026-05-15")

    def test_stockapi_hot_sectors_normalizes_rows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "id": "1",
                            "bkCode": "BK1234",
                            "bkName": "机器人",
                            "qjje": "1000000",
                            "qiangdu": "88.5",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_hot_sectors(date="2026-05-15", limit=5)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sectors"][0]["bk_code"], "BK1234")
        self.assertEqual(result["sectors"][0]["net_inflow"], 1000000.0)
        self.assertIn("/v1/hotBkJlrDr", mock_get.call_args.args[0])

    def test_stockapi_hot_sectors_fallbacks_when_permission_denied(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 60050,
                    "msg": "接口路径或参数不正确，或套餐权限不够",
                    "data": [],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()), \
                patch.object(
                    adapter,
                    "_fallback_hot_sectors",
                    return_value=(
                        [
                            {
                                "id": None,
                                "bk_code": "BK1234",
                                "bk_name": "机器人",
                                "return_pct": 3.2,
                                "return_diff": None,
                                "net_inflow": None,
                                "net_inflow_diff": None,
                                "inflow_days": None,
                                "strength": None,
                                "strength_diff": None,
                                "amount": 120000000.0,
                            },
                            {
                                "id": None,
                                "bk_code": "BK5678",
                                "bk_name": "半导体",
                                "return_pct": 2.1,
                                "return_diff": None,
                                "net_inflow": None,
                                "net_inflow_diff": None,
                                "inflow_days": None,
                                "strength": None,
                                "strength_diff": None,
                                "amount": 90000000.0,
                            },
                        ],
                        [],
                        "akshare:stock_board_industry_name_em",
                    ),
                ):
            result = adapter.get_stockapi_hot_sectors(date="2026-05-15", limit=2)

        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["primary_source"], "stockapi:hotBkJlrDr")
        self.assertEqual(result["fallback_source"], "akshare:stock_board_industry_name_em")
        self.assertIn("60050", result["errors"][0])
        self.assertEqual(result["sectors"][0]["bk_name"], "机器人")
        self.assertEqual(result["sectors"][0]["return_pct"], 3.2)
        self.assertIsNone(result["sectors"][0]["net_inflow"])

    def test_stockapi_sector_constituents_requires_bk_code(self) -> None:
        adapter = AkshareFundamentalAdapter()

        result = adapter.get_stockapi_sector_constituents("")

        self.assertEqual(result["status"], "failed")
        self.assertIn("missing_bk_code", result["errors"][0])

    def test_stockapi_hot_sector_leaders_normalizes_rows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "code": "600001",
                            "name": "测试龙头",
                            "bkCode": "BK1234",
                            "bkName": "机器人",
                            "mainAmount": "1230000",
                            "qiangdu": "91.2",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_hot_sector_leaders(date="2026-05-15", limit=5)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["code"], "600001")
        self.assertEqual(result["items"][0]["bk_code"], "BK1234")
        self.assertEqual(result["items"][0]["main_net_inflow"], 1230000.0)
        self.assertIn("/v1/hotBkJlrLongTou", mock_get.call_args.args[0])

    def test_stockapi_change_all_history_normalizes_rows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "time": "145051",
                            "code": "603398",
                            "name": "测试异动",
                            "type": 8201,
                            "info": "4.67%",
                            "typeName": "火箭发射",
                            "dateId": "2026-05-15",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_change_all_history(
                start_date="2026-05-15",
                end_date="2026-05-15",
                event_type="8201",
                limit=5,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["code"], "603398")
        self.assertEqual(result["items"][0]["event_name"], "火箭发射")
        self.assertIn("/v1/change/allHistory", mock_get.call_args.args[0])
        self.assertEqual(mock_get.call_args.kwargs["params"]["type"], "8201")

    def test_stockapi_popularity_rank_normalizes_rows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "code": "300001",
                            "name": "人气股",
                            "order": "1",
                            "rate": "98.2",
                            "analyse": "市场关注度提升",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_popularity_rank(limit=3)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"][0]["rank"], 1.0)
        self.assertEqual(result["items"][0]["reason"], "市场关注度提升")
        self.assertIn("/v1/change/renQi", mock_get.call_args.args[0])

    def test_stockapi_popularity_rank_retries_rate_limit_error(self) -> None:
        _STOCKAPI_RESPONSE_CACHE.clear()
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        responses = [
            _Resp({"code": 88888, "msg": "每秒钟最大请求次数40次哦,不支持多线程并发请求哦"}),
            _Resp({
                "code": 20000,
                "msg": "success",
                "data": [{"code": "300001", "name": "人气股", "order": "1", "rate": "98.2"}],
            }),
        ]

        with patch("data_provider.fundamental_adapter.time.sleep"), \
                patch("data_provider.fundamental_adapter.requests.get", side_effect=responses) as mock_get:
            result = adapter.get_stockapi_popularity_rank(limit=3)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["items"][0]["code"], "300001")
        self.assertEqual(mock_get.call_count, 2)

    def test_stockapi_popularity_rank_uses_short_cache(self) -> None:
        _STOCKAPI_RESPONSE_CACHE.clear()
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [{"code": "300002", "name": "缓存股", "order": "2", "rate": "88.0"}],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            first = adapter.get_stockapi_popularity_rank(limit=3)
            second = adapter.get_stockapi_popularity_rank(limit=3)

        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["status"], "partial")
        self.assertEqual(second["items"][0]["code"], "300002")
        self.assertEqual(mock_get.call_count, 1)

    def test_stockapi_hot_money_activity_uses_stock_or_rank_mode(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 20000,
                    "msg": "success",
                    "data": [
                        {
                            "rq": "2026-05-15",
                            "gpdm": "600001",
                            "gpmc": "测试股",
                            "yzmc": "知名游资",
                            "yyb": "某营业部",
                            "jlrje": "5000000",
                        }
                    ],
                }

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_stockapi_hot_money_activity(stock_code="SH600001", limit=3)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["mode"], "stock")
        self.assertEqual(result["stock_code"], "600001")
        self.assertEqual(result["items"][0]["net_inflow"], 5000000.0)
        self.assertIn("/v1/youzi/gegu", mock_get.call_args.args[0])

    def test_stockapi_hot_money_rank_fallbacks_to_dragon_tiger_proxy(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "code": 60050,
                    "msg": "接口路径或参数不正确，或套餐权限不够",
                    "data": [],
                }

        fallback_df = pd.DataFrame([
            {
                "上榜日": "2026-05-15",
                "代码": "600001",
                "名称": "测试股",
                "龙虎榜净买额": "5000000",
                "龙虎榜买入额": "8000000",
                "龙虎榜卖出额": "3000000",
                "上榜原因": "日涨幅偏离值达7%",
            }
        ])

        with patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()), \
                patch.object(adapter, "_call_df_candidates", return_value=(fallback_df, "stock_lhb_detail_em", [])):
            result = adapter.get_stockapi_hot_money_activity(limit=3)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["mode"], "rank")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["primary_source"], "stockapi:youziRank")
        self.assertEqual(result["fallback_source"], "akshare:stock_lhb_detail_em")
        self.assertEqual(result["proxy_type"], "dragon_tiger_stock_list")
        self.assertIn("60050", result["errors"][0])
        self.assertEqual(result["items"][0]["code"], "600001")
        self.assertEqual(result["items"][0]["net_inflow"], 5000000.0)
        self.assertEqual(result["items"][0]["hot_money_name"], "")

    def test_market_capital_flow_normalizes_market_and_rankings(self) -> None:
        adapter = AkshareFundamentalAdapter()
        seen = []

        market_df = pd.DataFrame(
            {
                "日期": ["2026-05-07"],
                "主力净流入": [100.0],
                "超大单净流入": [60.0],
                "大单净流入": [40.0],
                "中单净流入": [20.0],
                "小单净流入": [-20.0],
            }
        )
        ranking_df = pd.DataFrame(
            {
                "板块名称": ["白酒", "半导体", "煤炭"],
                "主力净流入": [300.0, 200.0, -100.0],
            }
        )

        def _fake_call_df_candidates(candidates):
            seen.append(candidates)
            if len(seen) == 1:
                return market_df, "stock_market_fund_flow", []
            if len(seen) in (2, 3, 4):
                return ranking_df, "stock_fund_flow_individual", []
            return None, None, []

        with patch.object(adapter, "_call_df_candidates", side_effect=_fake_call_df_candidates):
            result = adapter.get_market_capital_flow(top_n=2)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["market_flow"]["main_net_inflow"], 100.0)
        self.assertEqual(len(result["individual_rankings"]["top"]), 2)
        self.assertEqual(result["individual_rankings"]["top"][0]["name"], "白酒")
        self.assertTrue(result["source_chain"])

    def test_northbound_capital_flow_returns_summary_and_history(self) -> None:
        adapter = AkshareFundamentalAdapter()
        summary_df = pd.DataFrame(
            {
                "日期": ["2026-05-07"],
                "北向资金": [1200.0],
                "南向资金": [-200.0],
            }
        )
        hist_df = pd.DataFrame(
            {
                "日期": ["2026-05-07", "2026-05-06"],
                "净买入": [100.0, -50.0],
                "买入": [200.0, 150.0],
                "卖出": [100.0, 200.0],
            }
        )
        seen = []

        def _fake_call_df_candidates(candidates):
            seen.append(candidates)
            if len(seen) == 1:
                return summary_df, "stock_hsgt_fund_flow_summary_em", []
            if len(seen) == 2:
                return hist_df, "stock_hsgt_hist_em", []
            return None, None, []

        with patch.object(adapter, "_call_df_candidates", side_effect=_fake_call_df_candidates):
            result = adapter.get_northbound_capital_flow(limit=2)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["northbound_net_inflow"], 1200.0)
        self.assertEqual(len(result["history"]), 2)
        self.assertTrue(result["source_chain"])
        self.assertTrue(result["data_quality"]["core_numeric_available"])

    def test_northbound_capital_flow_marks_rows_without_numeric_fields(self) -> None:
        adapter = AkshareFundamentalAdapter()
        summary_df = pd.DataFrame({"日期": ["2026-05-07"], "说明": ["北向资金暂无数据"]})
        hist_df = pd.DataFrame({"日期": ["2026-05-06", "2026-05-07"], "状态": ["休市", "暂无"]})
        seen = []

        def _fake_call_df_candidates(candidates):
            seen.append(candidates)
            if len(seen) == 1:
                return summary_df, "stock_hsgt_fund_flow_summary_em", []
            if len(seen) == 2:
                return hist_df, "stock_hsgt_hist_em", []
            return None, None, []

        with patch.object(adapter, "_call_df_candidates", side_effect=_fake_call_df_candidates):
            result = adapter.get_northbound_capital_flow(limit=2)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["data_quality"]["core_numeric_available"])
        self.assertEqual(result["data_quality"]["history_rows"], 2)
        self.assertEqual(result["data_quality"]["history_rows_with_numeric"], 0)
        self.assertIn(
            "northbound_source_returned_rows_but_no_numeric_flow_fields",
            result["warnings"],
        )

    def test_margin_trading_summary_returns_exchange_data(self) -> None:
        adapter = AkshareFundamentalAdapter()
        account_df = pd.DataFrame(
            {
                "日期": ["2026-05-07"],
                "融资余额": [900.0],
                "融券余额": [100.0],
                "融资融券余额": [1000.0],
            }
        )
        sse_df = pd.DataFrame(
            {
                "日期": ["2026-05-07", "2026-05-06"],
                "融资余额": [800.0, 700.0],
                "融资买入额": [120.0, 110.0],
                "融资偿还额": [80.0, 90.0],
                "融券余额": [50.0, 55.0],
            }
        )
        szse_df = pd.DataFrame(
            {
                "日期": ["2026-05-07"],
                "融资余额": [600.0],
                "融资买入额": [90.0],
                "融资偿还额": [70.0],
                "融券余额": [30.0],
            }
        )
        seen = []

        def _fake_call_df_candidates(candidates):
            seen.append(candidates)
            if len(seen) == 1:
                return account_df, "stock_margin_account_info", []
            if len(seen) == 2:
                return sse_df, "stock_margin_sse", []
            if len(seen) == 3:
                return szse_df, "stock_margin_szse", []
            return None, None, []

        with patch.object(adapter, "_call_df_candidates", side_effect=_fake_call_df_candidates):
            result = adapter.get_margin_trading_summary(limit=2)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["account_info"]["margin_balance"], 1000.0)
        self.assertEqual(len(result["sse"]), 2)
        self.assertEqual(len(result["szse"]), 1)
        self.assertTrue(result["source_chain"])

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        quick_df = pd.DataFrame({"股票代码": ["600519"], "快报": ["快报摘要"]})
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (fin_df, "stock_financial_abstract", []),
                (forecast_df, "stock_yjyg_em", []),
                (quick_df, "stock_yjkb_em", []),
                (dividend_df, "stock_fhps_detail_em", []),
                (None, None, []),
                (None, None, []),
            ],
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)


if __name__ == "__main__":
    unittest.main()
