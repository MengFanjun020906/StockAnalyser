# -*- coding: utf-8 -*-
"""Contract tests for Tushare-backed Agent data tools."""

from unittest.mock import patch

import pandas as pd

from src.agent.tools.data_tools import (
    _handle_get_margin_trading_summary,
    _handle_get_tushare_basic_data,
    _handle_get_tushare_daily_bars,
    _handle_get_tushare_financial_statements,
    _handle_get_tushare_reference_events,
)


def _fake_query(api_name, params=None, fields="", timeout=30):
    if api_name == "stock_basic":
        return pd.DataFrame([{"ts_code": "603418.SH", "symbol": "603418", "name": "友升股份"}])
    if api_name in {"daily", "weekly", "monthly"}:
        return pd.DataFrame([{"ts_code": "603418.SH", "trade_date": "20260514", "close": 34.5}])
    if api_name in {"income", "balancesheet", "cashflow"}:
        return pd.DataFrame([{"ts_code": "603418.SH", "end_date": "20251231"}])
    if api_name == "margin":
        return pd.DataFrame([{"trade_date": "20260514", "exchange_id": (params or {}).get("exchange_id"), "rzye": 100.0}])
    return pd.DataFrame([{"ts_code": "603418.SH", "api_name": api_name}])


def test_get_tushare_basic_data_stock_normalizes_rows():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_basic_data(asset_type="stock", limit=5)

    assert result["status"] == "ok"
    assert result["api_name"] == "stock_basic"
    assert result["items"][0]["name"] == "友升股份"
    assert result["source_chain"][0]["endpoint"] == "http://unit/"


def test_get_tushare_daily_bars_converts_code_and_period():
    seen = {}

    def capture(api_name, params=None, fields="", timeout=30):
        seen["api_name"] = api_name
        seen["params"] = params
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_daily_bars("603418", period="weekly", start_date="2026-05-01", end_date="20260515")

    assert result["status"] == "ok"
    assert seen["api_name"] == "weekly"
    assert seen["params"]["ts_code"] == "603418.SH"
    assert seen["params"]["start_date"] == "20260501"


def test_get_tushare_financial_statements_returns_three_blocks():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_financial_statements("603418", period="20251231")

    assert result["status"] == "ok"
    assert set(result["blocks"]) == {"income", "balancesheet", "cashflow"}


def test_get_tushare_reference_events_can_select_unlock():
    seen = {}

    def capture(api_name, params=None, fields="", timeout=30):
        seen["api_name"] = api_name
        seen["params"] = params
        return _fake_query(api_name, params=params, fields=fields, timeout=timeout)

    with patch("data_provider.tushare_client.query_tushare_api", side_effect=capture), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_tushare_reference_events("603418", event_type="unlock", start_date="20250101")

    assert result["status"] == "ok"
    assert seen["api_name"] == "share_float"
    assert seen["params"]["ts_code"] == "603418.SH"
    assert "unlock" in result["blocks"]


def test_get_margin_trading_summary_uses_tushare_margin():
    with patch("data_provider.tushare_client.query_tushare_api", side_effect=_fake_query), \
            patch("data_provider.tushare_client.get_tushare_http_url", return_value="http://unit/"):
        result = _handle_get_margin_trading_summary(limit=2)

    assert result["status"] == "ok"
    assert result["sse"][0]["exchange_id"] == "SSE"
    assert result["szse"][0]["exchange_id"] == "SZSE"
    assert result["source_chain"][0]["provider"] == "tushare:margin"
