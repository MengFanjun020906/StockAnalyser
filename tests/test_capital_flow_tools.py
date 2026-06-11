# -*- coding: utf-8 -*-
"""Tests for extended capital flow tools registration and planner mapping."""

import time
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.candidate_experts.orchestrator import CandidateExpertOrchestrator
from src.agent.factory import get_tool_registry
from src.agent.tools.data_tools import _handle_get_market_capital_flow


def test_capital_flow_tools_are_registered():
    registry = get_tool_registry()

    assert "get_capital_flow" in registry
    assert "get_tushare_moneyflow_ind_ths" in registry
    assert "get_tushare_moneyflow_ind_dc" in registry
    assert "get_tushare_moneyflow_cnt_ths" in registry
    assert "get_tushare_ths_member" in registry
    assert "get_tushare_announcements" in registry
    assert "get_tushare_stock_alerts" in registry
    assert "get_tushare_stock_shock" in registry
    assert "get_tushare_pledge_stat" in registry
    assert "get_tushare_pledge_detail" in registry
    assert "get_tushare_share_float" in registry
    assert "get_tushare_holder_trade" in registry
    assert "get_tushare_repurchase" in registry
    assert "get_tushare_daily_basic" in registry
    assert "get_tushare_financial_indicators" in registry
    assert "get_tushare_forecast" in registry
    assert "get_tushare_express" in registry
    assert "get_tushare_dividend" in registry
    assert "get_tushare_adj_factor" in registry
    assert "get_tushare_index_daily" in registry
    assert "get_tushare_trade_calendar" in registry
    assert "get_tushare_moneyflow_ths" in registry
    assert "get_tushare_moneyflow_dc" in registry
    assert "get_tushare_moneyflow_mkt_dc" in registry
    assert "get_tushare_moneyflow_hsgt" in registry
    assert "get_tushare_hsgt_top10" in registry
    assert "get_tushare_margin_detail" in registry
    assert "get_tushare_block_trade" in registry
    assert "get_tushare_dragon_tiger_list" in registry
    assert "get_tushare_dragon_tiger_inst" in registry
    assert "get_tushare_limit_list_ths" in registry
    assert "get_tushare_limit_list_d" in registry
    assert "get_tushare_limit_step" in registry
    assert "get_tushare_hot_rank" in registry
    assert "get_market_capital_flow" in registry
    assert "get_northbound_capital_flow" in registry
    assert "get_margin_trading_summary" in registry
    tool = registry.get("get_capital_flow")
    assert tool is not None
    description = tool.description
    assert "main_net_inflow" in description
    assert "net_inflow" in description
    assert "Do not describe `net_inflow*` as 主力资金" in description


def test_stockapi_market_microstructure_tools_are_registered():
    registry = get_tool_registry()

    assert "get_stockapi_limit_up_pool" in registry
    assert "get_stockapi_hot_sectors" in registry
    assert "get_stockapi_sector_constituents" in registry
    assert "get_stockapi_sector_flow_history" in registry
    assert "get_stockapi_hot_sector_leaders" in registry
    assert "get_stockapi_change_all_history" in registry
    assert "get_stockapi_popularity_rank" in registry
    assert "get_stockapi_hot_money_activity" in registry


def test_get_market_capital_flow_returns_structured_timeout_inside_tool():
    class _SlowAdapter:
        def get_market_capital_flow(self, top_n=5):
            time.sleep(0.2)
            return {"status": "ok"}

    cfg = SimpleNamespace(agent_tushare_tool_timeout_seconds=0.01)
    with patch("src.agent.tools.data_tools._get_fundamental_adapter", return_value=_SlowAdapter()), \
         patch("src.config.get_config", return_value=cfg):
        result = _handle_get_market_capital_flow(top_n=2)

    assert result["status"] == "timeout"
    assert result["source_chain"][0]["result"] == "timeout"
    assert result["market_flow"] == {}


def test_tushare_moneyflow_dc_is_first_capital_expert_source():
    orchestrator = CandidateExpertOrchestrator(timeout_s=1.0)
    packet = orchestrator._capital_packet(limit=8, tools={
        "tushare_moneyflow_ths": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_moneyflow_dc": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_dragon_tiger_list": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_dragon_tiger_inst": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_limit_list_ths": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_limit_list_d": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_limit_step": lambda limit: {"status": "empty", "items": [], "errors": []},
        "tushare_hot_rank": lambda limit: {"status": "empty", "items": [], "errors": []},
        "stockapi_limit_up_pool": lambda limit: {"status": "empty", "items": [], "errors": []},
        "stockapi_popularity_rank": lambda limit: {"status": "empty", "items": [], "errors": []},
        "stockapi_hot_money_activity": lambda limit: {"status": "empty", "items": [], "errors": []},
    })

    assert packet.diagnostics[0]["source"] == "tushare_moneyflow_dc"
    assert packet.diagnostics[1]["source"] == "tushare_dragon_tiger_list"
    assert packet.diagnostics[2]["source"] == "tushare_dragon_tiger_inst"


