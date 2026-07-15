# -*- coding: utf-8 -*-

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.services.agent_entry_minute_data_service import AgentEntryMinuteDataService, EntryMinuteSyncResult
from src.services.agent_entry_execution_backtest_service import (
    AgentEntryExecutionBacktestService,
    _attach_signal_validity,
    _simulate_next_open,
    _simulate_strict,
)
from src.storage import DatabaseManager, StockDaily, StockMinuteBar


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_entry_trace(trace_dir: Path) -> None:
    trace_dir.mkdir(parents=True)
    _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
    _write_json(
        trace_dir / "final_report.json",
        {
            "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
            "portfolio_allocation": {
                "full": {
                    "positions_plan": [
                        {
                            "rank": 1,
                            "code": "600001",
                            "name": "测试一",
                            "action": "wait",
                            "execution_mode": "conditional_open",
                            "entry_condition": "回踩 10.0-10.2 可试探",
                            "entry_expiry_days": 2,
                            "signal_valid_days": 2,
                            "signal_validity_label": "2 个交易日，未触发则失效",
                            "stop_loss_condition": "跌破 9.7",
                            "take_profit_condition": "目标 10.8",
                        }
                    ]
                }
            },
            "pricing_agent": {"full": {"if_then_order_matrix": []}},
            "judge_decision": {"summary": {"final_action": "wait"}},
        },
    )


def _write_mixed_entry_trace(trace_dir: Path) -> None:
    trace_dir.mkdir(parents=True)
    _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
    _write_json(
        trace_dir / "final_report.json",
        {
            "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
            "portfolio_allocation": {
                "full": {
                    "positions_plan": [
                        {
                            "rank": 1,
                            "code": "600001",
                            "name": "测试一",
                            "action": "wait",
                            "execution_mode": "conditional_open",
                            "entry_condition": "回踩 10.0-10.2 可试探",
                            "entry_expiry_days": 2,
                            "signal_valid_days": 2,
                            "signal_validity_label": "2 个交易日，未触发则失效",
                            "stop_loss_condition": "跌破 9.7",
                            "take_profit_condition": "目标 10.8",
                        },
                        {
                            "rank": 2,
                            "code": "600002",
                            "name": "价格缺失",
                            "action": "wait",
                            "execution_mode": "conditional_open",
                            "entry_condition": "价格回落至均值回归锚点附近(价格缺失)",
                            "stop_loss_condition": "跌破入场低点或账户止损线 (-8%)",
                            "take_profit_condition": "到达第一压力位分批止盈",
                        },
                    ]
                }
            },
            "pricing_agent": {"full": {"if_then_order_matrix": []}},
            "judge_decision": {"summary": {"final_action": "wait"}},
        },
    )


