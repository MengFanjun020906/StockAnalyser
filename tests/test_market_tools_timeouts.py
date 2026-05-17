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
