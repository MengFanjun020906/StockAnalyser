# -*- coding: utf-8 -*-
"""
Contract tests for get_capital_flow tool output semantics.
"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_capital_flow
from src.agent.tools import data_tools


class _DummyManagerOk:
    """Returns a well-formed capital flow context."""

    def get_capital_flow_context(
        self,
        _stock_code: str,
        budget_seconds=None,
        start_date=None,
        end_date=None,
        page_no=1,
        page_size=50,
    ):
        self.budget_seconds = budget_seconds
        self.kwargs = {
            "start_date": start_date,
            "end_date": end_date,
            "page_no": page_no,
            "page_size": page_size,
        }
        return {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 1500000.0,
                    "main_inflow_5d": 8000000.0,
                    "main_inflow_10d": 15000000.0,
                    "inflow_5d": 8000000.0,
                    "inflow_10d": 15000000.0,
                    "net_inflow": -500000.0,
                    "net_inflow_5d": -3000000.0,
                    "net_inflow_10d": -7000000.0,
                    "amount_unit": "CNY",
                    "raw_amount_unit": "10k CNY",
                    "latest_date": "2026-05-08",
                    "source_update": "after_market_close",
                    "selected_flow_source": "tushare_moneyflow_dc",
                    "flow_sources": {"tushare_moneyflow_dc": {"main_net_inflow": 1500000.0}},
                },
                "sector_rankings": {
                    "top": [{"name": "白酒", "inflow": 5e8}, {"name": "半导体", "inflow": 3e8}],
                    "bottom": [{"name": "煤炭", "inflow": -2e8}],
                },
            },
            "errors": [],
        }


class _DummyManagerNotSupported:
    """Returns not_supported status (e.g. ETF or HK stock)."""

    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None, **_kwargs):
        return {"status": "not_supported"}


class _DummyManagerRaises:
    """Simulates a fetch failure."""

    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None, **_kwargs):
        raise RuntimeError("network timeout")


class _DummyManagerEndpointUnreachable:
    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None, **_kwargs):
        return {
            "status": "failed",
            "data": {"stock_flow": {}, "sector_rankings": {"top": [], "bottom": []}},
            "errors": [
                "tushare_moneyflow:ConnectionError:HTTPConnectionPool(host='118.89.66.41', port=8010)",
            ],
        }


class _DummyManagerEmptyStockAPI:
    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None, **_kwargs):
        return {
            "status": "failed",
            "data": {"stock_flow": {}, "sector_rankings": {"top": [], "bottom": []}},
            "errors": ["stockapi_codeFlow:empty_data"],
            "source_chain": [{"provider": "capital_stock:stockapi_codeFlow", "result": "failed"}],
        }


class _DummyManagerEmptyTushare:
    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None, **_kwargs):
        return {
            "status": "failed",
            "data": {"stock_flow": {}, "sector_rankings": {"top": [], "bottom": []}},
            "errors": ["tushare_moneyflow:empty_data"],
            "source_chain": [{"provider": "capital_stock:tushare_moneyflow", "result": "failed"}],
        }


class TestGetCapitalFlowContract(unittest.TestCase):

    def test_ok_response_shape(self) -> None:
        """Happy path: key fields are present and values match the source data."""
        manager = _DummyManagerOk()
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=manager,
        ), patch("src.config.get_config", return_value=type("Cfg", (), {"agent_capital_flow_timeout_seconds": 4.2})()):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(manager.budget_seconds, 4.2)
        self.assertEqual(result["main_net_inflow"], 1500000.0)
        self.assertEqual(result["main_inflow_5d"], 8000000.0)
        self.assertEqual(result["main_inflow_10d"], 15000000.0)
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertEqual(result["inflow_10d"], 15000000.0)
        self.assertEqual(result["net_inflow"], -500000.0)
        self.assertEqual(result["net_inflow_5d"], -3000000.0)
        self.assertEqual(result["net_inflow_10d"], -7000000.0)
        self.assertEqual(result["amount_unit"], "CNY")
        self.assertEqual(result["raw_amount_unit"], "10k CNY")
        self.assertEqual(result["latest_date"], "2026-05-08")
        self.assertEqual(result["source_update"], "after_market_close")
        self.assertEqual(result["selected_flow_source"], "tushare_moneyflow_dc")
        self.assertIn("tushare_moneyflow_dc", result["flow_sources"])
        self.assertEqual(
            result["query"],
            {"start_date": None, "end_date": None, "page_no": 1, "page_size": 50},
        )
        self.assertEqual(
            manager.kwargs,
            {"start_date": None, "end_date": None, "page_no": 1, "page_size": 50},
        )
        self.assertIn("sector_rankings", result)
        self.assertIn("top_inflow_sectors", result["sector_rankings"])
        self.assertIn("top_outflow_sectors", result["sector_rankings"])
        # At most 3 items are returned per ranking list
        self.assertLessEqual(len(result["sector_rankings"]["top_inflow_sectors"]), 3)
        self.assertEqual(result["errors"], [])

    def test_not_supported_for_non_cn_or_etf(self) -> None:
        """ETF / non-CN stocks return status=not_supported with an explanatory note."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerNotSupported(),
        ):
            result = _handle_get_capital_flow("510300")

        self.assertEqual(result["stock_code"], "510300")
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("note", result)

    def test_exception_path_formatting(self) -> None:
        """Fetch errors are caught and returned with status=error."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerRaises(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "error")
        self.assertIn("capital flow fetch failed", result["error"])
        self.assertIn("network timeout", result["error"])

    def test_explicit_stockapi_window_is_passed_to_manager(self) -> None:
        manager = _DummyManagerOk()
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=manager,
        ), patch("src.config.get_config", return_value=type("Cfg", (), {"agent_capital_flow_timeout_seconds": 4.2})()):
            result = _handle_get_capital_flow(
                "600519",
                start_date="2026-05-15",
                end_date="20260515",
                page_no=2,
                page_size=80,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            manager.kwargs,
            {"start_date": "2026-05-15", "end_date": "2026-05-15", "page_no": 2, "page_size": 80},
        )
        self.assertEqual(result["query"], manager.kwargs)

    def test_invalid_stockapi_window_returns_error(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ):
            result = _handle_get_capital_flow("600519", start_date="2026/05/15")

        self.assertEqual(result["status"], "error")
        self.assertIn("start_date must be YYYY-MM-DD or YYYYMMDD", result["error"])

    def test_tushare_endpoint_failure_has_clear_error_summary(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerEndpointUnreachable(),
        ):
            result = _handle_get_capital_flow("688469")

        self.assertEqual(result["stock_code"], "688469")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_summary"], "Tushare moneyflow capital-flow endpoints failed")
        self.assertIn("tushare_moneyflow", result["errors"][0])

    def test_tushare_empty_has_clear_error_summary(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerEmptyTushare(),
        ):
            result = _handle_get_capital_flow("301028")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_summary"], "Tushare moneyflow endpoints returned no capital-flow rows for the queried window")
        self.assertEqual(result["errors"], ["tushare_moneyflow:empty_data"])

    def test_stockapi_empty_is_reported_when_fallback_also_has_no_rows(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerEmptyStockAPI(),
        ), patch("src.agent.tools.data_tools._query_tushare_stock_moneyflow") as tushare_mock:
            result = _handle_get_capital_flow("301028")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_summary"], "StockAPI codeFlow returned no capital-flow rows for the queried window")
        self.assertEqual(result["errors"], ["stockapi_codeFlow:empty_data"])
        tushare_mock.assert_not_called()

    def test_tushare_moneyflow_fallback_uses_weekday_without_trade_cal(self) -> None:
        seen = {}

        def fake_query(api_name, params=None, fields="", limit=30, timeout=None):
            seen["api_name"] = api_name
            seen["params"] = params
            seen["timeout"] = timeout
            return {
                "status": "ok",
                "items": [
                    {
                        "trade_date": "20260515",
                        "net_mf_amount": 1686.43,
                        "buy_lg_amount": 20.0,
                        "sell_lg_amount": 10.0,
                        "buy_elg_amount": 3.0,
                        "sell_elg_amount": 5.0,
                    },
                    {
                        "trade_date": "20260514",
                        "net_mf_amount": -2774.06,
                        "buy_lg_amount": 8.0,
                        "sell_lg_amount": 12.0,
                        "buy_elg_amount": 1.0,
                        "sell_elg_amount": 2.0,
                    },
                ],
                "source_chain": [{"provider": f"tushare:{api_name}", "result": "ok"}],
                "errors": [],
            }

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 17, 20, 0)

        with patch.object(data_tools, "datetime", _FixedDatetime), \
                patch("src.agent.tools.data_tools._tushare_query", side_effect=fake_query):
            result = data_tools._query_tushare_stock_moneyflow("301028")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen["api_name"], "moneyflow")
        self.assertEqual(seen["params"]["start_date"], "20260425")
        self.assertEqual(seen["params"]["end_date"], "20260515")
        self.assertEqual(seen["timeout"], 3.0)
        self.assertEqual(result["latest_date"], "2026-05-15")

    def test_tushare_moneyflow_uses_explicit_remaining_budget(self) -> None:
        seen = {}

        def fake_query(api_name, params=None, fields="", limit=30, timeout=None):
            seen["timeout"] = timeout
            return {
                "status": "timeout",
                "items": [],
                "source_chain": [{"provider": f"tushare:{api_name}", "result": "timeout"}],
                "errors": ["Tushare SDK call timed out after 1.2s"],
            }

        with patch("src.agent.tools.data_tools._tushare_query", side_effect=fake_query):
            result = data_tools._query_tushare_stock_moneyflow("301028", timeout_seconds=1.2)

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(seen["timeout"], 1.2)

    def test_tushare_moneyflow_returns_timeout_when_budget_exhausted(self) -> None:
        with patch("src.agent.tools.data_tools._tushare_query") as query_mock:
            result = data_tools._query_tushare_stock_moneyflow("301028", timeout_seconds=0)

        self.assertEqual(result["status"], "timeout")
        self.assertIn("budget exhausted", result["errors"][0])
        query_mock.assert_not_called()

    def test_tushare_moneyflow_retries_once_when_first_result_is_empty(self) -> None:
        calls = []

        def fake_query(api_name, params=None, fields="", limit=30, timeout=None):
            calls.append(params)
            if len(calls) == 1:
                return {
                    "status": "empty",
                    "items": [],
                    "source_chain": [{"provider": "tushare:moneyflow", "result": "empty", "params": params}],
                    "errors": [],
                }
            return {
                "status": "ok",
                "items": [{
                    "trade_date": "20260515",
                    "net_mf_amount": 1686.43,
                    "buy_lg_amount": 20.0,
                    "sell_lg_amount": 10.0,
                    "buy_elg_amount": 3.0,
                    "sell_elg_amount": 5.0,
                }],
                "source_chain": [{"provider": "tushare:moneyflow", "result": "ok", "params": params}],
                "errors": [],
            }

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 17, 20, 0)

        with patch.object(data_tools, "datetime", _FixedDatetime), \
                patch("src.agent.tools.data_tools._tushare_query", side_effect=fake_query):
            result = data_tools._query_tushare_stock_moneyflow("301028")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["net_inflow"], 16864300.0)
        self.assertEqual(result["main_net_inflow"], 80000.0)
        self.assertEqual(result["main_inflow_5d"], 80000.0)
        self.assertEqual(result["net_inflow_5d"], 16864300.0)
        self.assertEqual(result["amount_unit"], "CNY")
        self.assertEqual(result["raw_amount_unit"], "10k CNY")
        self.assertEqual(result["source_chain"][0]["result"], "empty")
        self.assertEqual(result["source_chain"][1]["result"], "ok")


if __name__ == "__main__":
    unittest.main()