def test_entry_execution_backtest_builds_final_report_trades_from_stock_daily():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260101-trace-entry"
        trace_dir.mkdir(parents=True)
        _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
        _write_json(
            trace_dir / "final_report.json",
            {
                "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
                "portfolio_allocation": {
                    "full": {
                        "positions_plan": [
                            {
                                "rank": 1,
                                "code": "600001",
                                "name": "测试一",
                                "action": "wait",
                                "execution_mode": "conditional_open",
                                "entry_condition": "回踩 10.0-10.2 可试探",
                                "entry_expiry_days": 2,
                                "signal_valid_days": 2,
                                "signal_validity_label": "2 个交易日，未触发则失效",
                                "stop_loss_condition": "跌破 9.7",
                                "take_profit_condition": "目标 11.2",
                            },
                            {
                                "rank": 2,
                                "code": "600002",
                                "name": "测试二",
                                "action": "wait",
                                "entry_condition": "回踩 20.0-20.2",
                                "entry_expiry_days": 2,
                                "signal_valid_days": 2,
                                "signal_validity_label": "2 个交易日，未触发则失效",
                                "stop_loss_condition": "跌破 19.5",
                            },
                            {
                                "rank": 3,
                                "code": "600003",
                                "name": "测试三",
                                "action": "wait",
                                "entry_condition": "回踩 30.0-30.2",
                                "entry_expiry_days": 2,
                                "signal_valid_days": 2,
                                "signal_validity_label": "2 个交易日，未触发则失效",
                                "stop_loss_condition": "跌破 29.5",
                            },
                            {
                                "rank": 4,
                                "code": "600004",
                                "name": "第四只",
                                "action": "wait",
                                "entry_condition": "回踩 40.0-40.2",
                                "entry_expiry_days": 2,
                                "signal_valid_days": 2,
                                "signal_validity_label": "2 个交易日，未触发则失效",
                            },
                            {
                                "rank": 5,
                                "code": "600005",
                                "name": "拒绝项",
                                "action": "reject",
                                "entry_condition": "回踩 50.0-50.2",
                            },
                        ]
                    }
                },
                "pricing_agent": {"full": {"if_then_order_matrix": []}},
                "judge_decision": {"summary": {"final_action": "wait"}},
            },
        )
        with db.get_session() as session:
            session.add(StockDaily(code="600001", date=date(2024, 1, 1), close=10.5, open=10.5, high=10.6, low=10.4))
            session.add(StockDaily(code="600001", date=date(2024, 1, 2), close=10.4, open=10.4, high=10.5, low=10.1))
            session.add(StockDaily(code="600001", date=date(2024, 1, 3), close=11.3, open=10.6, high=11.3, low=10.5))
            session.add(StockDaily(code="600002", date=date(2024, 1, 1), close=21.0, open=21.0, high=21.1, low=20.9))
            session.add(StockDaily(code="600002", date=date(2024, 1, 2), close=21.5, open=21.3, high=21.6, low=21.2))
            session.commit()

        rows = AgentEntryExecutionBacktestService(db_manager=db).build_backtests_for_trace(trace_dir=trace_dir)

        assert len(rows) == 4
        assert rows[0]["trace_id"] == "20260101-trace-entry"
        assert rows[0]["ts_code"] == "600001"
        assert rows[0]["trade_plan"]["entry_zone_low"] == 10.0
        assert rows[0]["trade_plan"]["entry_zone_high"] == 10.2
        strict = rows[0]["strategies"]["strict_ai_entry"]
        assert strict["status"] == "filled"
        assert strict["exit_reason"] == "take_profit"
        assert strict["pnl_pct"] > 9
        assert rows[1]["strategies"]["strict_ai_entry"]["status"] == "not_filled"
        assert rows[3]["ts_code"] == "600004"
        assert all(row["ts_code"] != "600005" for row in rows)

        output = Path(tmp) / "entry.jsonl"
        result = AgentEntryExecutionBacktestService(db_manager=db).write_backtests(rows, output_path=output)
        assert result.review_count == 4
        assert output.exists()

        DatabaseManager.reset_instance()


