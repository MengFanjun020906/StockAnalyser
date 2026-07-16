# -*- coding: utf-8 -*-
"""Regression tests for TushareFetcher HTTP client initialization."""

import importlib.util
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from concurrent.futures import ThreadPoolExecutor

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_client import (
    TushareCredentialError,
    TushareHttpClient,
    TushareQueryTimeout,
    get_tushare_runtime_health,
    get_tushare_http_url,
    query_tushare_api,
    reset_tushare_runtime_health,
    should_bypass_proxy_for_tushare_url,
)
from data_provider.tushare_fetcher import TushareFetcher


class TestTushareHttpClient(unittest.TestCase):
    """Ensure the lightweight HTTP client preserves Tushare Pro request semantics."""

    def setUp(self) -> None:
        reset_tushare_runtime_health()

    def test_default_endpoint_is_private_gateway(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_tushare_http_url(), "http://118.89.66.41:8010/")

    def test_query_posts_to_default_private_gateway(self) -> None:
        client = TushareHttpClient(token="demo-token", timeout=15)
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
            "http://118.89.66.41:8010/",
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
            proxies={"http": None, "https": None},
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
        self.assertEqual(post_mock.call_args.kwargs["proxies"], {"http": None, "https": None})

    def test_bypasses_proxy_for_numeric_private_gateway_ip(self) -> None:
        self.assertTrue(should_bypass_proxy_for_tushare_url("http://121.40.135.59:8010/"))

    def test_query_keeps_proxy_for_non_tushare_custom_endpoint(self) -> None:
        client = TushareHttpClient(token="demo-token", timeout=15, api_url="https://example.com/tushare")
        response = MagicMock(
            status_code=200,
            text=json.dumps({"code": 0, "data": {"fields": ["ts_code"], "items": [["000001.SH"]]}}),
        )

        with patch("data_provider.tushare_client.requests.post", return_value=response) as post_mock:
            client.index_basic(limit=1)

        self.assertNotIn("proxies", post_mock.call_args.kwargs)

    def test_expired_credential_is_quarantined_after_first_response(self) -> None:
        client = TushareHttpClient(token="expired-token", timeout=15)
        response = MagicMock(
            status_code=200,
            text=json.dumps({"code": 2002, "msg": "token已过期", "data": {}}),
        )

        with patch("data_provider.tushare_client.requests.post", return_value=response) as post_mock:
            with self.assertRaises(TushareCredentialError):
                client.index_basic(limit=1)
            with self.assertRaises(TushareCredentialError):
                client.index_basic(limit=1)

        self.assertEqual(post_mock.call_count, 1)
        health = get_tushare_runtime_health(token="expired-token", api_url=client.api_url)
        self.assertEqual(health["status"], "credential_invalid")
        self.assertFalse(health["available"])

    def test_query_tushare_api_prefers_sdk_endpoint_override(self) -> None:
        pro = MagicMock()
        pro.index_basic.return_value = "sdk-result"
        ts = MagicMock()

        with patch("data_provider.tushare_client.build_tushare_sdk_client", return_value=(ts, pro)), \
                patch("data_provider.tushare_client.build_tushare_http_client") as http_mock:
            result = query_tushare_api("index_basic", params={"limit": 5}, timeout=15)

        self.assertEqual(result, "sdk-result")
        pro.index_basic.assert_called_once_with(limit=5)
        http_mock.assert_not_called()

    def test_query_tushare_api_falls_back_to_http_client_when_sdk_fails(self) -> None:
        http_client = MagicMock()
        http_client.query.return_value = "http-result"

        with patch("data_provider.tushare_client.query_tushare_api_via_sdk", side_effect=RuntimeError("sdk down")), \
                patch("data_provider.tushare_client.build_tushare_http_client", return_value=http_client):
            result = query_tushare_api("index_basic", params={"limit": 5}, fields="ts_code", timeout=15)

        self.assertEqual(result, "http-result")
        http_client.query.assert_called_once_with("index_basic", fields="ts_code", limit=5)

    def test_query_tushare_api_does_not_retry_http_after_sdk_auth_failure(self) -> None:
        with patch(
            "data_provider.tushare_client.query_tushare_api_via_sdk",
            side_effect=RuntimeError("token已过期"),
        ), patch("data_provider.tushare_client.build_tushare_http_client") as http_mock:
            with self.assertRaises(TushareCredentialError):
                query_tushare_api("index_basic", params={"limit": 5}, timeout=15)

        http_mock.assert_not_called()

    def test_query_tushare_api_falls_back_when_sdk_times_out(self) -> None:
        with patch("data_provider.tushare_client.query_tushare_api_via_sdk", side_effect=TushareQueryTimeout("slow")), \
                patch("data_provider.tushare_client.build_tushare_http_client") as http_mock:
            with self.assertRaises(TushareQueryTimeout):
                query_tushare_api("index_basic", params={"limit": 5}, timeout=1)

        http_mock.assert_not_called()

    def test_query_tushare_api_serializes_gateway_calls(self) -> None:
        in_flight = 0
        max_in_flight = 0

        def fake_sdk(api_name, params=None, fields="", timeout=30):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                return f"result-{(params or {}).get('limit')}"
            finally:
                in_flight -= 1

        with patch("data_provider.tushare_client.query_tushare_api_via_sdk", side_effect=fake_sdk):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(
                    lambda value: query_tushare_api("index_basic", params={"limit": value}, timeout=15),
                    [1, 2, 3],
                ))

        self.assertEqual(results, ["result-1", "result-2", "result-3"])
        self.assertEqual(max_in_flight, 1)


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
