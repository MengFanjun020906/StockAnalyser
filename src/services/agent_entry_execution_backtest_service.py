# -*- coding: utf-8 -*-
"""Offline entry-execution backtest for final-stock-selection outputs.

This service stays read-only. It replays only the stocks that appear in the
final stock-selection report and evaluates entry/stop/take-profit/timeout
execution against local daily bars.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.config import get_config
from src.repositories.stock_repo import StockRepository
from src.storage import DatabaseManager


DEFAULT_ENTRY_EXPIRY_DAYS = 5
DEFAULT_MAX_HOLD_DAYS = 20
MAX_SYMBOLS_PER_TRACE = 4
STRATEGY_NAMES = ("strict_ai_entry", "next_open_baseline", "atr_elastic_entry", "breakout_fallback_entry")
DEFAULT_MINUTE_FREQUENCY = "5"
DEFAULT_MINUTE_ADJUSTFLAG = "3"
REJECT_ACTIONS = {"reject", "skip", "avoid"}
ACTIONABLE_ENTRY_ACTIONS = {"", "open", "buy", "add", "wait", "monitor", "plain_wait", "conditional_open"}


@dataclass(frozen=True)
class EntryExecutionBacktestResult:
    trace_count: int
    review_count: int
    output_path: Optional[str] = None
    skipped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_count": self.trace_count,
            "review_count": self.review_count,
            "output_path": self.output_path,
            "skipped": self.skipped,
        }


class AgentEntryExecutionBacktestService:
    """Build backtest rows from final stock-selection traces."""

    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        stock_repo: Optional[StockRepository] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.stock_repo = stock_repo or StockRepository(self.db)

    @staticmethod
    def default_trace_root() -> Path:
        return Path(get_config().database_path).expanduser().resolve().parent / "agent_traces"

    @staticmethod
    def default_output_path() -> Path:
        return Path("data/agent_reviews/entry_execution_backtest.jsonl")

    @staticmethod
    def default_insight_output_path() -> Path:
        return Path("data/agent_reviews/insights/agent_entry_execution_backtest.md")

    def build_backtests(
        self,
        *,
        trace_root: Optional[Path] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        root = Path(trace_root) if trace_root is not None else self.default_trace_root()
        rows: List[Dict[str, Any]] = []
        for trace_dir in _iter_trace_dirs(root, limit=limit):
            rows.extend(self.build_backtests_for_trace(trace_dir=trace_dir))
        return rows

    def build_backtests_for_trace(self, *, trace_dir: Path) -> List[Dict[str, Any]]:
        plan_items = self.collect_trade_plans_for_trace(trace_dir=trace_dir)
        rows: List[Dict[str, Any]] = []
        for item in plan_items:
            trade_plan = item["trade_plan"]
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            if trade_plan.get("parse_status") == "failed":
                continue

            strategy_results, warnings, price_data = self._run_strategies(
                symbol=str(trade_plan.get("ts_code") or ""),
                decision_date=_parse_date(trade_plan.get("decision_date")) or date.today(),
                trade_plan=trade_plan,
            )
            if price_data.get("status") == "invalid_trade_plan" or "invalid_entry_zone" in warnings:
                continue
            rows.append(
                _build_row(
                    Path(str(item.get("trace_dir") or trace_dir)),
                    summary,
                    trade_plan,
                    evaluation=strategy_results.get("strict_ai_entry", {}),
                    strategy_results=strategy_results,
                    warnings=warnings,
                    price_data=price_data,
                )
            )
        return rows

    def collect_trade_plans(
        self,
        *,
        trace_root: Optional[Path] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Collect normalized trade plans from final stock-selection reports only."""
        root = Path(trace_root) if trace_root is not None else self.default_trace_root()
        items: List[Dict[str, Any]] = []
        for trace_dir in _iter_trace_dirs(root, limit=limit):
            items.extend(self.collect_trade_plans_for_trace(trace_dir=trace_dir))
        return items

    def collect_trade_plans_for_trace(self, *, trace_dir: Path) -> List[Dict[str, Any]]:
        """Return final-report TradePlan items for one Agent trace."""
        final_report = _read_json(trace_dir / "final_report.json")
        if not final_report:
            stock_selection = _read_json(trace_dir / "stock_selection.json")
            final_report = stock_selection.get("final_report_json") if isinstance(stock_selection, dict) else {}
        if not isinstance(final_report, dict) or not final_report:
            return []
        if not _is_stock_selection_report(final_report):
            return []

        summary = _read_json(trace_dir / "summary.json")
        decision_date = _resolve_decision_date(final_report, trace_dir)
        positions = _nested_dict(final_report, "portfolio_allocation", "full").get("positions_plan")
        pricing_matrix = _nested_dict(final_report, "pricing_agent", "full").get("if_then_order_matrix")
        if not isinstance(positions, list):
            positions = []
        if not isinstance(pricing_matrix, list):
            pricing_matrix = []

        symbols = _extract_review_symbols(final_report)
        items: List[Dict[str, Any]] = []
        for idx, symbol_info in enumerate(symbols[:MAX_SYMBOLS_PER_TRACE], start=1):
            symbol = str(symbol_info.get("code") or "").strip()
            if not symbol:
                continue
            plan = _plan_for_symbol(positions, symbol)
            pricing = _plan_for_symbol(pricing_matrix, symbol)
            rank = _safe_int(symbol_info.get("rank")) or idx
            trade_plan = _build_trade_plan(
                trace_dir=trace_dir,
                decision_date=decision_date,
                symbol=symbol,
                name=symbol_info.get("name") or plan.get("name") or pricing.get("name"),
                rank=rank,
                plan=plan,
                pricing=pricing,
            )
            items.append(
                {
                    "trace_id": _trace_id(trace_dir, summary),
                    "trace_dir": str(trace_dir),
                    "summary": summary,
                    "decision_date": decision_date.isoformat(),
                    "ts_code": trade_plan.get("ts_code"),
                    "name": trade_plan.get("name"),
                    "rank": trade_plan.get("rank"),
                    "trade_plan": trade_plan,
                }
            )
        return items

    def write_backtests(
        self,
        backtests: Iterable[Dict[str, Any]],
        *,
        output_path: Optional[Path] = None,
    ) -> EntryExecutionBacktestResult:
        rows = list(backtests)
        path = Path(output_path) if output_path is not None else self.default_output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
                fh.write("\n")
        tmp.replace(path)
        return EntryExecutionBacktestResult(
            trace_count=len({row.get("trace_id") for row in rows}),
            review_count=len(rows),
            output_path=str(path),
            skipped=0,
        )

    def build_and_write(
        self,
        *,
        trace_root: Optional[Path] = None,
        output_path: Optional[Path] = None,
        limit: Optional[int] = None,
    ) -> EntryExecutionBacktestResult:
        root = Path(trace_root) if trace_root is not None else self.default_trace_root()
        trace_dirs = list(_iter_trace_dirs(root, limit=limit))
        rows: List[Dict[str, Any]] = []
        for trace_dir in trace_dirs:
            rows.extend(self.build_backtests_for_trace(trace_dir=trace_dir))
        result = self.write_backtests(rows, output_path=output_path)
        return EntryExecutionBacktestResult(
            trace_count=len(trace_dirs),
            review_count=result.review_count,
            output_path=result.output_path,
            skipped=max(0, len(trace_dirs) - len({row.get("trace_id") for row in rows})),
        )

    def query_backtests(
        self,
        *,
        input_path: Optional[Path] = None,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        decision_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        limit: int = 200,
    ) -> Dict[str, Any]:
        path = Path(input_path) if input_path is not None else self.default_output_path()
        rows = _read_jsonl(path)
        base_filtered = _filter_rows(rows, strategy=strategy, symbol=symbol, decision_date=None)
        available_dates = _available_decision_dates(base_filtered)
        filtered = _filter_rows(base_filtered, strategy=None, symbol=None, decision_date=decision_date)
        filtered.sort(key=lambda item: (str(item.get("decision_date") or ""), str(item.get("trace_id") or "")), reverse=True)
        safe_page_size = max(1, min(int(page_size or 50), 200))
        safe_page = max(1, int(page or 1))
        offset = (safe_page - 1) * safe_page_size
        safe_limit = max(1, min(int(limit or 200), 1000))
        items = filtered[offset: offset + safe_page_size]
        return {
            "source_path": str(path),
            "exists": path.exists(),
            "total": len(filtered),
            "page": safe_page,
            "page_size": safe_page_size,
            "total_pages": (len(filtered) + safe_page_size - 1) // safe_page_size if filtered else 0,
            "available_dates": available_dates,
            "items": items[:safe_limit],
            "summary": _summarize_rows(filtered),
            "history_summary": _summarize_rows(base_filtered),
        }

    def build_insight_markdown(
        self,
        *,
        input_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
        min_samples: int = 12,
        top_n: int = 12,
    ) -> Dict[str, Any]:
        source = Path(input_path) if input_path is not None else self.default_output_path()
        target = Path(output_path) if output_path is not None else self.default_insight_output_path()
        rows = _read_jsonl(source)
        safe_min_samples = max(1, int(min_samples or 12))
        safe_top_n = max(1, min(int(top_n or 12), 50))
        markdown = _render_markdown(source_path=source, rows=rows, min_samples=safe_min_samples, top_n=safe_top_n)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(markdown, encoding="utf-8")
        tmp.replace(target)
        return {
            "row_count": len(rows),
            "output_path": str(target),
            "min_samples": safe_min_samples,
            "top_n": safe_top_n,
        }

    def _run_strategies(
        self,
        *,
        symbol: str,
        decision_date: date,
        trade_plan: Dict[str, Any],
    ) -> tuple[Dict[str, Dict[str, Any]], List[str], Dict[str, Any]]:
        code_candidates = _stock_code_candidates(symbol)
        max_window = int(trade_plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)
        entry_expiry = int(trade_plan.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS)
        eval_window = max_window + entry_expiry

        for code in code_candidates:
            minute_bars = self.stock_repo.get_forward_minute_bars(
                code=code,
                analysis_date=decision_date,
                eval_window_days=eval_window,
                frequency=DEFAULT_MINUTE_FREQUENCY,
                adjustflag=DEFAULT_MINUTE_ADJUSTFLAG,
            )
            if minute_bars:
                sanitized_trade_plan, validation_warnings = _sanitize_trade_plan_against_bars(trade_plan, minute_bars)
                if "invalid_entry_zone" in validation_warnings:
                    return (
                        {name: {"status": "invalid_trade_plan", "limits": validation_warnings} for name in STRATEGY_NAMES},
                        validation_warnings,
                        {
                            "granularity": "minute",
                            "source": "stock_minute_bars",
                            "status": "invalid_trade_plan",
                            "code": code,
                            "bar_count": len(minute_bars),
                            "frequency": DEFAULT_MINUTE_FREQUENCY,
                            "adjustflag": DEFAULT_MINUTE_ADJUSTFLAG,
                            "first_bar_at": _value_to_iso(_bar_timestamp(minute_bars[0])),
                            "last_bar_at": _value_to_iso(_bar_timestamp(minute_bars[-1])),
                            "daily_bars": _aggregate_daily_bars(minute_bars),
                        },
                    )
                trade_plan.update(sanitized_trade_plan)
                strict = _simulate_strict(
                    strategy_name="strict_ai_entry",
                    start_price=0.0,
                    forward_bars=minute_bars,
                    trade_plan=sanitized_trade_plan,
                )
                next_open = _simulate_next_open(
                    strategy_name="next_open_baseline",
                    start_price=0.0,
                    forward_bars=minute_bars,
                    trade_plan=sanitized_trade_plan,
                )
                elastic = _simulate_elastic(
                    strategy_name="atr_elastic_entry",
                    start_price=0.0,
                    forward_bars=minute_bars,
                    trade_plan=sanitized_trade_plan,
                )
                breakout = _simulate_breakout(
                    strategy_name="breakout_fallback_entry",
                    start_price=0.0,
                    forward_bars=minute_bars,
                    trade_plan=sanitized_trade_plan,
                )
                return {
                    "strict_ai_entry": strict,
                    "next_open_baseline": next_open,
                    "atr_elastic_entry": elastic,
                    "breakout_fallback_entry": breakout,
                }, validation_warnings, {
                    "granularity": "minute",
                    "source": "stock_minute_bars",
                    "code": code,
                    "bar_count": len(minute_bars),
                    "frequency": DEFAULT_MINUTE_FREQUENCY,
                    "adjustflag": DEFAULT_MINUTE_ADJUSTFLAG,
                    "first_bar_at": _value_to_iso(_bar_timestamp(minute_bars[0])),
                    "last_bar_at": _value_to_iso(_bar_timestamp(minute_bars[-1])),
                    "daily_bars": _aggregate_daily_bars(minute_bars),
                }

        start_bar = None
        start_code = ""
        for code in code_candidates:
            start_bar = self.stock_repo.get_start_daily(code=code, analysis_date=decision_date)
            if start_bar is not None:
                start_code = code
                break
        if start_bar is None or not start_bar.close or float(start_bar.close) <= 0:
            return (
                {name: {"status": "insufficient_start_price", "limits": ["missing_start_bar"]} for name in STRATEGY_NAMES},
                ["missing_start_bar"],
                {"granularity": "none", "status": "missing_start_bar"},
            )

        forward_bars = self.stock_repo.get_forward_bars(code=start_code or symbol, analysis_date=decision_date, eval_window_days=eval_window)
        if not forward_bars:
            return (
                {name: {"status": "insufficient_forward_bars", "limits": ["missing_forward_bars"]} for name in STRATEGY_NAMES},
                ["missing_forward_bars"],
                {"granularity": "daily", "source": "stock_daily", "status": "missing_forward_bars", "code": start_code or symbol},
            )

        sanitized_trade_plan, validation_warnings = _sanitize_trade_plan_against_bars(trade_plan, forward_bars)
        if "invalid_entry_zone" in validation_warnings:
            return (
                {name: {"status": "invalid_trade_plan", "limits": validation_warnings} for name in STRATEGY_NAMES},
                validation_warnings,
                {
                    "granularity": "daily",
                    "source": "stock_daily",
                    "status": "invalid_trade_plan",
                    "code": start_code or symbol,
                    "bar_count": len(forward_bars),
                    "first_bar_at": _value_to_iso(_bar_timestamp(forward_bars[0])),
                    "last_bar_at": _value_to_iso(_bar_timestamp(forward_bars[-1])),
                    "daily_bars": _aggregate_daily_bars(forward_bars),
                },
            )
        trade_plan.update(sanitized_trade_plan)

        strict = _simulate_strict(strategy_name="strict_ai_entry", start_price=float(start_bar.close), forward_bars=forward_bars, trade_plan=sanitized_trade_plan)
        next_open = _simulate_next_open(strategy_name="next_open_baseline", start_price=float(start_bar.close), forward_bars=forward_bars, trade_plan=sanitized_trade_plan)
        elastic = _simulate_elastic(strategy_name="atr_elastic_entry", start_price=float(start_bar.close), forward_bars=forward_bars, trade_plan=sanitized_trade_plan)
        breakout = _simulate_breakout(strategy_name="breakout_fallback_entry", start_price=float(start_bar.close), forward_bars=forward_bars, trade_plan=sanitized_trade_plan)
        return {
            "strict_ai_entry": strict,
            "next_open_baseline": next_open,
            "atr_elastic_entry": elastic,
            "breakout_fallback_entry": breakout,
        }, validation_warnings, {
            "granularity": "daily",
            "source": "stock_daily",
            "code": start_code or symbol,
            "bar_count": len(forward_bars),
            "first_bar_at": _value_to_iso(_bar_timestamp(forward_bars[0])),
            "last_bar_at": _value_to_iso(_bar_timestamp(forward_bars[-1])),
            "daily_bars": _aggregate_daily_bars(forward_bars),
        }