def test_entry_execution_backtest_prefers_cached_minute_bars():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260101-trace-minute-entry"
        _write_entry_trace(trace_dir)
        with db.get_session() as session:
            session.add(
                StockMinuteBar(
                    code="600001",
                    baostock_code="sh.600001",
                    frequency="5",
                    adjustflag="3",
                    bar_datetime=datetime(2024, 1, 2, 9, 35),
                    bar_date=date(2024, 1, 2),
                    bar_time="09:35:00",
                    open=10.4,
                    high=10.3,
                    low=10.1,
                    close=10.2,
                    volume=1000,
                    amount=10200,
                )
            )
            session.add(
                StockMinuteBar(
                    code="600001",
                    baostock_code="sh.600001",
                    frequency="5",
                    adjustflag="3",
                    bar_datetime=datetime(2024, 1, 2, 10, 0),
                    bar_date=date(2024, 1, 2),
                    bar_time="10:00:00",
                    open=10.5,
                    high=10.9,
                    low=10.4,
                    close=10.85,
                    volume=1200,
                    amount=12900,
                )
            )
            session.add(
                StockMinuteBar(
                    code="600001",
                    baostock_code="sh.600001",
                    frequency="5",
                    adjustflag="3",
                    bar_datetime=datetime(2024, 1, 3, 9, 35),
                    bar_date=date(2024, 1, 3),
                    bar_time="09:35:00",
                    open=10.7,
                    high=10.9,
                    low=10.6,
                    close=10.85,
                    volume=1200,
                    amount=12900,
                )
            )
            session.commit()

        rows = AgentEntryExecutionBacktestService(db_manager=db).build_backtests_for_trace(trace_dir=trace_dir)

        assert len(rows) == 1
        assert rows[0]["price_data"]["granularity"] == "minute"
        strict = rows[0]["strategies"]["strict_ai_entry"]
        assert strict["status"] == "filled"
        assert strict["entry_date"] == "2024-01-02 09:35:00"
        assert strict["exit_date"] == "2024-01-03 09:35:00"
        assert strict["exit_reason"] == "take_profit"
        assert strict["holding_days"] == 1
        assert strict["trade_rule"] == "cn_t_plus_1"
        assert strict["sellable_from"] == "2024-01-03 09:35:00"
        assert rows[0]["trade_plan"]["signal_valid_until"] == "2024-01-03"

        DatabaseManager.reset_instance()


def test_strict_entry_respects_a_share_t_plus_one_on_daily_bars():
    bars = [
        {"date": "2026-07-02", "open": 10.0, "high": 11.3, "low": 9.5, "close": 10.5},
        {"date": "2026-07-03", "open": 10.4, "high": 10.9, "low": 10.2, "close": 10.6},
    ]
    plan = {
        "entry_zone_low": 10.0,
        "entry_zone_high": 10.2,
        "stop_loss_price": 9.7,
        "take_profit_price": 11.2,
        "entry_expiry_days": 1,
        "max_hold_days": 2,
    }

    result = _simulate_strict(
        strategy_name="strict_ai_entry",
        start_price=10.0,
        forward_bars=bars,
        trade_plan=plan,
    )

    assert result["status"] == "filled"
    assert result["entry_date"] == "2026-07-02"
    assert result["exit_date"] == "2026-07-03"
    assert result["exit_reason"] == "timeout_exit"
    assert result["trade_rule"] == "cn_t_plus_1"
    assert result["sellable_from"] == "2026-07-03"


def test_signal_validity_uses_entry_expiry_trading_window():
    bars = [
        {"date": "2026-07-02", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1},
        {"date": "2026-07-03", "open": 10.1, "high": 10.3, "low": 9.9, "close": 10.2},
        {"date": "2026-07-06", "open": 10.2, "high": 10.4, "low": 10.0, "close": 10.3},
        {"date": "2026-07-07", "open": 10.3, "high": 10.5, "low": 10.1, "close": 10.4},
    ]

    plan = _attach_signal_validity({"entry_expiry_days": 3}, bars)

    assert plan["signal_valid_days"] == 3
    assert plan["signal_valid_until"] == "2026-07-06"
    assert plan["signal_validity_label"] == "3 个交易日"


