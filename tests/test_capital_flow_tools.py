# -*- coding: utf-8 -*-
"""Tests for extended capital flow tools registration and planner mapping."""

from src.agent.factory import get_tool_registry


def test_capital_flow_tools_are_registered():
    registry = get_tool_registry()

    assert "get_capital_flow" in registry
    assert "get_market_capital_flow" in registry
    assert "get_northbound_capital_flow" in registry
    assert "get_margin_trading_summary" in registry


def test_stockapi_market_microstructure_tools_are_registered():
    registry = get_tool_registry()

    assert "get_stockapi_limit_up_pool" in registry
    assert "get_stockapi_hot_sectors" in registry
    assert "get_stockapi_sector_constituents" in registry
    assert "get_stockapi_sector_flow_history" in registry
    assert "get_stockapi_popularity_rank" in registry
    assert "get_stockapi_hot_money_activity" in registry
