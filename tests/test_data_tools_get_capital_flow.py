# -*- coding: utf-8 -*-
"""
Contract tests for get_capital_flow tool output semantics.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_capital_flow


class _DummyManagerOk:
    """Returns a well-formed capital flow context."""

    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None):
        self.budget_seconds = budget_seconds
        return {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 1500000.0,
                    "inflow_5d": 8000000.0,
                    "inflow_10d": 15000000.0,
                    "latest_date": "2026-05-08",
                    "source_update": "after_market_close",
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

    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None):
        return {"status": "not_supported"}


class _DummyManagerRaises:
    """Simulates a fetch failure."""

    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None):
        raise RuntimeError("network timeout")


class _DummyManagerEndpointUnreachable:
    def get_capital_flow_context(self, _stock_code: str, budget_seconds=None):
        return {
            "status": "failed",
            "data": {"stock_flow": {}, "sector_rankings": {"top": [], "bottom": []}},
            "errors": [
                "stockapi_codeFlow:ConnectionError:HTTPSConnectionPool(host='www.stockapi.com.cn')",
            ],
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
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertEqual(result["inflow_10d"], 15000000.0)
        self.assertEqual(result["latest_date"], "2026-05-08")
        self.assertEqual(result["source_update"], "after_market_close")
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

    def test_stockapi_endpoint_failure_has_clear_error_summary(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerEndpointUnreachable(),
        ):
            result = _handle_get_capital_flow("688469")

        self.assertEqual(result["stock_code"], "688469")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_summary"], "StockAPI codeFlow capital-flow endpoint failed")
        self.assertIn("stockapi_codeFlow", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
