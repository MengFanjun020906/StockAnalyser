# -*- coding: utf-8 -*-
"""Paper-trading shadow books: ledger, fills, A/B gates and idempotency."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.paper_trading import artifacts, runner
from src.agent.paper_trading.rules import (
    Bar,
    PlanLevels,
    affordable_quantity,
    buy_fees,
    entry_fill_price,
    exit_instructions,
    is_limit_locked,
    sell_fees,
)
from src.agent.paper_trading.store import PaperTradingStore

DAY = date(2026, 11, 24)
DECISION_DAY = date(2026, 11, 23)


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        return self

    def first(self):
        return ("600519",)


class _FakeDb:
    """Only allows ``with db.get_session()``; data helpers are patched."""

    def get_session(self):
        return _FakeSession()


def _config(tmp_path, **overrides):
    values = dict(
        agent_paper_trading_enabled=True,
        agent_paper_trading_db_dir=str(tmp_path / "books"),
        agent_paper_trading_trace_dir=str(tmp_path / "agent_traces"),
        agent_paper_trading_output_dir=str(tmp_path / "out"),
        agent_paper_trading_initial_cash=100000.0,
        agent_paper_trading_max_position_pct=20.0,
        agent_paper_trading_max_positions=5,
        agent_paper_trading_max_ingest_per_run=5,
        agent_paper_trading_benchmark="000985.CSI",
        agent_paper_trading_entry_fill_mode="touch",
        database_path=str(tmp_path / "main.db"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _plan(
    symbol="600519",
    *,
    ai_action="open",
    source=artifacts.SOURCE_SELECTION,
    ai_pct=20.0,
    engine_pct=None,
    entry_low=10.0,
    entry_high=10.5,
    valid_from="2026-11-24",
    valid_until="2026-12-01",
):
    return {
        "symbol": symbol,
        "plan_id": f"plan-{symbol}",
        "generation_status": "generated",
        "scenario_id": f"scn-{symbol}",
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": 9.5,
        "target_1": 11.5,
        "target_2": 12.5,
        "target_1_exit_ratio": 0.5,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "run_id": "run-1",
        "source": source,
        "ai_action": ai_action,
        "ai_position_pct": ai_pct,
        "engine_position_pct": engine_pct,
        "priority": 0,
    }


def _bar(open_=10.6, high=10.8, low=10.4, close=10.7, pct=1.0, volume=1000.0):
    return Bar(DAY, open_, high, low, close, volume, pct)


def _patch_session(monkeypatch, bars, calendar=None, closes=None):
    monkeypatch.setattr(runner, "_load_bars", lambda session, day, symbols: dict(bars))
    monkeypatch.setattr(runner, "_calendar", lambda session: list(calendar or [DAY]))

    def _fake_benchmark_close(config, code, day):
        bar = bars.get(code) or bars.get("000985.CSI") or bars.get("000985")
        return bar.close if bar is not None else None

    monkeypatch.setattr(runner, "_benchmark_close", _fake_benchmark_close)
    monkeypatch.setattr(
        runner,
        "_close_on_or_before",
        lambda session, code, day, cache=None: (closes or {}).get((code, day)),
    )


def _store(tmp_path, book="A"):
    return PaperTradingStore(tmp_path / "books" / f"{book.lower()}.db", book)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def test_touch_entry_fills_at_zone_upper_edge():
    levels = PlanLevels("600519", "p", "s", 10.0, 10.5, 9.5, 11.5, 12.5, 0.5, None)
    assert entry_fill_price(_bar(open_=10.6, low=10.4), levels) == 10.5
    assert entry_fill_price(_bar(open_=9.8, low=9.7), levels) == 9.8
    assert entry_fill_price(_bar(open_=10.8, low=10.6), levels) is None


def test_close_mode_requires_close_inside_zone():
    levels = PlanLevels("600519", "p", "s", 10.0, 10.5, 9.5, 11.5, 12.5, 0.5, None)
    assert entry_fill_price(_bar(low=10.1, close=10.4), levels, mode="close") == 10.4
    assert entry_fill_price(_bar(low=10.1, close=10.8), levels, mode="close") is None


def test_stop_takes_priority_over_target_on_same_bar():
    position = {
        "quantity": 1000,
        "available_quantity": 1000,
        "stop_loss": 9.5,
        "target_1": 11.5,
        "target_2": 12.5,
        "target_1_exit_ratio": 0.5,
        "target_1_done": 0,
    }
    result = exit_instructions(position, _bar(open_=10.6, high=12.6, low=9.4, close=10.0))
    assert len(result) == 1
    assert result[0].reason == "stop_loss"


def test_t1_blocks_same_day_sell():
    position = {
        "quantity": 1000,
        "available_quantity": 0,
        "stop_loss": 9.5,
        "target_1": 11.5,
        "target_2": None,
        "target_1_exit_ratio": 0.5,
        "target_1_done": 0,
    }
    assert exit_instructions(position, _bar(high=13.0, low=8.0)) == []


def test_limit_locked_blocks_buy_and_sell():
    one_word_up = Bar(DAY, 11.0, 11.0, 11.0, 11.0, 1000.0, 10.0)
    one_word_down = Bar(DAY, 9.0, 9.0, 9.0, 9.0, 1000.0, -10.0)
    assert is_limit_locked(one_word_up, "600519", "up") is True
    assert is_limit_locked(one_word_down, "600519", "down") is True
    assert is_limit_locked(one_word_up, "300750", "up") is False


def test_fees_are_charged_on_both_sides():
    assert buy_fees(10000.0, DAY) == pytest.approx(5.1, abs=0.01)
    assert sell_fees(10000.0, DAY) == pytest.approx(10.1, abs=0.01)
    assert affordable_quantity(cash=100000.0, budget=5000.0, price=10.5, trade_date=DAY) == 400


# ---------------------------------------------------------------------------
# artifact parsing / ingest
# ---------------------------------------------------------------------------
def test_parse_selection_artifact_reads_action_and_decision_date(tmp_path):
    trace = tmp_path / "agent_traces" / "20261123-180000-trace-abc"
    trace.mkdir(parents=True)
    payload = {
        "selection_context": {"run_id": "20261123-180000-trace-abc"},
        "final_report_json": {
            "portfolio_allocation": {
                "summary": {"portfolio_action": "wait"},
                "full": {
                    "positions_plan": [
                        {"code": "000001", "action": "open", "initial_position_pct": 15},
                        {"code": "600519", "action": "wait", "initial_position_pct": 0},
                    ]
                },
            }
        },
    }
    (trace / "stock_selection.json").write_text(json.dumps(payload), encoding="utf-8")

    records = artifacts.discover_selection_artifacts(tmp_path / "agent_traces")
    assert len(records) == 1
    record = records[0]
    assert record.decision_date == DECISION_DAY
    assert [(order.symbol, order.ai_action) for order in record.orders] == [
        ("000001", "open"),
        ("600519", "wait"),
    ]


def test_parse_single_analysis_maps_buy_to_open():
    row = SimpleNamespace(
        id=1,
        query_id="q-1",
        code="000001",
        name="平安银行",
        operation_advice="观望",
        raw_result=json.dumps({"decision_type": "buy"}),
    )
    record = artifacts.parse_single_analysis_row(row, decision_date=DAY)
    assert record is not None
    assert record.source == artifacts.SOURCE_SINGLE
    assert record.orders[0].ai_action == "open"


def test_ingest_is_idempotent_by_source_and_run_id(tmp_path):
    store = _store(tmp_path)
    record = {
        "source": artifacts.SOURCE_SELECTION,
        "run_id": "run-1",
        "decision_date": DAY.isoformat(),
        "artifact_path": "x",
        "ingested_at": "2026-11-24T18:10:00",
        "payload": {"orders": []},
    }
    assert store.record_ingest(record) is True
    assert store.record_ingest(record) is False
    assert len(store.ingests()) == 1


def test_ingest_materializes_plan_and_skips_duplicate(tmp_path, monkeypatch):
    config = _config(tmp_path)
    trace = tmp_path / "agent_traces" / "20261123-180000-trace-abc"
    trace.mkdir(parents=True)
    payload = {
        "selection_context": {"run_id": "20261123-180000-trace-abc"},
        "final_report_json": {
            "portfolio_allocation": {
                "summary": {"portfolio_action": "wait"},
                "full": {
                    "positions_plan": [
                        {"code": "000001", "action": "open", "initial_position_pct": 15}
                    ]
                },
            }
        },
    }
    (trace / "stock_selection.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(runner, "resolve_engine_plan", lambda *args, **kwargs: _plan("000001"))
    monkeypatch.setattr(runner, "_calendar", lambda session: [DECISION_DAY, DAY, date(2026, 11, 25)])
    monkeypatch.setattr(runner, "discover_single_analysis", lambda *args, **kwargs: [])

    store = _store(tmp_path, "A")
    first = runner.ingest_artifacts(
        config=config,
        db=_FakeDb(),
        session_date=DAY,
        stores={"A": store},
    )
    assert first["ingested"] == 1
    plan = store.plan_for("000001", DAY)
    assert plan is not None
    assert plan["valid_from"] == DAY.isoformat()
    assert plan["ai_action"] == "open"

    second = runner.ingest_artifacts(
        config=config,
        db=_FakeDb(),
        session_date=DAY,
        stores={"A": store},
    )
    assert second["ingested"] == 0
    assert len(store.ingests()) == 1


# ---------------------------------------------------------------------------
# A / B book gates
# ---------------------------------------------------------------------------
def test_a_book_requires_ai_open(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    store.save_plans(DECISION_DAY, [_plan(ai_action="wait")])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["status"] == "ok"
    assert result["trades"] == 0
    assert store.positions_on(DAY) == []


def test_a_book_buys_when_ai_says_open(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    store.save_plans(DECISION_DAY, [_plan(ai_action="open")])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["trades"] == 1
    trades = store.trades_on(DAY)
    assert trades[0]["price"] == 10.5
    assert trades[0]["quantity"] == 1900
    assert store.positions_on(DAY)[0]["available_quantity"] == 0  # T+1


def test_b_book_ignores_ai_wording_but_follows_engine_zone(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "B")
    store.save_plans(DECISION_DAY, [_plan(ai_action="wait", ai_pct=None, engine_pct=25.0)])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="B", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["trades"] == 1
    assert store.trades_on(DAY)[0]["quantity"] == 2300  # 25% of 100k at 10.5


def test_b_book_does_not_consume_single_analysis_artifacts(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "B")
    store.save_plans(
        DECISION_DAY,
        [_plan(ai_action="open", source=artifacts.SOURCE_SINGLE)],
    )
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="B", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["trades"] == 0


def test_plan_before_valid_from_is_not_executed(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "B")
    store.save_plans(DECISION_DAY, [_plan(valid_from="2026-11-25")])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="B", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["trades"] == 0


def test_close_inside_zone_flag_is_recorded(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    store.save_plans(DECISION_DAY, [_plan()])
    _patch_session(
        monkeypatch,
        {"600519": _bar(low=10.1, close=10.9), "000985": _bar(close=13.67)},
    )

    runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    trade = store.trades_on(DAY)[0]
    assert trade["close_in_zone"] == 0
    assert store.list_decisions()[0]["close_in_zone"] == 0


# ---------------------------------------------------------------------------
# session idempotency / no-artifact day
# ---------------------------------------------------------------------------
def test_day_without_artifacts_only_records_nav(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    _patch_session(monkeypatch, {"000985": _bar(close=13.67)})

    result = runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["status"] == "ok"
    assert result["trades"] == 0
    assert len(store.nav_curve()) == 1
    assert store.trades_on(DAY) == []


def test_same_day_rerun_overwrites_instead_of_appending(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "B")
    store.save_plans(DECISION_DAY, [_plan()])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})

    for _ in range(2):
        runner.run_paper_session(
            book="B", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
        )

    assert len(store.nav_curve()) == 1
    assert len(store.trades_on(DAY)) == 1
    assert len(store.positions_on(DAY)) == 1


def test_historical_rewrite_is_refused(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    _patch_session(monkeypatch, {"000985": _bar(close=13.67)})
    runner.run_paper_session(
        book="A", as_of=date(2026, 11, 25), config=config, store=store, db=_FakeDb(), write_report=False
    )
    result = runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )
    assert result["status"] == "refused_historical_rewrite"


def test_disabled_switch_is_a_noop(tmp_path):
    config = _config(tmp_path, agent_paper_trading_enabled=False)
    result = runner.run_paper_books(config=config, db=_FakeDb())
    assert result == {"status": "disabled", "enabled": False, "books": {}}


# ---------------------------------------------------------------------------
# exits / decisions / report
# ---------------------------------------------------------------------------
def test_exit_sells_and_closes_decision(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = _store(tmp_path, "A")
    store.save_plans(DECISION_DAY, [_plan()])
    _patch_session(monkeypatch, {"600519": _bar(), "000985": _bar(close=13.67)})
    runner.run_paper_session(
        book="A", as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
    )

    sell_day = date(2026, 11, 25)
    sell_bars = {
        "600519": Bar(sell_day, 11.6, 11.6, 9.4, 9.6, 1000.0, -9.0),
        "000985": _bar(close=13.5),
    }
    _patch_session(monkeypatch, sell_bars, calendar=[DAY, sell_day])
    result = runner.run_paper_session(
        book="A", as_of=sell_day, config=config, store=store, db=_FakeDb(), write_report=False
    )

    assert result["positions_count"] == 0
    sells = [trade for trade in store.trades_on(sell_day) if trade["side"] == "sell"]
    assert sells and sells[0]["reason"] == "stop_loss"
    decisions = store.list_decisions()
    assert decisions[0]["status"] == "closed"
    assert decisions[0]["realized_return"] is not None


def test_t5_and_t20_returns_are_recorded(tmp_path, monkeypatch):
    store = _store(tmp_path, "A")
    store.upsert_decision(
        {
            "decision_id": "pd-test",
            "symbol": "600519",
            "entry_as_of": DAY.isoformat(),
            "entry_price": 10.0,
            "quantity": 1000,
            "entry_fees": 5.0,
            "status": "open",
            "close_in_zone": 1,
        }
    )
    calendar = [date.fromordinal(DAY.toordinal() + offset) for offset in range(0, 25)]
    closes = {
        ("600519", calendar[5]): 11.0,
        ("600519", calendar[20]): 12.0,
    }
    monkeypatch.setattr(
        runner,
        "_close_on_or_before",
        lambda session, code, day, cache=None: closes.get((code, day)),
    )
    runner._refresh_decisions(store, _FakeSession(), calendar=calendar, as_of=calendar[20])

    decision = store.list_decisions()[0]
    assert decision["t5_date"] == calendar[5].isoformat()
    assert decision["t5_return"] == pytest.approx(0.1 - 0.00151, abs=1e-5)
    assert decision["t20_date"] == calendar[20].isoformat()
    assert decision["status"] == "settled"
    assert decision["close_in_zone"] == 1


def test_report_writes_both_books(tmp_path, monkeypatch):
    config = _config(tmp_path)
    bars = {"600519": _bar(), "000985": _bar(close=13.67)}
    _patch_session(monkeypatch, bars)
    for book, action in (("A", "open"), ("B", "wait")):
        store = _store(tmp_path, book)
        store.save_plans(DECISION_DAY, [_plan(ai_action=action, ai_pct=20.0, engine_pct=20.0)])
        runner.run_paper_session(
            book=book, as_of=DAY, config=config, store=store, db=_FakeDb(), write_report=False
        )

    report = runner.build_paper_report(config=config, books=["A", "B"])
    output = tmp_path / "out"

    assert (output / "report.json").exists()
    assert (output / "nav_curve_A.csv").exists()
    assert (output / "nav_curve_B.csv").exists()
    assert (output / "decisions_B.csv").exists()
    assert report["books"]["A"]["account"]["equity"] > 0
    assert report["books"]["B"]["close_mode"]["fills"] == 1
    assert report["comparison"]["decisions_b"] == 1
