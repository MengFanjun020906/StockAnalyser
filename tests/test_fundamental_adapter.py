# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _akshare_fund_flow_market,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
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

    def test_capital_flow_does_not_fallback_to_eastmoney_when_stockapi_fails(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch.object(adapter, "_get_stockapi_capital_flow", return_value=({}, None, ["stockapi_codeFlow:failed"])), \
                patch.object(adapter, "_call_df_candidates") as mock_akshare:
            result = adapter.get_capital_flow("300456")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stock_flow"], {})
        self.assertEqual(result["errors"], ["stockapi_codeFlow:failed"])
        mock_akshare.assert_not_called()

    def test_capital_flow_not_supported_for_non_a_share_without_eastmoney(self) -> None:
        adapter = AkshareFundamentalAdapter()

        with patch.object(
            adapter,
            "_get_stockapi_capital_flow",
            return_value=({}, None, ["stockapi_codeFlow:not_supported:HK.00700"]),
        ), patch.object(adapter, "_call_df_candidates") as mock_akshare:
            result = adapter.get_capital_flow("HK.00700")

        self.assertEqual(result["status"], "not_supported")
        self.assertEqual(result["stock_flow"], {})
        self.assertEqual(result["errors"], ["stockapi_codeFlow:not_supported:HK.00700"])
        mock_akshare.assert_not_called()

    def test_capital_flow_prefers_stockapi_code_flow(self) -> None:
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

        with patch.object(adapter, "_call_df_candidates") as mock_akshare, \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 30.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 110.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 110.0)
        self.assertEqual(result["stock_flow"]["latest_date"], "2026-05-08")
        self.assertIn("capital_stock:stockapi_codeFlow", result["source_chain"])
        self.assertEqual(result["errors"], [])
        mock_akshare.assert_not_called()
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["code"], "600004")
        self.assertEqual(params["pageSize"], "20")

    def test_capital_flow_splits_stockapi_free_quota_windows(self) -> None:
        adapter = AkshareFundamentalAdapter()

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": 20000, "msg": "success", "data": []}

        with patch.dict(os.environ, {"STOCKAPI_TOKEN": ""}, clear=False), \
                patch("data_provider.fundamental_adapter.datetime") as mock_datetime, \
                patch("data_provider.fundamental_adapter.requests.get", return_value=_Resp()) as mock_get:
            mock_datetime.now.return_value.date.return_value = datetime(2026, 5, 10).date()
            result = adapter.get_capital_flow("600004")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], ["stockapi_codeFlow:empty_data"])
        self.assertEqual(mock_get.call_count, 3)
        windows = [
            (call.kwargs["params"]["startDate"], call.kwargs["params"]["endDate"])
            for call in mock_get.call_args_list
        ]
        self.assertEqual(
            windows,
            [
                ("2026-05-02", "2026-05-06"),
                ("2026-04-27", "2026-05-01"),
                ("2026-04-22", "2026-04-26"),
            ],
        )

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