def _build_row(
    trace_dir: Path,
    summary: Dict[str, Any],
    trade_plan: Dict[str, Any],
    *,
    evaluation: Dict[str, Any],
    strategy_results: Dict[str, Dict[str, Any]],
    warnings: List[str],
    price_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "agent_entry_execution_backtest.v1",
        "trace_id": _trace_id(trace_dir, summary),
        "trace_dir": str(trace_dir),
        "decision_date": trade_plan.get("decision_date"),
        "ts_code": trade_plan.get("ts_code"),
        "name": trade_plan.get("name"),
        "rank": trade_plan.get("rank"),
        "trade_plan": trade_plan,
        "evaluation": evaluation,
        "strategies": strategy_results,
        "warnings": warnings,
        "parse_status": trade_plan.get("parse_status"),
        "price_data": price_data or {},
    }


def _build_trade_plan(
    *,
    trace_dir: Path,
    decision_date: date,
    symbol: str,
    name: Optional[str],
    rank: int,
    plan: Dict[str, Any],
    pricing: Dict[str, Any],
) -> Dict[str, Any]:
    source_text = " ".join(str(value or "") for value in (
        plan.get("entry_condition"),
        plan.get("stop_loss_condition"),
        plan.get("take_profit_condition"),
        pricing.get("entry_zone"),
        pricing.get("stop_loss"),
        pricing.get("take_profit"),
    ))
    entry_low, entry_high = _extract_price_range(plan.get("entry_condition") or pricing.get("entry_zone") or source_text)
    stop_loss = _extract_single_price(plan.get("stop_loss_condition") or pricing.get("stop_loss") or source_text)
    take_profit = _extract_single_price(plan.get("take_profit_condition") or pricing.get("take_profit") or source_text)
    breakout = _extract_single_price(pricing.get("breakout_trigger") or plan.get("breakout_trigger"))
    entry_rule = _infer_entry_rule(plan, pricing)
    parse_warnings: List[str] = []
    if entry_low is None and entry_high is None:
        parse_warnings.append("missing_entry_zone")
    parse_status = "ok" if not parse_warnings else "failed"
    if not any([entry_low, entry_high, stop_loss, take_profit, breakout]):
        parse_status = "failed"
        if "no_numeric_trade_levels" not in parse_warnings:
            parse_warnings.append("no_numeric_trade_levels")
    return {
        "schema_version": "agent_entry_execution_backtest.v1",
        "trace_id": _trace_id(trace_dir, {}),
        "decision_date": decision_date.isoformat(),
        "ts_code": _normalize_symbol(symbol),
        "name": name or symbol,
        "rank": rank,
        "source_stage": "portfolio_allocation" if plan else "pricing_agent",
        "entry_rule": entry_rule,
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high or entry_low,
        "breakout_trigger": breakout,
        "stop_loss_price": stop_loss,
        "take_profit_price": take_profit,
        "entry_expiry_days": int(plan.get("entry_expiry_days") or pricing.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS),
        "max_hold_days": int(plan.get("max_hold_days") or pricing.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS),
        "execution_mode": str(plan.get("execution_mode") or pricing.get("execution_mode") or "conditional_open"),
        "final_action": str(plan.get("action") or pricing.get("action") or "wait"),
        "parse_status": parse_status,
        "parse_warnings": parse_warnings,
    }