def test_sector_theme_expert_prefers_tushare_board_moneyflow_and_members():
    orchestrator = CandidateExpertOrchestrator(timeout_s=1.0)

    packet = orchestrator._sector_packet(
        sector_names=[],
        limit=3,
        tools={
            "top_sector_names": lambda top_n: [],
            "fetch_sector_constituents": lambda sector, limit, include_diagnostics=False: ([], []) if include_diagnostics else [],
            "tushare_moneyflow_ind_ths": lambda limit: {
                "status": "ok",
                "items": [{
                    "ts_code": "881001.TI",
                    "name": "AI",
                    "net_inflow": 300000000.0,
                    "change_ratio": 3.0,
                    "lead_stock": "样例股份",
                    "lead_stock_pct_change": 10.0,
                }],
                "errors": [],
            },
            "tushare_moneyflow_cnt_ths": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_moneyflow_ind_dc": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_ths_member": lambda ts_code, limit: {
                "status": "ok",
                "items": [{"code": "600519", "name": "贵州茅台", "weight": 5.0}],
                "errors": [],
            },
        },
    )

    assert packet.status == "ok"
    assert packet.diagnostics[0]["source"] == "tushare_moneyflow_ind_ths"
    assert packet.candidates[0].code == "600519"
    assert packet.candidates[0].raw["source"] == "sector_theme:tushare_moneyflow_ind_ths"


def test_news_event_expert_adds_tushare_structured_events():
    orchestrator = CandidateExpertOrchestrator(timeout_s=1.0)

    packet = orchestrator._news_packet(
        limit=3,
        seed_candidates=[],
        tools={
            "discover_news_momentum": lambda limit, seed_candidates=None: {
                "status": "empty",
                "candidates": [],
                "diagnostics": [],
            },
            "tushare_announcements": lambda limit: {
                "status": "ok",
                "items": [{"ts_code": "600519.SH", "name": "贵州茅台", "ann_date": "20260508", "title": "股份回购公告"}],
                "errors": [],
            },
            "tushare_stock_alerts": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_stock_shock": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_share_float": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_holder_trade": lambda limit: {"status": "empty", "items": [], "errors": []},
            "tushare_repurchase": lambda limit: {"status": "empty", "items": [], "errors": []},
        },
    )

    assert packet.status == "ok"
    assert packet.candidates[0].code == "600519"
    assert packet.candidates[0].raw["source"] == "news_event:tushare_announcements"
    assert any(item["source"] == "tushare_announcements" for item in packet.diagnostics)


def test_fundamental_expert_adds_tushare_daily_basic_candidates():
    orchestrator = CandidateExpertOrchestrator(timeout_s=1.0)

    with patch(
        "src.agent.candidate_experts.orchestrator.FundamentalCandidateProvider.discover",
        return_value={
            "status": "empty",
            "candidates": [],
            "diagnostics": [],
        },
    ):
        packet = orchestrator._fundamental_packet(
            limit=3,
            strategy_names=None,
            tools={
                "tushare_daily_basic": lambda limit: {
                    "status": "ok",
                    "items": [{"ts_code": "600519.SH", "trade_date": "20260508", "pe_ttm": 20.0, "pb": 4.0, "turnover_rate": 2.0, "total_mv": 20000000.0}],
                    "errors": [],
                },
                "tushare_forecast": lambda limit: {"status": "empty", "items": [], "errors": []},
                "tushare_express": lambda limit: {"status": "empty", "items": [], "errors": []},
                "tushare_dividend": lambda limit: {"status": "empty", "items": [], "errors": []},
            },
        )

    assert packet.status == "ok"
    assert any(item["source"] == "tushare_daily_basic" for item in packet.diagnostics)
    assert any(candidate.raw.get("source") == "fundamental:tushare_daily_basic" for candidate in packet.candidates)


def test_fundamental_expert_skips_single_stock_only_tushare_sources_in_auto_mode():
    orchestrator = CandidateExpertOrchestrator(timeout_s=1.0)
    called = {"forecast": 0, "express": 0, "dividend": 0}

    with patch(
        "src.agent.candidate_experts.orchestrator.FundamentalCandidateProvider.discover",
        return_value={
            "status": "empty",
            "candidates": [],
            "diagnostics": [],
        },
    ):
        packet = orchestrator._fundamental_packet(
            limit=3,
            strategy_names=None,
            tools={
                "tushare_daily_basic": lambda limit: {
                    "status": "ok",
                    "items": [{"ts_code": "600519.SH", "trade_date": "20260508", "pe_ttm": 20.0, "pb": 4.0, "turnover_rate": 2.0, "total_mv": 20000000.0}],
                    "errors": [],
                },
                "tushare_forecast": lambda limit: called.__setitem__("forecast", called["forecast"] + 1),
                "tushare_express": lambda limit: called.__setitem__("express", called["express"] + 1),
                "tushare_dividend": lambda limit: called.__setitem__("dividend", called["dividend"] + 1),
            },
        )

    assert packet.status == "ok"
    assert any(item["source"] == "tushare_daily_basic" for item in packet.diagnostics)
    assert any(candidate.raw.get("source") == "fundamental:tushare_daily_basic" for candidate in packet.candidates)
    assert called == {"forecast": 0, "express": 0, "dividend": 0}
