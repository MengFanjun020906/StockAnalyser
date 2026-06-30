# -*- coding: utf-8 -*-
"""Timeout diagnostics for market-level Agent tools."""

from unittest.mock import patch

import pandas as pd

from src.agent.tools.market_tools import _get_tushare_sector_rankings_fast, _handle_get_sector_rankings


def test_get_sector_rankings_returns_structured_timeout():
    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        return_value=(None, "sector_rankings timeout", 3000),
    ):
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "timeout"
    assert result["top_sectors"] == []
    assert result["bottom_sectors"] == []
    assert result["source_chain"][0]["result"] == "timeout"
    assert "sector_rankings timeout" in result["errors"]


def test_get_sector_rankings_prefers_tushare_ths_hot_industry_path():
    tushare_payload = (
        [{"name": "煤炭行业", "rank": 1, "change_pct": 3.2, "source": "tushare:ths_hot"}],
        [],
        [{"provider": "tushare:ths_hot", "result": "ok", "market": "行业板块"}],
        "",
    )

    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        return_value=(tushare_payload, None, 120),
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "ok"
    assert result["top_sectors"][0]["name"] == "煤炭行业"
    assert result["data_source"] == "tushare:ths_hot"
    assert result["ranking_market"] == "行业板块"
    assert result["source_chain"][0]["provider"] == "tushare:ths_hot"
    assert mock_timeout.call_count == 1


def test_get_sector_rankings_falls_back_to_stockapi_after_tushare_and_eastmoney_fail():
    tushare_payload = ([], [], [{"provider": "tushare:ths_hot", "result": "failed"}], "tushare quota exhausted")
    eastmoney_payload = ([], [], [{"provider": "eastmoney:stock_board_industry_name_em", "result": "failed"}], "eastmoney disconnected")
    stockapi_payload = (
        [{"name": "机器人", "change_pct": 3.2, "source": "stockapi:hotBkJlrDr"}],
        [],
        ["stockapi:hotBkJlrDr"],
        "",
    )

    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        side_effect=[
            (tushare_payload, None, 100),
            (eastmoney_payload, None, 100),
            (stockapi_payload, None, 100),
        ],
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "ok"
    assert result["top_sectors"][0]["name"] == "机器人"
    assert mock_timeout.call_count == 3


def test_get_sector_rankings_does_not_call_manager_probe_after_fast_sources_fail():
    tushare_payload = ([], [], [{"provider": "tushare:ths_hot", "result": "failed"}], "tushare quota exhausted")
    eastmoney_payload = ([], [], [{"provider": "eastmoney:stock_board_industry_name_em", "result": "failed"}], "eastmoney disconnected")
    stockapi_payload = ([], [], ["stockapi:hotBkJlrDr"], "stockapi quota exhausted")

    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        side_effect=[
            (tushare_payload, None, 100),
            (eastmoney_payload, None, 100),
            (stockapi_payload, None, 100),
        ],
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "failed"
    assert result["top_sectors"] == []
    assert result["bottom_sectors"] == []
    assert "tushare quota exhausted" in result["errors"]
    assert "eastmoney disconnected" in result["errors"]
    assert "stockapi quota exhausted" in result["errors"]
    assert mock_timeout.call_count == 3


def test_get_sector_rankings_preserves_stockapi_budget_when_total_timeout_is_low():
    calls = []
    empty_payload = ([], [], [], "empty")

    def fake_timeout(_task, timeout_seconds, task_name):
        calls.append((task_name, timeout_seconds))
        return empty_payload, None, 1

    with patch(
        "src.agent.tools.market_tools._get_agent_timeout_attr",
        return_value=6.0,
    ), patch(
        "src.agent.tools.market_tools._run_with_timeout",
        side_effect=fake_timeout,
    ):
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "failed"
    assert [name for name, _timeout in calls] == [
        "tushare_ths_hot_industry_rankings",
        "eastmoney_industry_rankings",
        "stockapi_hot_sectors",
    ]
    assert calls[2][1] >= 1.0


def test_tushare_sector_rankings_uses_ths_hot_industry_market():
    seen = []

    class FakeTushareHttpClient:
        _timeout = 3

        def query(self, api_name, fields="", **params):
            seen.append((api_name, params, fields, self._timeout))
            assert api_name == "ths_hot"
            trade_date = params.get("trade_date") or "20260612"
            return pd.DataFrame([
                {
                    "trade_date": "20260611",
                    "data_type": "行业板块",
                    "ts_code": "881121.TI",
                    "ts_name": "半导体旧日",
                    "rank": 1,
                    "pct_change": 9.9,
                    "current_price": 1200.0,
                    "hot": 99999,
                    "rank_time": "22:30",
                },
                {
                    "trade_date": trade_date,
                    "data_type": "行业板块",
                    "ts_code": "881001.TI",
                    "ts_name": "半导体",
                    "rank": 1,
                    "pct_change": 2.4,
                    "current_price": 1234.5,
                    "hot": 98765,
                    "rank_time": "22:30",
                },
                {
                    "trade_date": trade_date,
                    "data_type": "热股",
                    "ts_code": "600000.SH",
                    "ts_name": "浦发银行",
                    "rank": 2,
                    "pct_change": 1.0,
                },
            ])

    with patch("data_provider.tushare_client.get_tushare_token", return_value="token"), \
         patch("data_provider.tushare_client.build_tushare_http_client", return_value=FakeTushareHttpClient()):
        result = _get_tushare_sector_rankings_fast(top_n=5, timeout=3.0)

    assert result is not None
    top, bottom, source_chain, error = result
    assert error == ""
    assert top[0]["name"] == "半导体"
    assert top[0]["code"] == "881001.TI"
    assert top[0]["source"] == "tushare:ths_hot"
    assert bottom[0]["name"] == "半导体"
    ths_call = next(call for call in seen if call[0] == "ths_hot")
    assert ths_call[1]["market"] == "行业板块"
    assert ths_call[1]["is_new"] == "Y"
    assert ths_call[1]["trade_date"]
    assert source_chain[0]["provider"] == "tushare:ths_hot"