def test_missing_signal_validity_defaults_to_five_day_entry_window():
    bars = [
        {"date": "2026-07-02", "open": 10.4, "high": 10.8, "low": 10.3, "close": 10.5},
        {"date": "2026-07-03", "open": 10.5, "high": 10.7, "low": 10.4, "close": 10.6},
        {"date": "2026-07-06", "open": 10.6, "high": 10.9, "low": 10.4, "close": 10.7},
        {"date": "2026-07-07", "open": 10.7, "high": 10.8, "low": 10.4, "close": 10.6},
        {"date": "2026-07-08", "open": 10.6, "high": 10.7, "low": 10.3, "close": 10.5},
        {"date": "2026-07-09", "open": 10.5, "high": 10.7, "low": 10.1, "close": 10.3},
    ]

    plan = _attach_signal_validity({"decision_date": "2026-07-01"}, bars)
    assert plan["entry_expiry_days"] == 5
    assert plan["signal_valid_days"] == 5
    assert plan["signal_valid_until"] == "2026-07-08"
    assert plan["signal_validity_label"] == "AI 未给出有效期，默认 5 个交易日内未触发买入则失效"

    result = _simulate_strict(
        strategy_name="strict_ai_entry",
        start_price=10.5,
        forward_bars=bars,
        trade_plan={
            "decision_date": "2026-07-01",
            "entry_zone_low": 10.0,
            "entry_zone_high": 10.2,
            "stop_loss_price": 9.7,
            "take_profit_price": 11.2,
        },
    )

    assert result["status"] == "not_filled"
    assert result["entry_reason"] == "signal_expired"
    assert result["entry_window_days"] == 5
    assert result["limits"] == ["signal_expired"]


def test_default_max_hold_exits_after_thirty_trading_days_without_sl_tp():
    bars = [
        {
            "date": (date(2026, 7, 1) + timedelta(days=idx)).isoformat(),
            "open": 10.0,
            "high": 10.4,
            "low": 9.8,
            "close": 10.1,
        }
        for idx in range(35)
    ]

    result = _simulate_next_open(
        strategy_name="next_open_baseline",
        start_price=10.0,
        forward_bars=bars,
        trade_plan={
            "entry_expiry_days": 5,
            "stop_loss_price": 9.0,
            "take_profit_price": 12.0,
        },
    )

    assert result["status"] == "filled"
    assert result["exit_reason"] == "timeout_exit"
    assert result["holding_days"] == 30
    assert result["exit_date"] == "2026-07-31"


def test_backtest_extracts_validity_label_and_expires_unfilled_signal():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260101-trace-validity-label"
        trace_dir.mkdir(parents=True)
        _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
        _write_json(
            trace_dir / "final_report.json",
            {
                "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
                "portfolio_allocation": {
                    "full": {
                        "positions_plan": [
                            {
                                "rank": 1,
                                "code": "600030",
                                "name": "有效期文案",
                                "action": "wait",
                                "execution_mode": "conditional_open",
                                "entry_condition": "回踩 10.0-10.2 可试探",
                                "signal_validity_label": "2 个交易日，未触发则失效",
                                "stop_loss_condition": "跌破 9.7",
                                "take_profit_condition": "目标 11.2",
                            }
                        ]
                    }
                },
                "pricing_agent": {"full": {"if_then_order_matrix": []}},
                "judge_decision": {"summary": {"final_action": "wait"}},
            },
        )
        with db.get_session() as session:
            session.add(StockDaily(code="600030", date=date(2024, 1, 1), close=10.8, open=10.8, high=10.9, low=10.5))
            session.add(StockDaily(code="600030", date=date(2024, 1, 2), close=10.7, open=10.7, high=10.8, low=10.4))
            session.add(StockDaily(code="600030", date=date(2024, 1, 3), close=10.6, open=10.6, high=10.7, low=10.3))
            session.add(StockDaily(code="600030", date=date(2024, 1, 4), close=10.2, open=10.3, high=10.4, low=10.1))
            session.commit()

        rows = AgentEntryExecutionBacktestService(db_manager=db).build_backtests_for_trace(trace_dir=trace_dir)

        assert len(rows) == 1
        assert rows[0]["trade_plan"]["entry_expiry_days"] == 2
        assert rows[0]["trade_plan"]["signal_valid_until"] == "2024-01-03"
        strict = rows[0]["strategies"]["strict_ai_entry"]
        assert strict["status"] == "not_filled"
        assert strict["entry_reason"] == "signal_expired"
        assert strict["limits"] == ["signal_expired"]

        DatabaseManager.reset_instance()