def _simulate_strict(*, strategy_name: str, start_price: float, forward_bars: List[Any], trade_plan: Dict[str, Any]) -> Dict[str, Any]:
    entry_low = trade_plan.get("entry_zone_low")
    entry_high = trade_plan.get("entry_zone_high")
    expiry = int(trade_plan.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS)
    max_hold = int(trade_plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)
    stop_loss = trade_plan.get("stop_loss_price")
    take_profit = trade_plan.get("take_profit_price")
    if entry_low is None and entry_high is None:
        return {"status": "not_filled", "entry_reason": "missing_entry_zone", "limits": ["missing_entry_zone"]}
    entry_fill = None
    entry_date = None
    entry_window = _indexed_bars_for_first_trading_days(forward_bars, expiry)
    for idx, bar in entry_window:
        low = _bar_value(bar, "low")
        high = _bar_value(bar, "high")
        if low is None or high is None:
            continue
        if entry_low is not None and high < float(entry_low):
            continue
        if entry_high is not None and low > float(entry_high):
            continue
        entry_fill = float(entry_high if entry_high is not None else entry_low)
        entry_date = _bar_timestamp(bar)
        start_index = idx
        break
    if entry_fill is None:
        return {
            "status": "not_filled",
            "entry_reason": "timeout",
            "entry_window_days": expiry,
            "max_high": _max_bar_value([bar for _, bar in entry_window], "high"),
            "limits": ["entry_timeout"],
        }
    return _exit_after_entry(
        strategy_name=strategy_name,
        entry_fill=entry_fill,
        entry_date=entry_date,
        entry_index=start_index,
        forward_bars=forward_bars,
        stop_loss=stop_loss,
        take_profit=take_profit,
        max_hold=max_hold,
    )


