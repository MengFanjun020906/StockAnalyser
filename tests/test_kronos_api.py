# -*- coding: utf-8 -*-
"""Kronos API contract tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

import src.auth as auth
from api.app import create_app
from src.config import Config
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class KronosApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "kronos_api.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    "KRONOS_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self._env_patch = patch.dict(
            os.environ,
            {
                "ENV_FILE": str(self.env_path),
                "DATABASE_PATH": str(self.db_path),
                "KRONOS_ENABLED": "false",
            },
        )
        self._env_patch.start()
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.client = TestClient(create_app(static_dir=self.data_dir / "empty-static"))

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self._env_patch.stop()
        self.temp_dir.cleanup()

    def test_status_endpoint_reports_disabled_scope(self) -> None:
        response = self.client.get("/api/v1/kronos/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["market_scope"], ["cn"])
        self.assertEqual(payload["frequency_scope"], ["1d"])

    def test_analyze_endpoint_returns_pattern_payload(self) -> None:
        service = MagicMock()
        service.analyze.return_value = {
            "status": "ok",
            "code": "600519",
            "market": "cn",
            "frequency": "1d",
            "run_id": "kronos-run-1",
            "as_of": "2026-07-24",
            "bars": [
                {"time": "2026-07-23", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.3, "volume": 1000},
            ],
            "forecast": [
                {"time": "2026-07-27", "close": 10.8},
            ],
            "signal": {"bias": "bullish", "confidence": 0.72},
            "forecast_summary": {"expected_return_pct": 4.85},
            "pattern_instances": [
                {
                    "pattern_id": "pattern-1",
                    "pattern_type": "bottom_reversal",
                    "label": "底部反转",
                    "direction": "bullish",
                    "confidence": 0.7,
                    "start_time": "2026-07-10",
                    "end_time": "2026-07-24",
                    "price_low": 9.4,
                    "price_high": 10.6,
                    "red_box": {"x0": "2026-07-10", "x1": "2026-07-24", "y0": 9.4, "y1": 10.6},
                    "evidence": {"forecast": "mock"},
                }
            ],
            "data_quality": {"status": "sufficient", "bar_count": 80},
            "source_chain": ["history_loader:unit", "kronos:unit", "price_structure"],
            "diagnostics": {},
        }

        with patch("api.v1.endpoints.kronos.KronosSignalService", return_value=service):
            response = self.client.post(
                "/api/v1/kronos/signals/analyze",
                json={"stock_code": "600519", "lookback": 80, "pred_len": 5},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["pattern_instances"][0]["red_box"]["x0"], "2026-07-10")
        service.analyze.assert_called_once()
        self.assertEqual(service.analyze.call_args.kwargs["stock_code"], "600519")
        self.assertEqual(service.analyze.call_args.kwargs["frequency"], "1d")


if __name__ == "__main__":
    unittest.main()
