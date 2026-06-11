# -*- coding: utf-8 -*-
"""Regression tests for pseudo-precision dashboard fact guarding."""

from src.agent.fact_guard import sanitize_dashboard_facts
from src.agent.protocols import AgentContext, AgentOpinion


def test_sanitizes_unverified_capital_chip_ma_and_odd_lot_buy() -> None:
    ctx = AgentContext(query="通富微电能买吗", stock_code="002156", stock_name="通富微电")
    payload = {
        "dashboard": {
            "data_perspective": {
                "price_position": {"ma5": 66.85, "ma10": 67.79, "ma20": "N/A"},
                "capital_flow": {"inflow_10d": -7_720_000_000},
                "chip_structure": {
                    "profit_ratio": "11.5%",
                    "avg_cost": 67.26,
                    "concentration": "61.6-74.0",
                    "chip_health": "健康",
                },
            },
            "core_conclusion": {
                "position_advice": {"no_position": "可买入146股试仓。", "has_position": "继续持有。"}
            },
            "battle_plan": {"position_strategy": {"entry_plan": "买入 146 股"}},
        }
    }

    cleaned, warnings = sanitize_dashboard_facts(payload, ctx)

    dp = cleaned["dashboard"]["data_perspective"]
    assert dp["capital_flow"]["inflow_10d"] == "N/A"
    assert dp["chip_structure"]["profit_ratio"] == "N/A"
    assert dp["chip_structure"]["avg_cost"] == "N/A"
    assert dp["chip_structure"]["chip_health"] == "N/A"
    assert dp["price_position"]["ma5"] == "N/A"
    assert "按100股整数倍" in cleaned["dashboard"]["core_conclusion"]["position_advice"]["no_position"]
    assert "按100股整数倍" in cleaned["dashboard"]["battle_plan"]["position_strategy"]["entry_plan"]
    assert {item["code"] for item in warnings} >= {
        "unverified_capital_flow",
        "unverified_chip",
        "unverified_ma",
        "invalid_a_share_buy_lot",
    }


def test_keeps_values_matching_successful_tool_payloads() -> None:
    ctx = AgentContext(query="看一下", stock_code="002156", stock_name="通富微电")
    ctx.set_data(
        "capital_flow",
        {
            "status": "ok",
            "inflow_10d": -12_300_000,
            "source_chain": [{"provider": "tushare:moneyflow", "result": "ok"}],
        },
    )
    ctx.set_data(
        "chip_distribution",
        {
            "status": "ok",
            "profit_ratio": 0.115,
            "avg_cost": 67.26,
            "concentration_90": 0.22,
            "source_chain": [{"provider": "tushare:cyq_chips", "result": "ok"}],
        },
    )
    ctx.add_opinion(
        AgentOpinion(
            agent_name="technical",
            raw_data={"ma": {"ma5": {"value": 66.85}, "ma10": {"value": 67.79}}},
        )
    )
    payload = {
        "dashboard": {
            "data_perspective": {
                "price_position": {"ma5": 66.85, "ma10": 67.79},
                "capital_flow": {"inflow_10d": -12_300_000},
                "chip_structure": {"profit_ratio": "11.5%", "avg_cost": 67.26, "concentration": "22.00%"},
            },
            "core_conclusion": {"position_advice": {"no_position": "买入100股"}},
            "battle_plan": {},
        }
    }

    cleaned, warnings = sanitize_dashboard_facts(payload, ctx)

    dp = cleaned["dashboard"]["data_perspective"]
    assert dp["capital_flow"]["inflow_10d"] == -12_300_000
    assert dp["chip_structure"]["profit_ratio"] == "11.5%"
    assert dp["price_position"]["ma5"] == 66.85
    assert warnings == []
