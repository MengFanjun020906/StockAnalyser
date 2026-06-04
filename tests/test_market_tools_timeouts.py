# -*- coding: utf-8 -*-
"""Timeout diagnostics for market-level Agent tools."""

from unittest.mock import patch

from src.agent.tools.market_tools import _handle_get_sector_rankings


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


def test_get_sector_rankings_prefers_eastmoney_fast_path():
    eastmoney_payload = (
        [{"name": "煤炭行业", "change_pct": 3.2, "source": "eastmoney:stock_board_industry_name_em"}],
        [],
        [{"provider": "eastmoney:stock_board_industry_name_em", "result": "ok"}],
        "",
    )

    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        return_value=(eastmoney_payload, None, 120),
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "ok"
    assert result["top_sectors"][0]["name"] == "煤炭行业"
    assert result["source_chain"][0]["provider"] == "eastmoney:stock_board_industry_name_em"
    assert mock_timeout.call_count == 1


def test_get_sector_rankings_falls_back_to_stockapi_after_eastmoney_fails():
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
            (eastmoney_payload, None, 100),
            (stockapi_payload, None, 100),
        ],
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "ok"
    assert result["top_sectors"][0]["name"] == "机器人"
    assert mock_timeout.call_count == 2


def test_get_sector_rankings_does_not_call_manager_probe_after_fast_sources_fail():
    eastmoney_payload = ([], [], [{"provider": "eastmoney:stock_board_industry_name_em", "result": "failed"}], "eastmoney disconnected")
    stockapi_payload = ([], [], ["stockapi:hotBkJlrDr"], "stockapi quota exhausted")

    with patch(
        "src.agent.tools.market_tools._run_with_timeout",
        side_effect=[
            (eastmoney_payload, None, 100),
            (stockapi_payload, None, 100),
        ],
    ) as mock_timeout:
        result = _handle_get_sector_rankings(top_n=10)

    assert result["status"] == "failed"
    assert result["top_sectors"] == []
    assert result["bottom_sectors"] == []
    assert "eastmoney disconnected" in result["errors"]
    assert "stockapi quota exhausted" in result["errors"]
    assert mock_timeout.call_count == 2