def _simulate_next_open(*, strategy_name: str, start_price: float, forward_bars: List[Any], trade_plan: Dict[str, Any]) -> Dict[str, Any]:
    if not forward_bars:
        return {"status": "insufficient_forward_bars", "limits": ["missing_forward_bars"]}
    first = forward_bars[0]
    entry_fill = _bar_value(first, "open")
    if entry_fill is None:
        return {"status": "insufficient_forward_bars", "limits": ["missing_open"]}
    return _exit_after_entry(
        strategy_name=strategy_name,
        entry_fill=float(entry_fill),
        entry_date=_bar_timestamp(first),
        entry_index=0,
        forward_bars=forward_bars,
        stop_loss=trade_plan.get("stop_loss_price"),
        take_profit=trade_plan.get("take_profit_price"),
        max_hold=int(trade_plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS),
    )


def _simulate_elastic(*, strategy_name: str, start_price: float, forward_bars: List[Any], trade_plan: Dict[str, Any]) -> Dict[str, Any]:
    entry_low = trade_plan.get("entry_zone_low")
    entry_high = trade_plan.get("entry_zone_high")
    if entry_low is None and entry_high is None:
        return {"status": "not_filled", "entry_reason": "missing_entry_zone", "limits": ["missing_entry_zone"]}
    base_high = float(entry_high if entry_high is not None else entry_low)
    elastic_high = base_high * 1.02
    expiry = int(trade_plan.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS)
    max_hold = int(trade_plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)
    for idx, bar in _indexed_bars_for_first_trading_days(forward_bars, expiry):
        low = _bar_value(bar, "low")
        high = _bar_value(bar, "high")
        if low is None or high is None:
            continue
        if entry_low is not None and high < float(entry_low):
            continue
        if low > elastic_high:
            continue
        return _exit_after_entry(
            strategy_name=strategy_name,
            entry_fill=elastic_high,
            entry_date=_bar_timestamp(bar),
            entry_index=idx,
            forward_bars=forward_bars,
            stop_loss=trade_plan.get("stop_loss_price"),
            take_profit=trade_plan.get("take_profit_price"),
            max_hold=max_hold,
        )
    return {"status": "not_filled", "entry_reason": "timeout", "entry_window_days": expiry, "limits": ["entry_timeout"]}


def _simulate_breakout(*, strategy_name: str, start_price: float, forward_bars: List[Any], trade_plan: Dict[str, Any]) -> Dict[str, Any]:
    trigger = trade_plan.get("breakout_trigger")
    if trigger is None:
        return {"status": "strategy_skipped", "limits": ["missing_breakout_trigger"]}
    max_hold = int(trade_plan.get("max_hold_days") or DEFAULT_MAX_HOLD_DAYS)
    expiry = int(trade_plan.get("entry_expiry_days") or DEFAULT_ENTRY_EXPIRY_DAYS)
    for idx, bar in _indexed_bars_for_first_trading_days(forward_bars, expiry):
        high = _bar_value(bar, "high")
        if high is None or float(high) < float(trigger):
            continue
        entry_fill = float(trigger)
        return _exit_after_entry(
            strategy_name=strategy_name,
            entry_fill=entry_fill,
            entry_date=_bar_timestamp(bar),
            entry_index=idx,
            forward_bars=forward_bars,
            stop_loss=trade_plan.get("stop_loss_price"),
            take_profit=trade_plan.get("take_profit_price"),
            max_hold=max_hold,
        )
    return {"status": "not_filled", "entry_reason": "breakout_not_hit", "limits": ["breakout_not_hit"]}


