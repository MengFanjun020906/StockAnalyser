# -*- coding: utf-8 -*-
"""Contract tests for get_chip_distribution tool diagnostics."""

import os
import sys
import time
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.realtime_types import ChipDistribution
from src.agent.tools import data_tools
from src.agent.tools.data_tools import _handle_get_chip_distribution, _query_tushare_chip_distribution


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


class _DummyManagerCaptureTimeout:
    def __init__(self) -> None:
        self.timeout_seconds = None

    def _run_with_timeout(self, task, timeout_seconds, task_name):
        self.timeout_seconds = timeout_seconds
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

    def test_timeout_response_preserves_fast_path_diagnostics(self) -> None:
        fast_result = {
            "status": "failed",
            "errors": ["tushare:cyq_chips timed out"],
            "source_chain": [{"provider": "tushare:cyq_chips", "result": "timeout"}],
        }
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerTimeout(),
        ), patch("src.agent.tools.data_tools._query_tushare_chip_distribution", return_value=fast_result):
            result = _handle_get_chip_distribution("603418")

        self.assertEqual(result["status"], "timeout")
        self.assertIn("tushare:cyq_chips timed out", result["errors"])
        self.assertEqual(result["source_chain"][0]["provider"], "tushare:cyq_chips")
        self.assertEqual(result["source_chain"][-1]["provider"], "chip_distribution")

    def test_tushare_fast_path_has_hard_tool_boundary_timeout(self) -> None:
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerTimeout(),
        ), patch(
            "src.agent.tools.data_tools._query_tushare_chip_distribution",
            side_effect=lambda _code: (time.sleep(0.2) or {"status": "ok"}),
        ), patch("src.agent.tools.data_tools._get_agent_timeout_attr", return_value=0.05):
            result = _handle_get_chip_distribution("603418")

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["source_chain"][0]["provider"], "tushare:cyq_chips")
        self.assertEqual(result["source_chain"][0]["result"], "timeout")

    def test_manager_fallback_uses_remaining_timeout_budget(self) -> None:
        manager = _DummyManagerCaptureTimeout()
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=manager,
        ), patch(
            "src.agent.tools.data_tools._query_tushare_chip_distribution",
            side_effect=lambda _code: (time.sleep(0.03) or {"status": "failed", "errors": [], "source_chain": []}),
        ), patch("src.agent.tools.data_tools._get_agent_timeout_attr", return_value=0.05):
            result = _handle_get_chip_distribution("603418")

        self.assertEqual(result["status"], "timeout")
        self.assertIsNotNone(manager.timeout_seconds)
        self.assertLess(manager.timeout_seconds, 0.05)

    def test_tushare_fast_path_falls_back_to_recent_trade_date(self) -> None:
        responses = [
            {
                "status": "empty",
                "items": [],
                "source_chain": [{
                    "provider": "tushare:cyq_chips",
                    "result": "empty",
                    "params": {"start_date": "20260515", "end_date": "20260515"},
                }],
                "errors": [],
            },
            {
                "status": "ok",
                "items": [
                    {"price": 10, "percent": 25},
                    {"price": 12, "percent": 75},
                ],
                "source_chain": [{
                    "provider": "tushare:cyq_chips",
                    "result": "ok",
                    "params": {"start_date": "20260514", "end_date": "20260514"},
                }],
                "errors": [],
            },
            {
                "status": "ok",
                "items": [{"close": 11}],
                "source_chain": [{
                    "provider": "tushare:daily",
                    "result": "ok",
                    "params": {"start_date": "20260514", "end_date": "20260514"},
                }],
                "errors": [],
            },
        ]

        with patch(
            "src.agent.tools.data_tools._recent_weekday_dates",
            return_value=["20260515", "20260514"],
        ), patch("src.agent.tools.data_tools._tushare_query", side_effect=responses):
            result = _query_tushare_chip_distribution("301028")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date"], "2026-05-14")
        self.assertEqual(result["profit_ratio"], 0.25)
        self.assertEqual(result["avg_cost"], 11.5)
        self.assertEqual(result["source_chain"][0]["result"], "empty")
        self.assertEqual(result["source_chain"][-1]["trade_date"], "20260514")

    def test_tushare_query_rounds_timeout_up_instead_of_down(self) -> None:
        seen = {}

        def fake_query(api_name, params=None, fields="", timeout=None):
            seen["timeout"] = timeout
            return pd.DataFrame()

        with patch("data_provider.tushare_client.query_tushare_api", side_effect=fake_query):
            result = data_tools._tushare_query("daily", {"ts_code": "600000.SH"}, "ts_code", timeout=1.2)

        self.assertEqual(result["status"], "empty")
        self.assertEqual(seen["timeout"], 2)

    def test_tushare_chip_fast_path_uses_full_chip_timeout_budget(self) -> None:
        seen = {}

        def fake_query(api_name, params=None, fields="", limit=30, timeout=None):
            seen.setdefault("timeouts", []).append((api_name, timeout))
            if api_name == "cyq_chips":
                return {
                    "status": "ok",
                    "items": [{"price": 10, "percent": 40}, {"price": 12, "percent": 60}],
                    "source_chain": [{"provider": "tushare:cyq_chips", "result": "ok"}],
                    "errors": [],
                }
            return {
                "status": "ok",
                "items": [{"close": 11}],
                "source_chain": [{"provider": "tushare:daily", "result": "ok"}],
                "errors": [],
            }

        with patch("src.agent.tools.data_tools._recent_weekday_dates", return_value=["20260515"]), \
                patch("src.agent.tools.data_tools._tushare_query", side_effect=fake_query), \
                patch("src.agent.tools.data_tools._get_agent_timeout_attr", side_effect=lambda name, default: 3.0 if name == "agent_chip_distribution_timeout_seconds" else 5.0):
            result = _query_tushare_chip_distribution("600000")

        self.assertEqual(result["status"], "ok")
        self.assertIn(("cyq_chips", 3.0), seen["timeouts"])
        self.assertIn(("daily", 3.0), seen["timeouts"])

    def test_tushare_chip_fast_path_ignores_generic_tushare_timeout_cap(self) -> None:
        seen = {}

        def fake_query(api_name, params=None, fields="", limit=30, timeout=None):
            seen.setdefault("timeouts", []).append((api_name, timeout))
            if api_name == "cyq_chips":
                return {
                    "status": "ok",
                    "items": [{"price": 10, "percent": 40}, {"price": 12, "percent": 60}],
                    "source_chain": [{"provider": "tushare:cyq_chips", "result": "ok"}],
                    "errors": [],
                }
            return {
                "status": "ok",
                "items": [{"close": 11}],
                "source_chain": [{"provider": "tushare:daily", "result": "ok"}],
                "errors": [],
            }

        with patch("src.agent.tools.data_tools._recent_weekday_dates", return_value=["20260515"]), \
                patch("src.agent.tools.data_tools._tushare_query", side_effect=fake_query), \
                patch("src.agent.tools.data_tools._get_agent_timeout_attr", side_effect=lambda name, default: 8.0 if name == "agent_chip_distribution_timeout_seconds" else 5.0):
            result = _query_tushare_chip_distribution("600000")

        self.assertEqual(result["status"], "ok")
        self.assertIn(("cyq_chips", 8.0), seen["timeouts"])
        self.assertIn(("daily", 8.0), seen["timeouts"])


if __name__ == "__main__":
    unittest.main()
