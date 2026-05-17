from src.agent.context_builder import build_agent_user_context_from_portfolio_snapshot


def test_build_agent_user_context_from_portfolio_snapshot_maps_accounts_and_positions():
    snapshot = {
        "as_of": "2026-05-03",
        "cost_method": "fifo",
        "currency": "CNY",
        "fx_stale": False,
        "accounts": [
            {
                "account_id": 1,
                "account_name": "Main",
                "broker": "Demo",
                "market": "cn",
                "base_currency": "CNY",
                "total_cash": 20000,
                "total_market_value": 160000,
                "total_equity": 180000,
                "cost_method": "fifo",
                "positions": [
                    {
                        "symbol": "600519",
                        "market": "cn",
                        "quantity": 100,
                        "avg_cost": 1500,
                        "total_cost": 150000,
                        "last_price": 1600,
                        "market_value_base": 160000,
                        "unrealized_pnl_base": 10000,
                        "unrealized_pnl_pct": 6.6667,
                        "price_available": True,
                        "price_source": "daily_close",
                    }
                ],
            }
        ],
    }

    context = build_agent_user_context_from_portfolio_snapshot(
        snapshot,
        primary_symbol="600519",
        user_prompt="这只持仓要不要减仓？",
        analysis_mode="planning_execute",
    )

    assert context.report.analysis_mode == "planning_execute"
    assert context.report.primary_symbol == "600519"
    assert context.report.target_symbols == ["600519"]
    assert context.accounts[0].account_id == 1
    assert context.accounts[0].available_cash == 20000
    assert context.positions[0].symbol == "600519"
    assert context.positions[0].position_pct == 88.888889
    assert context.has_position_for("600519") is True
    assert context.metadata["source"] == "PortfolioService.get_portfolio_snapshot"