def _exit_after_entry(
    *,
    strategy_name: str,
    entry_fill: float,
    entry_date: Any,
    entry_index: int,
    forward_bars: List[Any],
    stop_loss: Any,
    take_profit: Any,
    max_hold: int,
) -> Dict[str, Any]:
    stop_loss_value = float(stop_loss) if stop_loss is not None else None
    take_profit_value = float(take_profit) if take_profit is not None else None
    horizon = _bars_from_index_for_trading_days(forward_bars, entry_index, max_hold)
    if not horizon:
        return {"status": "insufficient_forward_bars", "limits": ["missing_exit_bars"]}
    for bar_offset, bar in enumerate(horizon, start=1):
        low = _bar_value(bar, "low")
        high = _bar_value(bar, "high")
        close = _bar_value(bar, "close")
        bar_date = _bar_timestamp(bar)
        if low is None or high is None or close is None:
            continue
        stop_hit = stop_loss_value is not None and float(low) <= stop_loss_value
        take_hit = take_profit_value is not None and float(high) >= take_profit_value
        holding_days = len(_distinct_trade_dates(horizon[:bar_offset]))
        if stop_hit and take_hit:
            exit_price = stop_loss_value
            return _finalize_exit(strategy_name, entry_fill, entry_date, bar_date, exit_price, "ambiguous_stop_first", holding_days, True)
        if stop_hit:
            return _finalize_exit(strategy_name, entry_fill, entry_date, bar_date, stop_loss_value, "stop_loss", holding_days, False)
        if take_hit:
            return _finalize_exit(strategy_name, entry_fill, entry_date, bar_date, take_profit_value, "take_profit", holding_days, False)
    last = horizon[-1]
    return _finalize_exit(strategy_name, entry_fill, entry_date, _bar_timestamp(last), _bar_value(last, "close"), "timeout_exit", len(_distinct_trade_dates(horizon)), False)


def _finalize_exit(
    strategy_name: str,
    entry_fill: float,
    entry_date: Any,
    exit_date: Any,
    exit_price: Any,
    reason: str,
    holding_days: int,
    ambiguous_bar: bool,
) -> Dict[str, Any]:
    if exit_price is None:
        return {"status": "insufficient_forward_bars", "limits": ["missing_exit_price"]}
    pnl_pct = (float(exit_price) / float(entry_fill) - 1) * 100 if float(entry_fill) else None
    return {
        "status": "filled",
        "strategy": strategy_name,
        "entry_date": _value_to_iso(entry_date),
        "entry_price": round(float(entry_fill), 4),
        "exit_date": _value_to_iso(exit_date),
        "exit_price": round(float(exit_price), 4),
        "exit_reason": reason,
        "holding_days": holding_days,
        "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
        "ambiguous_bar": ambiguous_bar,
    }


def _bar_value(bar: Any, field: str) -> Any:
    if isinstance(bar, dict):
        if field == "date" and "date" not in bar:
            return bar.get("bar_date") or bar.get("bar_datetime")
        return bar.get(field)
    value = getattr(bar, field, None)
    if field == "date" and value is None:
        return getattr(bar, "bar_date", None) or getattr(bar, "bar_datetime", None)
    return value


def _bar_timestamp(bar: Any) -> Any:
    if isinstance(bar, dict):
        for field in ("bar_datetime", "datetime", "timestamp", "date"):
            value = bar.get(field)
            if value:
                return value
        return None
    for field in ("bar_datetime", "datetime", "timestamp", "date"):
        value = getattr(bar, field, None)
        if value:
            return value
    return None


def _bar_trade_date(bar: Any) -> Optional[date]:
    if isinstance(bar, dict):
        raw = bar.get("bar_date") or bar.get("date") or bar.get("bar_datetime") or bar.get("datetime")
    else:
        raw = (
            getattr(bar, "bar_date", None)
            or getattr(bar, "date", None)
            or getattr(bar, "bar_datetime", None)
            or getattr(bar, "datetime", None)
        )
    parsed = _parse_date(raw)
    if parsed is not None:
        return parsed
    if isinstance(raw, str) and len(raw) >= 8:
        text = raw.strip()
        if re.match(r"^\d{8}", text):
            try:
                return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return None
    return None


def _distinct_trade_dates(bars: Sequence[Any]) -> List[date]:
    dates: List[date] = []
    seen = set()
    for bar in bars:
        trade_date = _bar_trade_date(bar)
        if trade_date is None or trade_date in seen:
            continue
        seen.add(trade_date)
        dates.append(trade_date)
    return dates


def _indexed_bars_for_first_trading_days(bars: Sequence[Any], trading_days: int) -> List[tuple[int, Any]]:
    max_days = max(1, int(trading_days or 1))
    selected: List[tuple[int, Any]] = []
    seen_dates: List[date] = []
    seen_set = set()
    for idx, bar in enumerate(bars):
        trade_date = _bar_trade_date(bar)
        if trade_date is None:
            continue
        if trade_date not in seen_set:
            if len(seen_dates) >= max_days:
                break
            seen_dates.append(trade_date)
            seen_set.add(trade_date)
        selected.append((idx, bar))
    return selected


