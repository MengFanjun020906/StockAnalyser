# -*- coding: utf-8 -*-
"""Regression tests for TushareFetcher HTTP client initialization."""

import importlib.util
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_client import TushareHttpClient
from data_provider.tushare_fetcher import TushareFetcher


class TestTushareHttpClient(unittest.TestCase):
    """Ensure the lightweight HTTP client preserves Tushare Pro request semantics."""

    def test_query_posts_to_official_pro_endpoint(self) -> None:
        client = TushareHttpClient(token="demo-token", timeout=15, api_url="http://api.tushare.pro")
        response = MagicMock(
            status_code=200,
            text=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "close"],
                        "items": [["600519.SH", 1688.0]],
                    },
                }
            ),
        )

        with patch("data_provider.tushare_client.requests.post", return_value=response) as post_mock:
            df = client.daily(ts_code="600519.SH", start_date="20260320", end_date="20260325")

        post_mock.assert_called_once_with(
            "http://api.tushare.pro/",
            json={
                "api_name": "daily",
                "token": "demo-token",
                "params": {
                    "ts_code": "600519.SH",
                    "start_date": "20260320",
                    "end_date": "20260325",
                },
                "fields": "",
            },
            timeout=15,
        )
        self.assertEqual(df.to_dict(orient="records"), [{"ts_code": "600519.SH", "close": 1688.0}])

    def test_query_uses_env_private_gateway(self) -> None:
        response = MagicMock(
            status_code=200,
            text=json.dumps({"code": 0, "data": {"fields": ["ts_code"], "items": [["000001.SH"]]}}),
        )

        with patch.dict("os.environ", {"TUSHARE_HTTP_URL": "http://118.89.66.41:8010/"}, clear=False), \
                patch("data_provider.tushare_client.requests.post", return_value=response) as post_mock:
            client = TushareHttpClient(token="demo-token", timeout=15)
            client.index_basic(limit=1)

        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.args[0], "http://118.89.66.41:8010/")


class TestTushareFetcherInit(unittest.TestCase):
    """Ensure fetcher initialization no longer depends on the tushare SDK package."""

    def test_init_builds_http_client_when_token_present(self) -> None:
        config = SimpleNamespace(tushare_token="demo-token")

        with patch("data_provider.tushare_fetcher.get_config", return_value=config):
            fetcher = TushareFetcher()

        self.assertIsInstance(fetcher._api, TushareHttpClient)
        self.assertTrue(fetcher.is_available())
        self.assertEqual(fetcher.priority, -1)


if __name__ == "__main__":
    unittest.main()
