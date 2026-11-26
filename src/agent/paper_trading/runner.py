# -*- coding: utf-8 -*-
"""Daily settlement for the paper-trading shadow books.

The shadow account is a consumer, not a producer:

* it ingests artifacts that manual runs already persisted (selection traces and
  single-stock analysis history),
* it replays each session from those artifacts, and
* it marks the book to the close and appends exactly one NAV row per day.

Two books share the same execution rules, fees and quotes:

* ``A`` — strict chain: only buy when the AI final action is ``open``.
* ``B`` — price-first: buy whenever the engine entry zone is touched (and the
  name is not limit-locked up); the AI action is recorded but not executed.

Neither book ever calls an LLM, nor touches the real ``portfolio_*`` tables.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select

from src.agent.pricing.snapshot import resolve_completed_cn_as_of
from src.backtest.factor_eval.data import load_benchmark_series, normalize_benchmark_code
from src.config import get_config
from src.storage import DatabaseManager, StockDaily

from .artifacts import (
    SOURCE_SELECTION,
    ArtifactRecord,
    build_plan_builder,
    discover_selection_artifacts,
    discover_single_analysis,
    next_trading_day,
    resolve_engine_plan,
    trace_root,
)
from .rules import (
    Bar,
    PlanLevels,
    affordable_quantity,
    buy_fees,
    entry_fill_price,
    exit_instructions,
    is_limit_locked,
    sell_fees,
)
from .store import PaperTradingStore, resolve_store_dir, store_path_for

logger = logging.getLogger(__name__)

BOOK_A = "A"
BOOK_B = "B"
BOOKS = (BOOK_A, BOOK_B)

DEFAULT_BENCHMARK = "000985.CSI"

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_NO_BAR = "no_bar"
STATUS_REFUSED = "refused_historical_rewrite"


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------
def run_paper_books(
    *,
    as_of: date | None = None,
    config: Any = None,
    db: Any = None,
    books: Sequence[str] = BOOKS,
    ingest: bool = True,
    write_report: bool = True,
    force_reingest: bool = False,
) -> Dict[str, Any]:
    """Ingest new artifacts once, then settle each requested book."""
    config = config or get_config()
    if not bool(getattr(config, "agent_paper_trading_enabled", False)):
        return {"status": STATUS_DISABLED, "enabled": False, "books": {}}

    session_date = as_of or resolve_completed_cn_as_of(date.today())
    manager = db or DatabaseManager.get_instance()
    books = tuple(str(book).upper() for book in books if str(book).upper() in BOOKS) or BOOKS
    stores = {book: PaperTradingStore(store_path_for(config, book), book) for book in books}

    ingest_result = {"ingested": 0, "skipped": 0, "records": []}
    if ingest:
        ingest_result = ingest_artifacts(
            config=config,
            db=manager,
            session_date=session_date,
            stores=stores,
            force=force_reingest,
        )

    results: Dict[str, Any] = {}
    for book, store in stores.items():
        results[book] = run_paper_session(
            book=book,
            as_of=session_date,
            config=config,
            store=store,
            db=manager,
            write_report=False,
        )

    payload: Dict[str, Any] = {
        "status": STATUS_OK,
        "as_of": session_date.isoformat(),
        "ingest": {key: ingest_result.get(key) for key in ("ingested", "skipped")},
        "books": results,
    }
    if write_report:
        payload["report"] = build_paper_report(config=config, books=books)
    return payload


def run_paper_session(
    *,
    book: str = BOOK_A,
    as_of: date | None = None,
    config: Any = None,
    store: PaperTradingStore | None = None,
    db: Any = None,
    write_report: bool = True,
) -> Dict[str, Any]:
    """Settle one book for one session; idempotent per session date."""
    config = config or get_config()
    if not bool(getattr(config, "agent_paper_trading_enabled", False)):
        return {"status": STATUS_DISABLED, "enabled": False, "book": book}

    book = str(book or BOOK_A).upper()
    session_date = as_of or resolve_completed_cn_as_of(date.today())
    store = store or PaperTradingStore(store_path_for(config, book), book)
    initial_cash = store.ensure_account(
        float(getattr(config, "agent_paper_trading_initial_cash", 100000.0))
    )

    latest = store.latest_nav_date()
    if latest is not None and latest > session_date.isoformat():
        return {
            "status": STATUS_REFUSED,
            "book": book,
            "as_of": session_date.isoformat(),
            "latest_settled": latest,
            "message": "不能回写早于已结算日期的会话",
        }

    manager = db or DatabaseManager.get_instance()
    raw_benchmark = str(
        getattr(config, "agent_paper_trading_benchmark", DEFAULT_BENCHMARK) or DEFAULT_BENCHMARK
    )
    benchmark_code = normalize_benchmark_code(raw_benchmark) or raw_benchmark

    with manager.get_session() as db_session:
        has_session_bar = db_session.execute(
            select(StockDaily.code).where(StockDaily.date == session_date).limit(1)
        ).first()
        if has_session_bar is None:
            return {"status": STATUS_NO_BAR, "book": book, "as_of": session_date.isoformat()}

        previous_nav = store.latest_nav_before(session_date)
        previous_date = date.fromisoformat(previous_nav["as_of"]) if previous_nav else None
        positions = _carry_positions(store, previous_date)
        cash = float(previous_nav["cash"]) if previous_nav else initial_cash
        equity_ref = float(previous_nav["equity"]) if previous_nav else initial_cash

        store.expire_plans(session_date)
        active_plans = store.active_plans(session_date)
        wanted = set(positions) | {plan["symbol"] for plan in active_plans}
        session_bars = _load_bars(db_session, session_date, sorted(wanted))
        if not session_bars:
            return {"status": STATUS_NO_BAR, "book": book, "as_of": session_date.isoformat()}

        # Same-day rerun replaces, never appends: clear this session first.
        store.delete_day(session_date)

        trades: List[Dict[str, Any]] = []
        cash, trades, exited_symbols = _apply_sells(
            store=store,
            session_date=session_date,
            positions=positions,
            bars=session_bars,
            cash=cash,
            trades=trades,
        )

        cash, trades = _apply_buys(
            book=book,
            store=store,
            session_date=session_date,
            positions=positions,
            plans=active_plans,
            bars=session_bars,
            cash=cash,
            equity_ref=equity_ref,
            trades=trades,
            max_positions=int(getattr(config, "agent_paper_trading_max_positions", 5)),
            fallback_pct=float(getattr(config, "agent_paper_trading_max_position_pct", 20.0)),
            fill_mode=_fill_mode(config),
            blocked_symbols=exited_symbols,
        )

        for position in positions.values():
            bar = session_bars.get(position["symbol"])
            if bar is not None and bar.close > 0:
                position["last_price"] = bar.close

        market_value = round(
            sum(pos["quantity"] * float(pos["last_price"] or 0.0) for pos in positions.values()),
            2,
        )
        cash = round(cash, 2)
        equity = round(cash + market_value, 2)

        benchmark_close = _benchmark_close(config, benchmark_code, session_date)
        benchmark_nav = _benchmark_nav(store, initial_cash, benchmark_close)

        store.save_trades(session_date, trades)
        store.save_positions(session_date, positions.values())
        store.save_nav(
            session_date,
            {
                "cash": cash,
                "market_value": market_value,
                "equity": equity,
                "benchmark_close": benchmark_close,
                "benchmark_nav": benchmark_nav,
                "positions_count": len(positions),
            },
        )

        calendar = _calendar(db_session)
        _refresh_decisions(store, db_session, calendar=calendar, as_of=session_date)

    result = {
        "status": STATUS_OK,
        "book": book,
        "as_of": session_date.isoformat(),
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "positions_count": len(positions),
        "trades": len(trades),
        "benchmark_close": benchmark_close,
        "benchmark_nav": benchmark_nav,
    }
    if write_report:
        result["report"] = build_paper_report(config=config, books=[book])
    return result


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
def ingest_artifacts(
    *,
    config: Any,
    db: Any,
    session_date: date,
    stores: Dict[str, PaperTradingStore],
    force: bool = False,
) -> Dict[str, Any]:
    """Parse new artifacts once and book them into every store."""
    manager = db or DatabaseManager.get_instance()
    known: set[tuple[str, str]] = set()
    for store in stores.values():
        known |= store.ingested_run_ids()

    limit = int(getattr(config, "agent_paper_trading_max_ingest_per_run", 20) or 20)
    lookback = int(getattr(config, "agent_paper_trading_ingest_lookback_days", 7) or 7)
    since = session_date - timedelta(days=max(0, lookback))
    records: List[ArtifactRecord] = []
    with manager.get_session() as db_session:
        records.extend(
            discover_selection_artifacts(
                trace_root(config), known=known, limit=limit, since=since
            )
        )
        records.extend(
            discover_single_analysis(
                db_session,
                known=known,
                limit=limit,
                before=session_date,
                since=since,
            )
        )
        if not records:
            return {"ingested": 0, "skipped": 0, "records": []}

        builder = build_plan_builder(manager)
        calendar = _calendar(db_session)
        ingested = 0
        skipped = 0
        for record in sorted(records, key=lambda item: (item.decision_date, item.run_id)):
            _attach_engine_levels(db_session, record, builder=builder)
            payload = _record_payload(record)
            any_inserted = False
            for store in stores.values():
                if force:
                    _drop_ingest(store, record)
                inserted = store.record_ingest(
                    {
                        "source": record.source,
                        "run_id": record.run_id,
                        "decision_date": record.decision_date.isoformat(),
                        "artifact_path": record.artifact_path,
                        "ingested_at": datetime.now().isoformat(timespec="seconds"),
                        "payload": payload,
                    }
                )
                if inserted:
                    _materialize_plans(store, record, calendar=calendar)
                    any_inserted = True
            if any_inserted:
                ingested += 1
            else:
                skipped += 1
    return {"ingested": ingested, "skipped": skipped, "records": records}


def ingest_only(
    *,
    config: Any = None,
    db: Any = None,
    session_date: Optional[date] = None,
    books: Sequence[str] = BOOKS,
) -> Dict[str, Any]:
    """Book new artifacts without touching any NAV row."""
    config = config or get_config()
    if not bool(getattr(config, "agent_paper_trading_enabled", False)):
        return {"status": STATUS_DISABLED, "enabled": False, "ingested": 0, "skipped": 0}
    session_date = session_date or resolve_completed_cn_as_of(date.today())
    manager = db or DatabaseManager.get_instance()
    requested = tuple(str(book).upper() for book in books if str(book).upper() in BOOKS) or BOOKS
    stores = {book: PaperTradingStore(store_path_for(config, book), book) for book in requested}
    result = ingest_artifacts(
        config=config,
        db=manager,
        session_date=session_date,
        stores=stores,
    )
    return {
        "status": STATUS_OK,
        "as_of": session_date.isoformat(),
        "ingested": result.get("ingested", 0),
        "skipped": result.get("skipped", 0),
    }


def _record_payload(record: ArtifactRecord) -> Dict[str, Any]:
    return {
        "source": record.source,
        "run_id": record.run_id,
        "decision_date": record.decision_date.isoformat(),
        "meta": record.meta,
        "orders": [
            {
                "symbol": order.symbol,
                "name": order.name,
                "ai_action": order.ai_action,
                "ai_position_pct": order.ai_position_pct,
                "engine_position_pct": order.engine_position_pct,
                "execution_mode": order.execution_mode,
                "portfolio_action": order.portfolio_action,
                "engine_plan": order.engine_plan,
            }
            for order in record.orders
        ],
    }


def _attach_engine_levels(db_session: Any, record: ArtifactRecord, *, builder: Any) -> None:
    for order in record.orders:
        plan = resolve_engine_plan(
            db_session,
            order.symbol,
            record.decision_date,
            builder=builder,
        )
        order.engine_plan = plan
        if plan is not None and order.engine_position_pct is None:
            order.engine_position_pct = plan.get("engine_position_pct")


def _drop_ingest(store: PaperTradingStore, record: ArtifactRecord) -> None:
    store.delete_ingest(record.source, record.run_id)


def _materialize_plans(
    store: PaperTradingStore,
    record: ArtifactRecord,
    *,
    calendar: Sequence[date],
) -> None:
    valid_from = next_trading_day(calendar, record.decision_date)
    rows: List[Dict[str, Any]] = []
    for index, order in enumerate(record.orders):
        plan = order.engine_plan
        if plan is None:
            continue
        rows.append(
            {
                **plan,
                "symbol": order.symbol,
                "run_id": record.run_id,
                "source": record.source,
                "ai_action": order.ai_action,
                "ai_position_pct": order.ai_position_pct,
                "engine_position_pct": (
                    order.engine_position_pct
                    if order.engine_position_pct is not None
                    else plan.get("engine_position_pct")
                ),
                "valid_from": valid_from.isoformat(),
                "valid_until": plan.get("valid_until") or valid_from.isoformat(),
                "priority": index,
                "status": "active",
            }
        )
    if rows:
        store.save_plans(record.decision_date, rows)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def build_paper_report(
    *,
    config: Any = None,
    books: Sequence[str] = BOOKS,
) -> Dict[str, Any]:
    """Write the per-book NAV curves, decisions and the A/B comparison."""
    config = config or get_config()
    books = tuple(str(book).upper() for book in books if str(book).upper() in BOOKS) or BOOKS
    output_dir = resolve_output_dir(config)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark_symbol": str(getattr(config, "agent_paper_trading_benchmark", "000985") or "000985"),
        "as_of": None,
        "books": {},
        "comparison": {},
    }

    for book in books:
        store = PaperTradingStore(store_path_for(config, book), book)
        nav = store.nav_curve()
        decisions = store.list_decisions()
        initial_cash = store.initial_cash() or float(
            getattr(config, "agent_paper_trading_initial_cash", 100000.0)
        )
        latest = nav[-1] if nav else None
        equity = float(latest["equity"]) if latest else initial_cash
        benchmark_nav = (
            float(latest["benchmark_nav"])
            if latest and latest["benchmark_nav"] is not None
            else None
        )
        fills = [row for row in decisions if row.get("close_in_zone") is not None]
        payload["books"][book] = {
            "initial_cash": initial_cash,
            "account": {
                "cash": float(latest["cash"]) if latest else initial_cash,
                "market_value": float(latest["market_value"]) if latest else 0.0,
                "equity": equity,
                "return": round(equity / initial_cash - 1.0, 6) if initial_cash else None,
                "positions_count": int(latest["positions_count"]) if latest else 0,
            },
            "benchmark": {
                "nav": benchmark_nav,
                "return": round(benchmark_nav / initial_cash - 1.0, 6) if benchmark_nav else None,
                "excess_return": (
                    round((equity - benchmark_nav) / initial_cash, 6) if benchmark_nav else None
                ),
            },
            "summary": _decision_summary(decisions),
            "close_mode": {
                "fills": len(fills),
                "close_inside": sum(1 for row in fills if int(row["close_in_zone"]) == 1),
                "close_outside": sum(1 for row in fills if int(row["close_in_zone"]) == 0),
            },
            "nav_curve": nav,
            "decisions": decisions,
        }
        if latest:
            payload["as_of"] = latest["as_of"]
        _write_book_files(output_dir, book, nav, decisions)

    payload["comparison"] = _compare_books(payload["books"])
    _write_report_file(output_dir, payload)
    payload["output_dir"] = str(output_dir)
    return payload


def resolve_output_dir(config: Any) -> Path:
    configured = str(getattr(config, "agent_paper_trading_output_dir", "") or "").strip()
    if configured:
        return Path(configured)
    return resolve_store_dir(config) / "report"


def _compare_books(books: Dict[str, Any]) -> Dict[str, Any]:
    if BOOK_A not in books or BOOK_B not in books:
        return {}
    a = books[BOOK_A]
    b = books[BOOK_B]
    return {
        "return_a": a["account"]["return"],
        "return_b": b["account"]["return"],
        "return_diff_b_minus_a": (
            round((b["account"]["return"] or 0.0) - (a["account"]["return"] or 0.0), 6)
            if a["account"]["return"] is not None and b["account"]["return"] is not None
            else None
        ),
        "positions_a": a["account"]["positions_count"],
        "positions_b": b["account"]["positions_count"],
        "decisions_a": len(a["decisions"]),
        "decisions_b": len(b["decisions"]),
    }


def _write_book_files(
    output_dir: Path,
    book: str,
    nav: Sequence[Dict[str, Any]],
    decisions: Sequence[Dict[str, Any]],
) -> None:
    nav_path = output_dir / f"nav_curve_{book}.csv"
    with nav_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["as_of", "cash", "market_value", "equity", "benchmark_close", "benchmark_nav", "positions_count"]
        )
        for row in nav:
            writer.writerow(
                [
                    row.get("as_of"),
                    row.get("cash"),
                    row.get("market_value"),
                    row.get("equity"),
                    row.get("benchmark_close"),
                    row.get("benchmark_nav"),
                    row.get("positions_count"),
                ]
            )

    decision_path = output_dir / f"decisions_{book}.csv"
    with decision_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "decision_id",
                "symbol",
                "source",
                "run_id",
                "entry_as_of",
                "entry_price",
                "quantity",
                "status",
                "exit_as_of",
                "exit_price",
                "realized_return",
                "t5_date",
                "t5_return",
                "t20_date",
                "t20_return",
                "close_in_zone",
            ]
        )
        for row in decisions:
            writer.writerow(
                [
                    row.get("decision_id"),
                    row.get("symbol"),
                    row.get("source"),
                    row.get("run_id"),
                    row.get("entry_as_of"),
                    row.get("entry_price"),
                    row.get("quantity"),
                    row.get("status"),
                    row.get("exit_as_of"),
                    row.get("exit_price"),
                    row.get("realized_return"),
                    row.get("t5_date"),
                    row.get("t5_return"),
                    row.get("t20_date"),
                    row.get("t20_return"),
                    row.get("close_in_zone"),
                ]
            )


def _write_report_file(output_dir: Path, payload: Dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _decision_summary(decisions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [row for row in decisions if row.get("status") == "closed"]
    open_rows = [row for row in decisions if row.get("status") == "open"]
    settled = [row for row in decisions if row.get("status") == "settled"]
    realized = [float(row["realized_return"]) for row in closed if row.get("realized_return") is not None]
    return {
        "total": len(decisions),
        "open": len(open_rows),
        "closed": len(closed),
        "settled": len(settled),
        "win_rate": (
            round(sum(1 for value in realized if value > 0) / len(realized), 4) if realized else None
        ),
        "avg_realized_return": round(sum(realized) / len(realized), 6) if realized else None,
    }


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------
def _fill_mode(config: Any) -> str:
    mode = str(getattr(config, "agent_paper_trading_entry_fill_mode", "touch") or "touch")
    return mode if mode in ("touch", "close") else "touch"


def _carry_positions(store: PaperTradingStore, previous_date: Optional[date]) -> Dict[str, Dict[str, Any]]:
    if previous_date is None:
        return {}
    carried: Dict[str, Dict[str, Any]] = {}
    for row in store.positions_on(previous_date):
        position = dict(row)
        # T+1: everything held before today is sellable today.
        position["available_quantity"] = int(position["quantity"])
        carried[position["symbol"]] = position
    return carried


def _apply_sells(
    *,
    store: PaperTradingStore,
    session_date: date,
    positions: Dict[str, Dict[str, Any]],
    bars: Dict[str, Bar],
    cash: float,
    trades: List[Dict[str, Any]],
) -> tuple[float, List[Dict[str, Any]], set]:
    exited: set = set()
    for symbol in sorted(list(positions)):
        position = positions[symbol]
        bar = bars.get(symbol)
        if bar is None or not bar.tradable:
            continue
        if is_limit_locked(bar, symbol, "down"):
            continue
        for instruction in exit_instructions(position, bar):
            quantity = instruction.quantity
            if quantity <= 0:
                continue
            notional = instruction.price * quantity
            fees = sell_fees(notional, session_date)
            cash += notional - fees
            position["quantity"] = int(position["quantity"]) - quantity
            position["available_quantity"] = int(position["available_quantity"]) - quantity
            position["target_1_done"] = bool(instruction.target_1_done)
            trades.append(
                {
                    "trade_id": _trade_id(session_date, symbol, "sell", instruction.reason, len(trades)),
                    "symbol": symbol,
                    "side": "sell",
                    "price": instruction.price,
                    "quantity": quantity,
                    "fees": round(fees, 2),
                    "amount": round(notional, 2),
                    "reason": instruction.reason,
                    "decision_id": position.get("decision_id"),
                    "close_price": bar.close,
                }
            )
            if int(position["quantity"]) <= 0:
                exited.add(symbol)
                positions.pop(symbol, None)
                if position.get("decision_id"):
                    _close_decision(store, position["decision_id"], session_date, instruction.price)
                break
    return cash, trades, exited


def _apply_buys(
    *,
    book: str,
    store: PaperTradingStore,
    session_date: date,
    positions: Dict[str, Dict[str, Any]],
    plans: Sequence[Dict[str, Any]],
    bars: Dict[str, Bar],
    cash: float,
    equity_ref: float,
    trades: List[Dict[str, Any]],
    max_positions: int,
    fallback_pct: float,
    fill_mode: str,
    blocked_symbols: set,
) -> tuple[float, List[Dict[str, Any]]]:
    for plan in plans:
        symbol = str(plan.get("symbol") or "")
        if not symbol or symbol in positions or symbol in blocked_symbols:
            continue
        if len(positions) >= max(1, max_positions):
            break
        if not _book_allows(book, plan):
            continue
        bar = bars.get(symbol)
        if bar is None or not bar.tradable:
            continue
        if is_limit_locked(bar, symbol, "up"):
            continue
        levels = PlanLevels(
            symbol=symbol,
            plan_id=str(plan.get("plan_id") or ""),
            scenario_id=str(plan.get("scenario_id") or ""),
            entry_low=float(plan["entry_low"]),
            entry_high=float(plan["entry_high"]),
            stop_loss=float(plan["stop_loss"]),
            target_1=float(plan["target_1"]),
            target_2=plan.get("target_2"),
            target_1_exit_ratio=float(plan.get("target_1_exit_ratio") or 0.5),
            valid_until=plan.get("valid_until"),
        )
        price = entry_fill_price(bar, levels, mode=fill_mode)
        if price is None:
            continue

        budget_pct = _size_pct(book, plan, fallback_pct)
        budget = max(0.0, equity_ref * budget_pct / 100.0)
        quantity = affordable_quantity(
            cash=cash, budget=budget, price=price, trade_date=session_date
        )
        if quantity <= 0:
            continue

        notional = price * quantity
        fees = buy_fees(notional, session_date)
        cash -= notional + fees
        close_in_zone = levels.entry_low <= bar.close <= levels.entry_high
        decision_id = _decision_id(session_date, symbol, plan.get("run_id"), plan.get("source"))
        trades.append(
            {
                "trade_id": _trade_id(session_date, symbol, "buy", "entry", len(trades)),
                "symbol": symbol,
                "side": "buy",
                "price": round(price, 4),
                "quantity": quantity,
                "fees": round(fees, 2),
                "amount": round(notional, 2),
                "reason": "entry",
                "decision_id": decision_id,
                "run_id": plan.get("run_id"),
                "source": plan.get("source"),
                "close_price": bar.close,
                "close_in_zone": close_in_zone,
            }
        )
        positions[symbol] = {
            "symbol": symbol,
            "quantity": quantity,
            "available_quantity": 0,  # T+1: bought today cannot be sold today
            "avg_price": round((notional + fees) / quantity, 4),
            "last_price": bar.close,
            "stop_loss": levels.stop_loss,
            "target_1": levels.target_1,
            "target_2": levels.target_2,
            "target_1_exit_ratio": levels.target_1_exit_ratio,
            "target_1_done": 0,
            "decision_id": decision_id,
        }
        store.upsert_decision(
            {
                "decision_id": decision_id,
                "symbol": symbol,
                "source": plan.get("source"),
                "run_id": plan.get("run_id"),
                "entry_as_of": session_date.isoformat(),
                "entry_price": round(price, 4),
                "quantity": quantity,
                "entry_fees": round(fees, 2),
                "status": "open",
                "close_in_zone": close_in_zone,
            }
        )
    return cash, trades


def _book_allows(book: str, plan: Dict[str, Any]) -> bool:
    if book == BOOK_A:
        # Strict chain: the AI must have said "open".
        return str(plan.get("ai_action") or "") == "open"
    # Price-first book: engine levels decide; AI action is recorded only.
    return str(plan.get("source") or "") == SOURCE_SELECTION


def _size_pct(book: str, plan: Dict[str, Any], fallback_pct: float) -> float:
    candidates: List[Any] = []
    if book == BOOK_A:
        candidates = [plan.get("ai_position_pct"), plan.get("engine_position_pct")]
    else:
        candidates = [plan.get("engine_position_pct")]
    for value in candidates:
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            return pct
    return max(1.0, float(fallback_pct or 20.0))


def _close_decision(store: PaperTradingStore, decision_id: str, session_date: date, price: float) -> None:
    rows = [row for row in store.list_decisions() if row.get("decision_id") == decision_id]
    for row in rows:
        invested = float(row["entry_price"]) * int(row["quantity"]) + float(row.get("entry_fees") or 0.0)
        proceeds = price * int(row["quantity"]) - sell_fees(price * int(row["quantity"]), session_date)
        store.upsert_decision(
            {
                **row,
                "status": "closed",
                "exit_as_of": session_date.isoformat(),
                "exit_price": round(price, 4),
                "realized_return": round(proceeds / invested - 1.0, 6) if invested else None,
                "close_in_zone": row.get("close_in_zone"),
            }
        )


def _refresh_decisions(
    store: PaperTradingStore,
    db_session: Any,
    *,
    calendar: Sequence[date],
    as_of: date,
) -> None:
    cache: Dict[tuple[str, date], Optional[float]] = {}
    for row in store.list_decisions():
        decision = dict(row)
        entry_date = date.fromisoformat(decision["entry_as_of"])
        symbol = decision["symbol"]
        base = float(decision["entry_price"])
        if base <= 0:
            continue

        changes: Dict[str, Any] = {}
        for horizon in (5, 20):
            target_date = _trading_day_offset(calendar, entry_date, horizon)
            if target_date is None or target_date > as_of:
                continue
            close = _close_on_or_before(db_session, symbol, target_date, cache)
            if close is None:
                continue
            changes[f"t{horizon}_date"] = target_date.isoformat()
            changes[f"t{horizon}_return"] = round(
                close / base - 1.0 - 0.00151, 6
            )

        if decision.get("status") == "open" and len(changes) >= 4:
            changes["status"] = "settled"
        if changes:
            merged = {**decision, **changes, "close_in_zone": decision.get("close_in_zone")}
            store.upsert_decision(merged)


def _trading_day_offset(calendar: Sequence[date], start: date, offset: int) -> Optional[date]:
    try:
        index = bisect.bisect_left(calendar, start)
    except TypeError:  # pragma: no cover - defensive
        return None
    if index >= len(calendar) or calendar[index] != start:
        index = bisect.bisect_right(calendar, start)
    target = index + offset
    return calendar[target] if 0 <= target < len(calendar) else None


# ---------------------------------------------------------------------------
# market data helpers
# ---------------------------------------------------------------------------
def _benchmark_close(config: Any, benchmark_code: str, day: date) -> Optional[float]:
    """Benchmark close from the index_daily cache, never from ``stock_daily``."""

    series = load_benchmark_series(
        benchmark_code,
        day,
        day,
        cache_dir=getattr(config, "index_benchmark_cache_dir", "") or None,
    )
    bar = series.bars.get(day) or {}
    return bar.get("close")


def _load_bars(session: Any, day: date, symbols: Sequence[str]) -> Dict[str, Bar]:
    if not symbols:
        return {}
    rows = session.execute(
        select(StockDaily).where(
            StockDaily.date == day,
            StockDaily.code.in_(list(symbols)),
        )
    ).scalars().all()
    bars: Dict[str, Bar] = {}
    for row in rows:
        bars[str(row.code)] = Bar(
            trade_date=day,
            open=float(row.open or 0.0),
            high=float(row.high or 0.0),
            low=float(row.low or 0.0),
            close=float(row.close or 0.0),
            volume=float(row.volume or 0.0),
            pct_chg=float(row.pct_chg) if row.pct_chg is not None else None,
        )
    return bars


def _calendar(session: Any) -> List[date]:
    rows = session.execute(select(StockDaily.date).distinct().order_by(StockDaily.date)).all()
    days = sorted({row[0] for row in rows if row[0] is not None})
    return list(days)


def _close_on_or_before(
    session: Any,
    code: str,
    day: date,
    cache: Optional[Dict[tuple[str, date], Optional[float]]] = None,
) -> Optional[float]:
    if cache is not None and (code, day) in cache:
        return cache[(code, day)]
    row = session.execute(
        select(StockDaily.close)
        .where(StockDaily.code == code, StockDaily.date <= day)
        .order_by(StockDaily.date.desc())
        .limit(1)
    ).first()
    value = float(row[0]) if row and row[0] is not None else None
    if cache is not None:
        cache[(code, day)] = value
    return value


def _benchmark_nav(store: PaperTradingStore, initial_cash: float, close: Optional[float]) -> Optional[float]:
    if close is None or close <= 0:
        return None
    nav = store.nav_curve()
    base_close = None
    for row in nav:
        if row.get("benchmark_close"):
            base_close = float(row["benchmark_close"])
            break
    if base_close is None or base_close <= 0:
        return round(initial_cash, 2)
    return round(initial_cash * close / base_close, 2)


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------
def _trade_id(day: date, symbol: str, side: str, reason: str, index: int) -> str:
    raw = f"{day.isoformat()}|{symbol}|{side}|{reason}|{index}"
    return "pt-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _decision_id(day: date, symbol: str, run_id: Optional[str], source: Optional[str]) -> str:
    raw = f"{day.isoformat()}|{symbol}|{run_id or ''}|{source or ''}"
    return "pd-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "BOOKS",
    "BOOK_A",
    "BOOK_B",
    "STATUS_DISABLED",
    "STATUS_NO_BAR",
    "STATUS_OK",
    "STATUS_REFUSED",
    "build_paper_report",
    "ingest_artifacts",
    "ingest_only",
    "resolve_output_dir",
    "run_paper_books",
    "run_paper_session",
]