def test_entry_execution_backtest_ignores_list_markers_and_skips_abnormal_entry_prices():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260101-trace-price-sanity"
        trace_dir.mkdir(parents=True)
        _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
        _write_json(
            trace_dir / "final_report.json",
            {
                "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
                "portfolio_allocation": {
                    "full": {
                        "positions_plan": [
                            {
                                "rank": 1,
                                "code": "600010",
                                "name": "列表编号",
                                "action": "wait",
                                "execution_mode": "conditional_open",
                                "entry_condition": "1. 回踩 10.0-10.2 可试探",
                                "entry_expiry_days": 2,
                                "signal_valid_days": 2,
                                "signal_validity_label": "2 个交易日，未触发则失效",
                                "stop_loss_condition": "2. 跌破 9.7",
                                "take_profit_condition": "3. 目标 11.2",
                            },
                            {
                                "rank": 2,
                                "code": "600020",
                                "name": "异常价位",
                                "action": "wait",
                                "execution_mode": "conditional_open",
                                "entry_condition": "回踩 1.0 附近试探",
                                "stop_loss_condition": "跌破 0.9",
                                "take_profit_condition": "目标 1.2",
                            },
                        ]
                    }
                },
                "pricing_agent": {"full": {"if_then_order_matrix": []}},
                "judge_decision": {"summary": {"final_action": "wait"}},
            },
        )
        with db.get_session() as session:
            for code in ("600010", "600020"):
                session.add(StockDaily(code=code, date=date(2024, 1, 1), close=10.5, open=10.5, high=10.6, low=10.4))
                session.add(StockDaily(code=code, date=date(2024, 1, 2), close=10.4, open=10.4, high=10.5, low=10.1))
                session.add(StockDaily(code=code, date=date(2024, 1, 3), close=11.3, open=10.6, high=11.3, low=10.5))
            session.commit()

        rows = AgentEntryExecutionBacktestService(db_manager=db).build_backtests_for_trace(trace_dir=trace_dir)

        assert len(rows) == 1
        assert rows[0]["ts_code"] == "600010"
        assert rows[0]["trade_plan"]["entry_zone_low"] == 10.0
        assert rows[0]["trade_plan"]["stop_loss_price"] == 9.7
        assert rows[0]["trade_plan"]["take_profit_price"] == 11.2

        DatabaseManager.reset_instance()


def test_entry_minute_data_service_syncs_baostock_rows_for_final_report_only():
    class FakeBaostockFetcher:
        def get_minute_k_data(self, stock_code, start_date, end_date, *, frequency="5", adjustflag="3"):
            assert stock_code == "600001"
            assert start_date == "2024-01-01"
            return pd.DataFrame(
                [
                    {
                        "date": "2024-01-02",
                        "time": "20240102093500000",
                        "code": "sh.600001",
                        "open": "10.40",
                        "high": "10.50",
                        "low": "10.10",
                        "close": "10.20",
                        "volume": "1000",
                        "amount": "10200",
                        "adjustflag": "3",
                    },
                    {
                        "date": "2024-01-02",
                        "time": "20240102094000000",
                        "code": "sh.600001",
                        "open": "10.20",
                        "high": "10.90",
                        "low": "10.20",
                        "close": "10.85",
                        "volume": "1200",
                        "amount": "12900",
                        "adjustflag": "3",
                    },
                ]
            )

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_root = Path(tmp) / "agent_traces"
        _write_mixed_entry_trace(trace_root / "20260101-trace-minute-sync")

        service = AgentEntryMinuteDataService(db_manager=db, baostock_fetcher=FakeBaostockFetcher())
        result = service.sync_for_latest_reports(trace_root=trace_root, limit=10, current_date=date(2024, 1, 5))

        assert result.plan_count == 2
        assert result.symbol_count == 1
        assert result.fetched_rows == 2
        assert result.written_rows == 2
        filtered = service.sync_for_latest_reports(
            trace_root=trace_root,
            limit=10,
            decision_date=date(2024, 1, 2),
            current_date=date(2024, 1, 5),
        )
        assert filtered.plan_count == 0
        assert filtered.symbol_count == 0
        assert filtered.fetched_rows == 0
        coverage = service.stock_repo.get_minute_coverage(
            code="600001",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 5),
        )
        assert coverage["count"] == 2

        DatabaseManager.reset_instance()