def _bars_from_index_for_trading_days(bars: Sequence[Any], start_index: int, trading_days: int) -> List[Any]:
    max_days = max(1, int(trading_days or 1))
    selected: List[Any] = []
    seen_dates: List[date] = []
    seen_set = set()
    for bar in bars[max(0, start_index):]:
        trade_date = _bar_trade_date(bar)
        if trade_date is None:
            continue
        if trade_date not in seen_set:
            if len(seen_dates) >= max_days:
                break
            seen_dates.append(trade_date)
            seen_set.add(trade_date)
        selected.append(bar)
    return selected


def _aggregate_daily_bars(bars: Sequence[Any]) -> List[Dict[str, Any]]:
    grouped: Dict[date, Dict[str, Any]] = {}
    for bar in bars:
        trade_date = _bar_trade_date(bar)
        if trade_date is None:
            continue
        open_value = _bar_value(bar, "open")
        high_value = _bar_value(bar, "high")
        low_value = _bar_value(bar, "low")
        close_value = _bar_value(bar, "close")
        volume_value = _bar_value(bar, "volume")
        if close_value is None:
            continue
        item = grouped.get(trade_date)
        if item is None:
            grouped[trade_date] = {
                "date": trade_date.isoformat(),
                "open": _safe_float(open_value),
                "high": _safe_float(high_value),
                "low": _safe_float(low_value),
                "close": _safe_float(close_value),
                "volume": _safe_float(volume_value) or 0.0,
            }
            continue
        high_float = _safe_float(high_value)
        low_float = _safe_float(low_value)
        volume_float = _safe_float(volume_value)
        if high_float is not None:
            item["high"] = max(item.get("high") or high_float, high_float)
        if low_float is not None:
            item["low"] = min(item.get("low") or low_float, low_float)
        item["close"] = _safe_float(close_value)
        item["volume"] = float(item.get("volume") or 0.0) + float(volume_float or 0.0)
    return [grouped[key] for key in sorted(grouped)]


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _max_bar_value(bars: List[Any], field: str) -> Any:
    values = [v for v in (_bar_value(bar, field) for bar in bars) if isinstance(v, (int, float))]
    return max(values) if values else None


def _sanitize_trade_plan_against_bars(trade_plan: Dict[str, Any], bars: Sequence[Any]) -> tuple[Dict[str, Any], List[str]]:
    """Drop parsed price levels that are implausible against observed bars."""
    prices = _bar_price_values(bars)
    if not prices:
        return trade_plan, []

    market_low = min(prices)
    market_high = max(prices)
    market_mid = median(prices)
    sanitized = dict(trade_plan)
    warnings: List[str] = []
    entry_low = _safe_float(sanitized.get("entry_zone_low"))
    entry_high = _safe_float(sanitized.get("entry_zone_high"))

    if entry_low is None and entry_high is None:
        return sanitized, warnings
    if entry_low is None:
        entry_low = entry_high
    if entry_high is None:
        entry_high = entry_low
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low

    if entry_low is None or entry_high is None:
        return sanitized, warnings

    # A valid pullback/breakout plan can sit away from the current close, but
    # parsed list markers such as "1." become prices that are orders of
    # magnitude away from the instrument's actual trading range.
    if entry_high < market_low * 0.5 or entry_low > market_high * 1.5:
        warnings.append("invalid_entry_zone")
        return sanitized, warnings

    sanitized["entry_zone_low"] = entry_low
    sanitized["entry_zone_high"] = entry_high
    anchor = max(entry_high, market_mid * 0.5)

    for field, warning, low_ratio, high_ratio in (
        ("stop_loss_price", "invalid_stop_loss_price", 0.5, 1.25),
        ("take_profit_price", "invalid_take_profit_price", 0.5, 2.5),
        ("breakout_trigger", "invalid_breakout_trigger", 0.5, 2.5),
    ):
        value = _safe_float(sanitized.get(field))
        if value is None:
            continue
        if value < anchor * low_ratio or value > anchor * high_ratio:
            sanitized[field] = None
            warnings.append(warning)
        else:
            sanitized[field] = value
    return sanitized, warnings


def _bar_price_values(bars: Sequence[Any]) -> List[float]:
    values: List[float] = []
    for bar in bars:
        for field in ("open", "high", "low", "close"):
            value = _safe_float(_bar_value(bar, field))
            if value is not None and value > 0:
                values.append(value)
    return values


def _value_to_iso(value: Any) -> Optional[str]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if not value:
        return None
    text = str(value).strip()
    return text or None


def _extract_price_range(value: Any) -> tuple[Optional[float], Optional[float]]:
    text = str(value or "").strip()
    if not text:
        return None, None
    text = text.replace("元", "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-~到]\s*(\d+(?:\.\d+)?)", text)
    if match:
        if _is_percent_number(text, match) or (match.end() < len(text) and text[match.end()] == "%"):
            return None, None
        low = float(match.group(1))
        high = float(match.group(2))
        return (min(low, high), max(low, high))
    single = _extract_single_price(text)
    if single is not None:
        return single, single
    return None, None


def _extract_single_price(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        if _is_list_marker_number(text, match):
            continue
        if _is_percent_number(text, match):
            continue
        if _is_indicator_number(text, match):
            continue
        try:
            return float(match.group(0))
        except ValueError:
            continue
    return None


def _is_percent_number(text: str, match: re.Match[str]) -> bool:
    end = match.end()
    return end < len(text) and text[end] == "%"


def _is_indicator_number(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 3): match.start()].upper()
    return bool(re.search(r"(MA|RSI|CCI)$", prefix))


def _is_list_marker_number(text: str, match: re.Match[str]) -> bool:
    value = match.group(0)
    if "." in value:
        return False
    try:
        number = int(value)
    except ValueError:
        return False
    if number < 1 or number > 30:
        return False
    prefix = text[max(0, match.start() - 2): match.start()]
    suffix = text[match.end(): match.end() + 2]
    previous_is_boundary = match.start() == 0 or bool(re.search(r"[\s\n\r:：,，;；。.!！?？(（[【]", prefix[-1:]))
    next_is_marker = bool(suffix) and suffix[0] in ".．、)）]】"
    return previous_is_boundary and next_is_marker


