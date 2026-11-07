# -*- coding: utf-8 -*-
"""Kronos K-line pattern signal service tests."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any, Dict
from unittest.mock import patch

import pandas as pd

from src.services.kronos_inference_service import KronosInferenceResult
from src.services.kronos_signal_service import KronosSignalService


def _daily_bars(count: int = 90) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-07-24", periods=count)
    rows = []
    for idx, ts in enumerate(dates):
        if idx < count - 24:
            close = 10.0 + idx * 0.018
        elif idx < count - 6:
            close = 11.2 - (idx - (count - 24)) * 0.085
        else:
            close = 9.65 + (idx - (count - 6)) * 0.24
        open_ = close * (0.992 if idx % 2 == 0 else 1.006)
        high = max(open_, close) * 1.018
        low = min(open_, close) * 0.982
        volume = 1_000_000 + idx * 13_000
        rows.append(
            {
                "code": "600519",
                "date": ts.date(),
                "open": round(open_, 3),
                "high": round(high, 3),
                "low": round(low, 3),
                "close": round(close, 3),
                "volume": volume,
                "amount": round(volume * close, 2),
            }
        )
    return pd.DataFrame(rows)


def _double_bottom_bars() -> pd.DataFrame:
    closes = (
        [112, 110, 108, 106, 104, 101, 98, 95, 92, 90]
        + [93, 96, 100, 103, 105, 104, 101, 98, 95, 92, 91]
        + [93, 96, 99, 102, 104, 105, 106, 107, 108]
    )
    dates = pd.bdate_range(end="2026-07-24", periods=len(closes))
    return pd.DataFrame(
        [
            {
                "code": "600519",
                "date": ts.date(),
                "open": close * (0.998 if idx % 2 == 0 else 1.002),
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000 + idx * 10_000,
                "amount": close * (1_000_000 + idx * 10_000),
            }
            for idx, (ts, close) in enumerate(zip(dates, closes))
        ]
    )


class _DisabledInference:
    def is_enabled(self) -> bool:
        return False

    def status(self) -> Dict[str, Any]:
        return {"enabled": False, "reason": "disabled_by_config"}


class _BullishInference:
    def is_enabled(self) -> bool:
        return True

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "model_name": "unit-kronos"}

    def predict_daily(self, prepared, *, pred_len: int) -> KronosInferenceResult:
        last_close = float(prepared.bars_df["close"].iloc[-1])
        dates = pd.bdate_range(start=pd.Timestamp(prepared.as_of) + pd.Timedelta(days=1), periods=pred_len)
        closes = [last_close * (1.012 + i * 0.014) for i in range(pred_len)]
        forecast = pd.DataFrame(
            {
                "open": [value * 0.996 for value in closes],
                "high": [value * 1.025 for value in closes],
                "low": [value * 0.988 for value in closes],
                "close": closes,
                "volume": [1_500_000 + i * 10_000 for i in range(pred_len)],
                "amount": [1_500_000 * value for value in closes],
            },
            index=dates,
        )
        return KronosInferenceResult(
            status="ok",
            forecast_df=forecast,
            model_profile={"model_name": "unit-kronos", "max_context": 512},
            diagnostics={"mocked": True},
        )


class KronosSignalServiceTestCase(unittest.TestCase):
    def test_disabled_model_returns_unavailable_without_loading_history(self) -> None:
        service = KronosSignalService(inference_service=_DisabledInference())

        with patch("src.services.kronos_data_adapter.load_history_df") as loader:
            result = service.analyze("600519", lookback=80, pred_len=5)

        loader.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "model_disabled")
        self.assertEqual(result["code"], "600519")

    def test_rejects_non_a_share_symbols(self) -> None:
        service = KronosSignalService(inference_service=_BullishInference())

        result = service.analyze("AAPL", lookback=80, pred_len=5)

        self.assertEqual(result["status"], "unsupported_market")
        self.assertEqual(result["market"], "unsupported")

    def test_insufficient_history_returns_structured_data_quality(self) -> None:
        service = KronosSignalService(inference_service=_BullishInference())
        with patch(
            "src.services.kronos_data_adapter.load_history_df",
            return_value=(_daily_bars(12), "unit_test"),
        ):
            result = service.analyze("SH600519", lookback=80, pred_len=5, fallback_to_network=False)

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["code"], "600519")
        self.assertEqual(result["data_quality"]["bar_count"], 12)
        self.assertGreaterEqual(result["data_quality"]["required_bars"], 30)
        self.assertEqual(result["pattern_instances"], [])

    def test_mocked_forecast_builds_bullish_pattern_with_history_box(self) -> None:
        service = KronosSignalService(inference_service=_BullishInference())
        bars = _daily_bars(90)

        with patch("src.services.kronos_data_adapter.load_history_df", return_value=(bars, "unit_test")):
            result = service.analyze("SH600519", lookback=80, pred_len=5, target_date=date(2026, 7, 24), fallback_to_network=False)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "600519")
        self.assertEqual(result["frequency"], "1d")
        self.assertEqual(result["signal"]["bias"], "bullish")
        self.assertGreater(result["forecast_summary"]["expected_return_pct"], 0)
        self.assertEqual(len(result["forecast"]), 5)
        self.assertGreaterEqual(len(result["bars"]), 80)

        patterns = result["pattern_instances"]
        self.assertTrue(patterns)
        first = patterns[0]
        self.assertIn(first["pattern_type"], {"bottom_reversal", "volume_price_repair", "breakout_pullback"})
        self.assertEqual(first["direction"], "bullish")
        self.assertLessEqual(first["end_time"], result["as_of"])
        self.assertIn("red_box", first)
        self.assertEqual(first["red_box"]["x0"], first["start_time"])
        self.assertEqual(first["red_box"]["x1"], first["end_time"])
        self.assertLess(first["red_box"]["y0"], first["red_box"]["y1"])
        self.assertIn("price_structure", result["source_chain"])

    def test_deterministic_double_bottom_is_merged_with_kronos_confirmation(self) -> None:
        service = KronosSignalService(inference_service=_BullishInference())
        bars = _double_bottom_bars()

        with patch("src.services.kronos_data_adapter.load_history_df", return_value=(bars, "unit_test")):
            result = service.analyze("600519", lookback=80, pred_len=5, target_date=date(2026, 7, 24), fallback_to_network=False)

        self.assertEqual(result["status"], "ok")
        self.assertIn("kline_pattern_detector", result["source_chain"])
        pattern_by_type = {item["pattern_type"]: item for item in result["pattern_instances"]}
        self.assertIn("double_bottom", pattern_by_type)
        double_bottom = pattern_by_type["double_bottom"]
        self.assertEqual(double_bottom["label"], "W底")
        self.assertEqual(double_bottom["direction"], "bullish")
        self.assertEqual(double_bottom["evidence"]["detector"], "deterministic_kline_patterns_v1")
        self.assertEqual(double_bottom["evidence"]["kronos_confirmation"], "kronos_confirmed")


if __name__ == "__main__":
    unittest.main()
