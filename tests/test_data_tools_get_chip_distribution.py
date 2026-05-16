# -*- coding: utf-8 -*-
"""Contract tests for get_chip_distribution tool diagnostics."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.realtime_types import ChipDistribution
from src.agent.tools.data_tools import _handle_get_chip_distribution


class _DummyManagerOk:
    def get_chip_distribution_context(self, _stock_code: str):
        return {
            "stock_code": "600519",
            "status": "ok",
            "data": ChipDistribution(
                code="600519",
                date="2026-04-30",
                profit_ratio=0.67,
                avg_cost=1680.5,
                concentration_90=0.12,
            ),
            "source_chain": [{"provider": "akshare_chip", "result": "ok", "duration_ms": 120}],
            "errors": [],
        }


class _DummyManagerFailed:
    def get_chip_distribution_context(self, _stock_code: str):
        return {
            "stock_code": "688469",
            "status": "failed",
            "data": None,
            "error_summary": "Eastmoney chip distribution endpoint disconnected",
            "errors": [
                "akshare_chip:ConnectionError:RemoteDisconnected",
                "tushare_chip:Exception:抱歉，您没有接口(trade_cal)访问权限",
            ],
            "source_chain": [
                {"provider": "akshare_chip", "result": "failed", "duration_ms": 300},
                {"provider": "tushare_chip", "result": "failed", "duration_ms": 20},
            ],
        }


class _DummyManagerDisabled:
    def get_chip_distribution_context(self, _stock_code: str):
        return {
            "stock_code": "600519",
            "status": "disabled",
            "data": None,
            "error_summary": "chip distribution disabled",
            "errors": ["ENABLE_CHIP_DISTRIBUTION=false"],
            "source_chain": [{"provider": "chip_distribution", "result": "disabled", "duration_ms": 0}],
        }


class _DummyManagerTimeout:
    def _run_with_timeout(self, task, timeout_seconds, task_name):
        return None, f"{task_name} timeout", int(float(timeout_seconds) * 1000)

    def get_chip_distribution_context(self, _stock_code: str):
        raise AssertionError("bounded wrapper should synthesize timeout before using this result")


class TestGetChipDistributionContract(unittest.TestCase):
    def test_ok_response_shape(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ):
            result = _handle_get_chip_distribution("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["profit_ratio"], 0.67)
        self.assertEqual(result["avg_cost"], 1680.5)

    def test_failed_response_preserves_source_diagnostics(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerFailed(),
        ), patch("src.agent.tools.data_tools._query_tushare_chip_distribution", return_value={"status": "failed", "errors": [], "source_chain": []}):
            result = _handle_get_chip_distribution("688469")

        self.assertEqual(result["stock_code"], "688469")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_summary"], "Eastmoney chip distribution endpoint disconnected")
        self.assertIn("RemoteDisconnected", result["errors"][0])
        self.assertEqual(result["source_chain"][0]["provider"], "akshare_chip")
        self.assertIsNone(result["profit_ratio"])

    def test_disabled_response_is_structured(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerDisabled(),
        ), patch("src.agent.tools.data_tools._query_tushare_chip_distribution", return_value={"status": "failed", "errors": [], "source_chain": []}):
            result = _handle_get_chip_distribution("600519")

        self.assertEqual(result["status"], "disabled")
        self.assertIn("ENABLE_CHIP_DISTRIBUTION=false", result["errors"])
        self.assertEqual(result["source_chain"][0]["result"], "disabled")

    def test_timeout_response_is_structured(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerTimeout(),
        ), patch("src.agent.tools.data_tools._query_tushare_chip_distribution", return_value={"status": "failed", "errors": [], "source_chain": []}):
            result = _handle_get_chip_distribution("603418")

        self.assertEqual(result["status"], "timeout")
        self.assertIn("chip_distribution timeout", result["errors"])
        self.assertEqual(result["source_chain"][0]["result"], "timeout")
        self.assertIsNone(result["profit_ratio"])


if __name__ == "__main__":
    unittest.main()