def _infer_entry_rule(plan: Dict[str, Any], pricing: Dict[str, Any]) -> str:
    value = str(plan.get("execution_mode") or pricing.get("execution_mode") or "").strip().lower()
    if "break" in value:
        return "breakout"
    if "open" in value:
        return "immediate"
    if "wait" in value or "conditional" in value:
        return "pullback"
    return "unknown"


def _iter_trace_dirs(root: Path, *, limit: Optional[int]) -> List[Path]:
    if not root.exists():
        return []
    if (root / "final_report.json").exists() or (root / "stock_selection.json").exists():
        return [root]
    dirs = [path for path in root.iterdir() if path.is_dir()]
    dirs.sort(key=lambda item: item.name, reverse=True)
    return dirs[:limit] if limit is not None and limit > 0 else dirs


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return []
    return rows


def _is_stock_selection_report(final_report: Dict[str, Any]) -> bool:
    return bool(
        isinstance(final_report.get("judge_decision"), dict)
        and isinstance(final_report.get("portfolio_allocation"), dict)
    )


def _resolve_decision_date(final_report: Dict[str, Any], trace_dir: Path) -> date:
    market_regime = final_report.get("market_regime") if isinstance(final_report.get("market_regime"), dict) else {}
    trace_date = _parse_date(_date_from_trace_dir(trace_dir))
    report_dates = [
        _parse_date(market_regime.get("as_of")),
        _parse_date(market_regime.get("date")),
    ]
    for report_date in report_dates:
        if report_date is None:
            continue
        if trace_date is not None:
            lag_days = (trace_date - report_date).days
            if 0 < lag_days <= 60:
                return trace_date
        return report_date
    if trace_date is not None:
        return trace_date
    return date.today()


def _date_from_trace_dir(trace_dir: Path) -> Optional[str]:
    match = re.match(r"(\d{8})-", trace_dir.name)
    if not match:
        return None
    value = match.group(1)
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _trace_id(trace_dir: Path, summary: Dict[str, Any]) -> str:
    artifact_dir = str(summary.get("artifact_dir") or "")
    if artifact_dir:
        return Path(artifact_dir).name
    return trace_dir.name


def _nested_dict(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0] if "." in text else text