def test_entry_minute_sync_api_defaults_to_all_historical_reports():
    app = create_app()
    client = TestClient(app)
    sync_result = EntryMinuteSyncResult()

    with patch.object(
        AgentEntryMinuteDataService,
        "sync_for_latest_reports",
        return_value=sync_result,
    ) as sync_mock:
        response = client.post(
            "/api/v1/agent-entry-execution-backtests/minute-bars/sync",
            params={"rebuild": "false"},
        )

    assert response.status_code == 200
    sync_mock.assert_called_once_with(
        limit=None,
        decision_date=None,
        symbol=None,
        frequency="5",
        adjustflag="3",
    )


def test_entry_execution_backtest_query_and_api_rebuild():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "entry_execution_backtest.jsonl"
        rows = [
            {
                "trace_id": "trace-a",
                "decision_date": "2024-01-02",
                "ts_code": "600001",
                "strategies": {
                    "strict_ai_entry": {"status": "filled", "pnl_pct": 5.0},
                    "next_open_baseline": {"status": "filled", "pnl_pct": 3.0},
                },
            },
            {
                "trace_id": "trace-b",
                "decision_date": "2024-01-01",
                "ts_code": "600002",
                "strategies": {"strict_ai_entry": {"status": "not_filled"}},
            },
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

        result = AgentEntryExecutionBacktestService().query_backtests(input_path=path, symbol="600001")
        assert result["exists"] is True
        assert result["total"] == 1
        assert result["page"] == 1
        assert result["page_size"] == 50
        assert result["total_pages"] == 1
        assert result["available_dates"] == ["2024-01-02"]
        assert result["items"][0]["trace_id"] == "trace-a"
        assert result["summary"]["fill_rate_pct"] == 100
        assert result["summary"]["avg_pnl_pct"]["strict_ai_entry"] == 5.0
        assert result["summary"]["strategy_metrics"]["strict_ai_entry"]["filled"] == 1
        assert result["summary"]["strategy_metrics"]["strict_ai_entry"]["win_rate_pct"] == 100
        assert result["summary"]["strategy_metrics"]["strict_ai_entry"]["compounded_pnl_pct"] == 5.0
        assert result["summary"]["headline_metrics"]["best_strategy"] == "strict_ai_entry"
        assert result["history_summary"]["total"] == 1
        assert result["history_summary"]["strategy_metrics"]["strict_ai_entry"]["win_rate_pct"] == 100
        paged = AgentEntryExecutionBacktestService().query_backtests(
            input_path=path,
            decision_date="2024-01-01",
            page=1,
            page_size=1,
        )
        assert paged["available_dates"] == ["2024-01-02", "2024-01-01"]
        assert paged["total"] == 1
        assert paged["items"][0]["trace_id"] == "trace-b"
        assert paged["history_summary"]["total"] == 2

        app = create_app()
        client = TestClient(app)
        output = Path(tmp) / "api_entry.jsonl"
        with patch.object(AgentEntryExecutionBacktestService, "default_output_path", staticmethod(lambda: output)):
            with patch.object(AgentEntryExecutionBacktestService, "default_trace_root", staticmethod(lambda: Path(tmp) / "agent_traces")):
                response = client.post("/api/v1/agent-entry-execution-backtests/rebuild", params={"limit": 10})
                assert response.status_code == 200
                assert response.json()["review_count"] == 0
                list_response = client.get("/api/v1/agent-entry-execution-backtests")
                assert list_response.status_code == 200
                assert list_response.json()["exists"] is True

        DatabaseManager.reset_instance()