def _extract_review_symbols(final_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    positions = _nested_dict(final_report, "portfolio_allocation", "full").get("positions_plan")
    items: List[Dict[str, Any]] = []
    if isinstance(positions, list):
        items.extend(item for item in positions if _is_actionable_entry_item(item))
    if not items:
        pricing = _nested_dict(final_report, "pricing_agent", "full").get("if_then_order_matrix")
        if isinstance(pricing, list):
            items.extend(item for item in pricing if _is_actionable_entry_item(item))
    deduped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        code = str(item.get("code") or "").strip()
        if code and code not in deduped:
            deduped[code] = {"code": code, "name": item.get("name"), "rank": item.get("rank")}
    return list(deduped.values())


def _is_actionable_entry_item(item: Any) -> bool:
    if not isinstance(item, dict) or not item.get("code"):
        return False
    action = str(item.get("action") or item.get("final_action") or "").strip().lower()
    execution_mode = str(item.get("execution_mode") or "").strip().lower()
    if action in REJECT_ACTIONS or execution_mode in REJECT_ACTIONS:
        return False
    if action and action not in ACTIONABLE_ENTRY_ACTIONS:
        return False
    if execution_mode and execution_mode not in ACTIONABLE_ENTRY_ACTIONS and "conditional" not in execution_mode:
        return False
    return True


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _plan_for_symbol(items: Any, symbol: str) -> Dict[str, Any]:
    if not isinstance(items, list):
        return {}
    normalized = _normalize_symbol(symbol)
    for item in items:
        if isinstance(item, dict) and _normalize_symbol(item.get("code")) == normalized:
            return item
    return {}


def _filter_rows(
    rows: List[Dict[str, Any]],
    *,
    strategy: Optional[str],
    symbol: Optional[str],
    decision_date: Optional[str],
) -> List[Dict[str, Any]]:
    normalized_strategy = str(strategy or "").strip()
    normalized_symbol = _normalize_symbol(symbol)
    normalized_date = str(decision_date or "").strip()
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        if normalized_strategy and not _row_has_strategy(row, normalized_strategy):
            continue
        if normalized_symbol and _normalize_symbol(row.get("ts_code")) != normalized_symbol:
            continue
        if normalized_date and str(row.get("decision_date") or "")[:10] != normalized_date:
            continue
        filtered.append(row)
    return filtered


def _available_decision_dates(rows: List[Dict[str, Any]]) -> List[str]:
    values = {str(row.get("decision_date") or "")[:10] for row in rows if row.get("decision_date")}
    return sorted((value for value in values if value), reverse=True)


def _row_has_strategy(row: Dict[str, Any], strategy: str) -> bool:
    strategies = row.get("strategies") if isinstance(row.get("strategies"), dict) else {}
    return strategy in strategies


def _summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    strategy_counts: Dict[str, int] = {name: 0 for name in STRATEGY_NAMES}
    status_counts: Dict[str, int] = {}
    pnl_values: Dict[str, List[float]] = {name: [] for name in STRATEGY_NAMES}
    strategy_metrics: Dict[str, Dict[str, Any]] = {
        name: {
            "total": 0,
            "filled": 0,
            "not_filled": 0,
            "skipped": 0,
            "win_count": 0,
            "loss_count": 0,
            "flat_count": 0,
            "pnl_values": [],
        }
        for name in STRATEGY_NAMES
    }
    fill_count = 0
    for row in rows:
        strategies = row.get("strategies") if isinstance(row.get("strategies"), dict) else {}
        strict = strategies.get("strict_ai_entry") if isinstance(strategies.get("strict_ai_entry"), dict) else {}
        if strict.get("status") == "filled":
            fill_count += 1
        for name in STRATEGY_NAMES:
            result = strategies.get(name)
            if isinstance(result, dict):
                strategy_counts[name] += 1
                metrics = strategy_metrics[name]
                metrics["total"] += 1
                status = str(result.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
                if status == "filled":
                    metrics["filled"] += 1
                elif status == "not_filled":
                    metrics["not_filled"] += 1
                elif status == "strategy_skipped":
                    metrics["skipped"] += 1
                pnl = result.get("pnl_pct")
                if isinstance(pnl, (int, float)):
                    pnl_value = float(pnl)
                    pnl_values[name].append(pnl_value)
                    metrics["pnl_values"].append(pnl_value)
                    if pnl_value > 0:
                        metrics["win_count"] += 1
                    elif pnl_value < 0:
                        metrics["loss_count"] += 1
                    else:
                        metrics["flat_count"] += 1
    normalized_strategy_metrics = {
        name: _summarize_strategy_metrics(metrics)
        for name, metrics in strategy_metrics.items()
    }
    return {
        "total": total,
        "fill_rate_pct": round(fill_count / total * 100, 2) if total else 0,
        "strategy_counts": strategy_counts,
        "status_counts": status_counts,
        "avg_pnl_pct": {name: round(mean(values), 4) if values else None for name, values in pnl_values.items()},
        "median_pnl_pct": {name: round(median(values), 4) if values else None for name, values in pnl_values.items()},
        "strategy_metrics": normalized_strategy_metrics,
        "headline_metrics": _build_headline_metrics(normalized_strategy_metrics),
    }


def _summarize_strategy_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    total = int(metrics.get("total") or 0)
    filled = int(metrics.get("filled") or 0)
    values = [float(value) for value in metrics.get("pnl_values") or []]
    wins = int(metrics.get("win_count") or 0)
    losses = int(metrics.get("loss_count") or 0)
    avg_win = mean([value for value in values if value > 0]) if wins else None
    avg_loss = mean([value for value in values if value < 0]) if losses else None
    compounded = 1.0
    for value in values:
        compounded *= 1 + value / 100.0
    payoff_ratio = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss is not None and avg_loss != 0 else None
    return {
        "total": total,
        "filled": filled,
        "not_filled": int(metrics.get("not_filled") or 0),
        "skipped": int(metrics.get("skipped") or 0),
        "fill_rate_pct": round(filled / total * 100, 2) if total else 0,
        "win_count": wins,
        "loss_count": losses,
        "flat_count": int(metrics.get("flat_count") or 0),
        "win_rate_pct": round(wins / filled * 100, 2) if filled else 0,
        "avg_pnl_pct": round(mean(values), 4) if values else None,
        "median_pnl_pct": round(median(values), 4) if values else None,
        "total_pnl_pct": round(sum(values), 4) if values else None,
        "compounded_pnl_pct": round((compounded - 1) * 100, 4) if values else None,
        "best_pnl_pct": round(max(values), 4) if values else None,
        "worst_pnl_pct": round(min(values), 4) if values else None,
        "avg_win_pct": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_pct": round(avg_loss, 4) if avg_loss is not None else None,
        "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
    }


def _build_headline_metrics(strategy_metrics: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    ranked = [
        (name, metrics)
        for name, metrics in strategy_metrics.items()
        if isinstance(metrics.get("compounded_pnl_pct"), (int, float))
    ]
    if not ranked:
        return {
            "best_strategy": None,
            "best_compounded_pnl_pct": None,
            "best_win_rate_pct": None,
            "best_fill_rate_pct": None,
            "best_filled": 0,
        }
    ranked.sort(
        key=lambda item: (
            float(item[1].get("compounded_pnl_pct") or 0),
            float(item[1].get("win_rate_pct") or 0),
            float(item[1].get("avg_pnl_pct") or 0),
        ),
        reverse=True,
    )
    best_name, best_metrics = ranked[0]
    return {
        "best_strategy": best_name,
        "best_compounded_pnl_pct": best_metrics.get("compounded_pnl_pct"),
        "best_total_pnl_pct": best_metrics.get("total_pnl_pct"),
        "best_win_rate_pct": best_metrics.get("win_rate_pct"),
        "best_fill_rate_pct": best_metrics.get("fill_rate_pct"),
        "best_filled": best_metrics.get("filled"),
        "best_avg_pnl_pct": best_metrics.get("avg_pnl_pct"),
    }


def _render_markdown(*, source_path: Path, rows: List[Dict[str, Any]], min_samples: int, top_n: int) -> str:
    summary = _summarize_rows(rows)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Agent Entry Execution Backtest Insights",
        "",
        f"- 生成时间：{generated_at}",
        f"- 数据源：`{source_path}`",
        f"- 样本数：{len(rows)}",
        f"- 稳定洞察阈值：至少 {min_samples} 条样本",
        "",
        "## 概览",
        "",
        f"- 严格入场成交率：{summary.get('fill_rate_pct', 0):.2f}%",
        "",
        "## 策略汇总",
        "",
        "| 策略 | 平均收益 | 中位收益 |",
        "| --- | ---: | ---: |",
    ]
    for name in STRATEGY_NAMES[:top_n]:
        lines.append(
            f"| {name} | {_fmt_pct(summary.get('avg_pnl_pct', {}).get(name))} | {_fmt_pct(summary.get('median_pnl_pct', {}).get(name))} |"
        )
    lines.extend([
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ])
    for status, count in sorted(summary.get("status_counts", {}).items(), key=lambda item: (-int(item[1] or 0), item[0])):
        if int(count or 0) <= 0:
            continue
        lines.append(f"| {status} | {count} |")
    return "\n".join(lines)


def _fmt_pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    sign = "+" if float(value) > 0 else ""
    return f"{sign}{float(value):.2f}%"


def _stock_code_candidates(symbol: str) -> List[str]:
    text = str(symbol or "").strip()
    if not text:
        return []
    candidates = [text]
    if "." in text:
        candidates.append(text.split(".", 1)[0])
    upper = text.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        candidates.append(upper[:-3])
    return list(dict.fromkeys(candidates))


__all__ = [
    "AgentEntryExecutionBacktestService",
    "EntryExecutionBacktestResult",
    "MAX_SYMBOLS_PER_TRACE",
]
