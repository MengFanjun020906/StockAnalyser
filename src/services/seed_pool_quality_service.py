# -*- coding: utf-8 -*-
"""Seed Pool quality monitoring service."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import is_meaningful_stock_name
from src.repositories.seed_pool_quality_repo import SeedPoolQualityRepository
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "000001.SH"
BENCHMARK_BAOSTOCK_CODE = "sh.000001"
PRICE_LINE_META = {
    "bos_level": {"label": "BOS 支撑", "color": "#16a34a"},
    "invalidation_level": {"label": "证伪/止损", "color": "#dc2626"},
    "ma20_anchor": {"label": "MA20 锚点", "color": "#ca8a04"},
}
CN_MARKET_CLOSE_BUFFER = time(15, 30)
SEED_POOL_ROLLOVER_TIME = time(9, 0)
CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    amount: Optional[float] = None
    source: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "trade_date": self.trade_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "source": self.source,
        }


class MarketDataProvider:
    """Uniform daily OHLC provider for seed quality evaluation."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def get_daily_bars(self, symbol: str, start_date: date, end_date: date, market: str = "cn") -> List[DailyBar]:
        bars = self._load_from_db(symbol, start_date, end_date)
        if bars:
            return bars
        self._try_online_fill(symbol, start_date, end_date, market=market)
        return self._load_from_db(symbol, start_date, end_date)

    def get_daily_bars_from_db(self, symbol: str, start_date: date, end_date: date) -> List[DailyBar]:
        return self._load_from_db(symbol, start_date, end_date)

    def _load_from_db(self, symbol: str, start_date: date, end_date: date) -> List[DailyBar]:
        if _normalize_code(symbol) == BENCHMARK_CODE:
            sequoia_bars = _load_from_sequoia_db(symbol, start_date, end_date)
            if sequoia_bars:
                return sequoia_bars
        sequoia_bars = _load_from_sequoia_db(symbol, start_date, end_date)
        if sequoia_bars:
            return sequoia_bars
        db_bars: List[DailyBar] = []
        for code in _code_variants(symbol):
            rows = self.db.get_data_range(code, start_date, end_date)
            bars = [_bar_from_stock_daily(row, requested_code=symbol) for row in rows if _row_has_ohlc(row)]
            if bars:
                db_bars.extend(bars)
        return _merge_daily_bars(db_bars)

    def _try_online_fill(self, symbol: str, start_date: date, end_date: date, market: str = "cn") -> None:
        fetch_symbols = _fetch_variants(symbol)
        try:
            from data_provider import DataFetcherManager

            manager = DataFetcherManager()
            for fetch_symbol in fetch_symbols:
                try:
                    df, source = manager.get_daily_data(
                        fetch_symbol,
                        start_date=start_date.isoformat(),
                        end_date=end_date.isoformat(),
                    )
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        self.db.save_daily_data(df, symbol, f"SeedPoolQuality:{source}")
                        return
                except Exception as exc:
                    logger.debug("seed quality online fill failed for %s via %s: %s", symbol, fetch_symbol, exc)
        except Exception as exc:
            logger.debug("seed quality DataFetcherManager unavailable: %s", exc)


class SeedPoolQualityService:
    """Application service for seed-pool quality snapshots and evaluation."""

    def __init__(
        self,
        repo: Optional[SeedPoolQualityRepository] = None,
        market_data_provider: Optional[MarketDataProvider] = None,
    ) -> None:
        self.repo = repo or SeedPoolQualityRepository()
        self.market_data = market_data_provider or MarketDataProvider()

    def persist_candidate_discovery_snapshot(
        self,
        *,
        candidate_discovery: Dict[str, Any],
        run_id: str,
        trace_id: str,
        seed_date: Optional[date] = None,
        generated_at: Optional[datetime] = None,
        market: str = "cn",
        candidate_discovery_mode: str = "thesis_desk_committee",
    ) -> Dict[str, Any]:
        items = _extract_seed_items(candidate_discovery)
        if not items:
            return {"status": "skipped", "reason": "empty_seed_pool"}
        effective_generated_at = generated_at or datetime.now(CN_TZ)
        effective_seed_date = (
            seed_date
            or infer_seed_pool_snapshot_date(candidate_discovery)
            or effective_seed_pool_date(effective_generated_at)
        )
        outcomes = _extract_desk_outcomes_by_code(candidate_discovery)
        summary = candidate_discovery.get("seed_pool_summary") if isinstance(candidate_discovery.get("seed_pool_summary"), dict) else {}
        diagnostics = candidate_discovery.get("seed_pool_diagnostics") if isinstance(candidate_discovery.get("seed_pool_diagnostics"), list) else []
        result = self.repo.save_snapshot(
            run_id=run_id,
            trace_id=trace_id,
            seed_date=effective_seed_date,
            generated_at=effective_generated_at,
            market=market or str(candidate_discovery.get("market") or "cn"),
            candidate_discovery_mode=candidate_discovery_mode,
            status=str(candidate_discovery.get("status") or "ok"),
            error=str(candidate_discovery.get("error") or ""),
            source_summary=summary,
            diagnostics=[item for item in diagnostics if isinstance(item, dict)],
            items=items,
            desk_outcomes_by_code=outcomes,
        )
        return {"status": "ok", **result}

    def evaluate_seed_date(self, seed_date: date, *, limit: int = 500) -> Dict[str, Any]:
        pending = self.repo.list_items_requiring_evaluation(seed_date=seed_date, limit=limit, include_ok=True)
        preflight = self._preflight_evaluation(seed_date=seed_date, items=pending)
        results = [
            self.evaluate_item(
                int(item["item_id"]),
                db_only=True,
                expected_evaluation_date=preflight["expected_evaluation_date"],
            )
            for item in pending
        ]
        return {
            "seed_date": seed_date.isoformat(),
            "expected_evaluation_date": preflight["expected_evaluation_date"].isoformat(),
            "data_source": "local_db",
            "requested": len(pending),
            "updated": sum(1 for item in results if item.get("data_status") == "ok"),
            "failed": sum(1 for item in results if item.get("data_status") != "ok"),
            "results": results,
        }

    def evaluate_item(
        self,
        item_id: int,
        *,
        db_only: bool = False,
        expected_evaluation_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        item = self.repo.get_item_with_snapshot(item_id)
        if not item:
            return {"item_id": item_id, "data_status": "not_found", "error": "seed item not found"}
        seed_date = item["seed_date"]
        code = str(item["code"])
        market = str(item.get("market") or "cn")
        start_date = seed_date - timedelta(days=14)
        end_date = expected_evaluation_date or (seed_date + timedelta(days=14))
        if db_only:
            stock_bars = self.market_data.get_daily_bars_from_db(code, start_date, end_date)
            benchmark_bars = self.market_data.get_daily_bars_from_db(BENCHMARK_CODE, start_date, end_date)
        else:
            stock_bars = self.market_data.get_daily_bars(code, start_date, end_date, market=market)
            benchmark_bars = self.market_data.get_daily_bars(BENCHMARK_CODE, start_date, end_date, market=market)
        payload = _calculate_evaluation_payload(
            item_id=item_id,
            seed_date=seed_date,
            stock_bars=stock_bars,
            benchmark_bars=benchmark_bars,
            expected_evaluation_date=expected_evaluation_date,
        )
        self.repo.upsert_evaluation(payload)
        return {
            "item_id": item_id,
            "code": code,
            "evaluation_date": payload.get("evaluation_date").isoformat() if isinstance(payload.get("evaluation_date"), date) else None,
            "data_status": payload.get("data_status"),
            "liquidity_status": payload.get("liquidity_status"),
            "alpha_return_pct": payload.get("alpha_return_pct"),
            "error": payload.get("error") or "",
        }

    def _preflight_evaluation(self, *, seed_date: date, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {"expected_evaluation_date": _next_weekday(seed_date)}
        expected, benchmark_bars, has_future_benchmark_bar = self._resolve_expected_evaluation_date(seed_date)
        now = datetime.now(CN_TZ)
        if now.date() < expected or (now.date() == expected and now.time() < CN_MARKET_CLOSE_BUFFER):
            raise SeedPoolEvaluationPreconditionError(
                status_code=409,
                error="evaluation_not_due",
                message=f"{seed_date.isoformat()} 的 T+1 评估日预计为 {expected.isoformat()}，当前尚未到收盘后更新时间。",
                details={
                    "seed_date": seed_date.isoformat(),
                    "expected_evaluation_date": expected.isoformat(),
                    "current_time": now.isoformat(),
                },
            )

        if not benchmark_bars:
            raise SeedPoolEvaluationPreconditionError(
                status_code=409,
                error="missing_benchmark_ohlc",
                message="本地数据库缺少上证指数行情，无法识别 seed 基准日和下一交易日；请先同步 OHLC。",
                details={
                    "seed_date": seed_date.isoformat(),
                    "fallback_expected_evaluation_date": expected.isoformat(),
                    "benchmark_code": BENCHMARK_CODE,
                    "data_source": "local_db",
                },
            )
        has_seed_benchmark_bar = any(bar.trade_date <= seed_date for bar in benchmark_bars)
        if not has_seed_benchmark_bar:
            raise SeedPoolEvaluationPreconditionError(
                status_code=409,
                error="missing_benchmark_ohlc",
                message="本地数据库缺少上证指数 seed 基准日行情，无法计算 Alpha；请先同步 OHLC。",
                details={
                    "seed_date": seed_date.isoformat(),
                    "expected_evaluation_date": expected.isoformat(),
                    "benchmark_code": BENCHMARK_CODE,
                    "data_source": "local_db",
                },
            )
        if not has_future_benchmark_bar:
            raise SeedPoolEvaluationPreconditionError(
                status_code=409,
                error="missing_benchmark_ohlc",
                message=f"本地数据库缺少 {seed_date.isoformat()} 之后的上证指数下一交易日行情，无法识别评估日并计算 Alpha；请先同步 OHLC。",
                details={
                    "seed_date": seed_date.isoformat(),
                    "fallback_expected_evaluation_date": expected.isoformat(),
                    "benchmark_code": BENCHMARK_CODE,
                    "data_source": "local_db",
                },
            )

        sample_codes = [str(item.get("code") or "").strip() for item in items if item.get("code")]
        has_any_stock_bar = any(
            self.market_data.get_daily_bars_from_db(code, expected, expected)
            for code in sample_codes[: min(len(sample_codes), 20)]
        )
        if not has_any_stock_bar:
            raise SeedPoolEvaluationPreconditionError(
                status_code=409,
                error="missing_stock_ohlc",
                message=f"本地数据库缺少 {expected.isoformat()} 的 Seed 股票行情，无法更新 T+1；请先同步当日 OHLC。",
                details={
                    "seed_date": seed_date.isoformat(),
                    "expected_evaluation_date": expected.isoformat(),
                    "checked_codes": sample_codes[:20],
                    "data_source": "local_db",
                },
            )

        return {"expected_evaluation_date": expected}

    def _resolve_expected_evaluation_date(self, seed_date: date) -> tuple[date, List[DailyBar], bool]:
        """Resolve T+1 as the next observed benchmark trading day.

        Weekday-only calendars misclassify exchange holidays. The local
        benchmark OHLC table is the source of truth for whether a date traded.
        """
        fallback = _next_weekday(seed_date)
        benchmark_bars = self.market_data.get_daily_bars_from_db(
            BENCHMARK_CODE,
            seed_date - timedelta(days=14),
            seed_date + timedelta(days=14),
        )
        future_trade_dates = sorted({bar.trade_date for bar in benchmark_bars if bar.trade_date > seed_date})
        if future_trade_dates:
            return future_trade_dates[0], benchmark_bars, True
        return fallback, benchmark_bars, False

    def chart_data(self, item_id: int) -> Dict[str, Any]:
        item = self.repo.get_item_with_snapshot(item_id)
        detail = self.repo.get_item_detail(item_id)
        if not item or not detail:
            return {}
        seed_date = item["seed_date"]
        bars = self.market_data.get_daily_bars(
            str(item["code"]),
            seed_date - timedelta(days=45),
            seed_date + timedelta(days=14),
            market=str(item.get("market") or "cn"),
        )
        window = _trading_window(bars, seed_date, before=20, after=5)
        return {
            "item": detail,
            "bars": [bar.to_dict() for bar in window],
            "evaluation": detail.get("evaluation") or {},
            "catalyst": {
                "catalyst_tags": detail.get("catalyst_tags") or [],
                "catalyst_tier": detail.get("catalyst_tier") or 0,
                "trigger_signals": detail.get("trigger_signals") or [],
            },
            "desk_outcomes": detail.get("desk_outcomes") or [],
            "price_lines": extract_price_lines(detail.get("desk_outcomes") or []),
        }


class SeedPoolEvaluationPreconditionError(RuntimeError):
    def __init__(self, *, status_code: int, error: str, message: str, details: Dict[str, Any]) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.details = details


def extract_price_lines(desk_outcomes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return optional explicit price lines.

    Four thesis desks do not own entry/stop price generation. Only render
    lines when an upstream payload explicitly provides standardized numeric
    metrics; do not infer trading levels from natural-language desk reasons.
    """
    lines: List[Dict[str, Any]] = []
    seen = set()
    for outcome in desk_outcomes:
        if not isinstance(outcome, dict):
            continue
        desk = str(outcome.get("desk") or "unknown")
        metrics = outcome.get("metrics") if isinstance(outcome.get("metrics"), dict) else {}
        for key, meta in PRICE_LINE_META.items():
            price = _safe_float(metrics.get(key))
            if price is None:
                continue
            dedupe_key = (desk, key, round(price, 4))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            lines.append({
                "desk": desk,
                "key": key,
                "price": round(price, 4),
                "label": meta["label"],
                "color": meta["color"],
            })
    return lines


def _calculate_evaluation_payload(
    *,
    item_id: int,
    seed_date: date,
    stock_bars: List[DailyBar],
    benchmark_bars: List[DailyBar],
    expected_evaluation_date: Optional[date] = None,
) -> Dict[str, Any]:
    seed_bar = next((bar for bar in sorted(stock_bars, key=lambda item: item.trade_date, reverse=True) if bar.trade_date <= seed_date), None)
    eval_bar = (
        next((bar for bar in stock_bars if bar.trade_date == expected_evaluation_date), None)
        if expected_evaluation_date
        else next((bar for bar in stock_bars if bar.trade_date > seed_date), None)
    )
    if seed_bar is None or eval_bar is None:
        return {
            "item_id": item_id,
            "evaluation_date": expected_evaluation_date or _next_weekday(seed_date),
            "benchmark_code": BENCHMARK_CODE,
            "data_status": "missing_price",
            "liquidity_status": "UNKNOWN",
            "error": f"missing seed_date or {expected_evaluation_date.isoformat() if expected_evaluation_date else 'T+1'} stock OHLC",
        }
    bench_seed = next((bar for bar in sorted(benchmark_bars, key=lambda item: item.trade_date, reverse=True) if bar.trade_date <= seed_date), None)
    bench_eval = next((bar for bar in benchmark_bars if bar.trade_date == eval_bar.trade_date), None)
    if bench_seed is None or bench_eval is None:
        return {
            "item_id": item_id,
            "evaluation_date": eval_bar.trade_date,
            "seed_close": seed_bar.close,
            "evaluation_open": eval_bar.open,
            "evaluation_high": eval_bar.high,
            "evaluation_low": eval_bar.low,
            "evaluation_close": eval_bar.close,
            "benchmark_code": BENCHMARK_CODE,
            "data_status": "missing_price",
            "liquidity_status": "UNKNOWN",
            "error": "missing benchmark OHLC",
        }
    next_return = _return_pct(eval_bar.close, seed_bar.close)
    benchmark_return = _return_pct(bench_eval.close, bench_seed.close)
    liquidity = _liquidity_status(eval_bar, next_return)
    return {
        "item_id": item_id,
        "evaluation_date": eval_bar.trade_date,
        "seed_close": seed_bar.close,
        "evaluation_open": eval_bar.open,
        "evaluation_high": eval_bar.high,
        "evaluation_low": eval_bar.low,
        "evaluation_close": eval_bar.close,
        "next_close_return_pct": next_return,
        "benchmark_code": BENCHMARK_CODE,
        "benchmark_return_pct": benchmark_return,
        "alpha_return_pct": round(next_return - benchmark_return, 4),
        "mfe_pct": _return_pct(eval_bar.high, seed_bar.close),
        "mae_pct": _return_pct(eval_bar.low, seed_bar.close),
        "liquidity_status": liquidity,
        "data_status": "ok",
        "error": "",
    }


def _extract_seed_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = payload.get("seed_pool_summary") if isinstance(payload.get("seed_pool_summary"), dict) else {}
    preview = summary.get("preview") if isinstance(summary.get("preview"), list) else []
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    seed_packets = payload.get("seed_fact_packets") if isinstance(payload.get("seed_fact_packets"), list) else []
    preview_by_code = {
        _normalize_code(item.get("code") or item.get("stock_code")): item
        for item in preview
        if isinstance(item, dict)
    }
    candidate_by_code = {_normalize_code(item.get("code") or item.get("stock_code")): item for item in candidates if isinstance(item, dict)}
    if seed_packets:
        items = [
            _seed_fact_packet_to_seed_item(packet, preview_by_code=preview_by_code, candidate_by_code=candidate_by_code)
            for packet in seed_packets
            if isinstance(packet, dict)
        ]
    else:
        items = [item for item in preview if isinstance(item, dict)] or [item for item in candidates if isinstance(item, dict)]
    merged: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        code = _normalize_code(item.get("code") or item.get("stock_code"))
        enriched = dict(candidate_by_code.get(code, {}))
        enriched.update(item)
        enriched.setdefault("seed_order", idx)
        if code:
            enriched["code"] = code
            enriched["name"] = _resolve_seed_stock_name(code, enriched)
        merged.append(enriched)
    return merged


def _seed_fact_packet_to_seed_item(
    packet: Dict[str, Any],
    *,
    preview_by_code: Dict[str, Dict[str, Any]],
    candidate_by_code: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    code = _normalize_code(packet.get("code") or packet.get("stock_code"))
    preview = preview_by_code.get(code, {})
    candidate = candidate_by_code.get(code, {})
    flags = [item for item in packet.get("flags") or [] if isinstance(item, dict)]
    recall_sources = [
        str(item).strip()
        for item in packet.get("recall_sources") or []
        if str(item).strip()
    ]
    fact_sheet = packet.get("fact_sheet") if isinstance(packet.get("fact_sheet"), dict) else {}
    source = (
        preview.get("source")
        or (recall_sources[0] if recall_sources else "")
        or candidate.get("source")
        or candidate.get("candidate_source")
        or "unknown"
    )
    trigger_signals = preview.get("trigger_signals") if isinstance(preview.get("trigger_signals"), list) else []
    if not trigger_signals:
        trigger_signals = [
            {
                "signal_type": flag.get("detector") or flag.get("kind") or "seed_fact_flag",
                "label": flag.get("kind") or "seed_fact",
                "summary": flag.get("summary") or flag.get("reason") or "",
            }
            for flag in flags
        ]
    hint = str(preview.get("hint") or "").strip()
    if not hint:
        hint = "；".join(str(flag.get("summary") or "").strip() for flag in flags if flag.get("summary"))
    source_diagnostics = preview.get("source_diagnostics") if isinstance(preview.get("source_diagnostics"), dict) else {}
    if not source_diagnostics:
        source_diagnostics = {
            "source": source,
            "recall_sources": recall_sources,
            "fact_sheet_freshness": fact_sheet.get("freshness"),
        }
    return {
        "code": code,
        "name": packet.get("name") or preview.get("name") or candidate.get("name") or candidate.get("stock_name") or code,
        "market": packet.get("market") or preview.get("market") or candidate.get("market") or "cn",
        "source": source,
        "source_diagnostics": {key: value for key, value in source_diagnostics.items() if value not in (None, "", [])},
        "trigger_signals": trigger_signals,
        "catalyst_tags": _seed_fact_catalyst_tags(flags=flags, trigger_signals=trigger_signals),
        "entry_reason": hint,
        "freshness": preview.get("freshness") or fact_sheet.get("freshness") or packet.get("freshness") or "",
        "raw_seed_fact_packet": packet,
    }


def _seed_fact_catalyst_tags(*, flags: Sequence[Dict[str, Any]], trigger_signals: Sequence[Dict[str, Any]]) -> List[str]:
    tags: List[str] = []
    for flag in flags:
        for key in ("kind", "detector", "summary"):
            text = str(flag.get(key) or "").strip()
            if text and text not in tags:
                tags.append(text)
    for signal in trigger_signals:
        if not isinstance(signal, dict):
            continue
        for key in ("label", "signal_type", "summary"):
            text = str(signal.get(key) or "").strip()
            if text and text not in tags:
                tags.append(text)
    return tags[:8]


def _resolve_seed_stock_name(code: str, item: Dict[str, Any]) -> str:
    for key in ("name", "stock_name", "display_name"):
        name = item.get(key)
        if is_meaningful_stock_name(str(name or ""), code):
            return str(name).strip()
    index_name = get_index_stock_name(code)
    if is_meaningful_stock_name(index_name, code):
        return str(index_name).strip()
    return code


def _extract_desk_outcomes_by_code(payload: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for packet in payload.get("thesis_desk_packets") or []:
        if not isinstance(packet, dict):
            continue
        desk = str(packet.get("expert") or packet.get("desk") or "unknown")
        status = str(packet.get("status") or "unknown")
        elapsed_ms = packet.get("elapsed_ms")
        for key, stance_default, decision_default in (
            ("candidates", "support", "accepted"),
            ("rejected", "oppose", "rejected"),
        ):
            for row in packet.get(key) or []:
                if not isinstance(row, dict):
                    continue
                code = _normalize_code(row.get("code") or row.get("stock_code"))
                if not code:
                    continue
                _put_desk_outcome(result, code, {
                    "desk": desk,
                    "status": status,
                    "stance": row.get("stance") or stance_default,
                    "decision": row.get("decision") or decision_default,
                    "reason": row.get("reason") or row.get("summary") or row.get("reject_reason") or "",
                    "risks": row.get("risks") or row.get("risk_factors") or [],
                    "evidence": row.get("evidence") or [],
                    "metrics": row.get("metrics") or {},
                    "errors": row.get("errors") or packet.get("errors") or [],
                    "elapsed_ms": elapsed_ms,
                }, priority=30)
        for row in packet.get("per_seed_packets") or []:
            if not isinstance(row, dict):
                continue
            code = _normalize_code(_per_seed_packet_code(row))
            emitted_nested = False
            for key, stance_default, decision_default in (
                ("candidates", "support", "accepted"),
                ("rejected", "oppose", "rejected"),
            ):
                for nested in row.get(key) or []:
                    if not isinstance(nested, dict):
                        continue
                    nested_code = _normalize_code(nested.get("code") or nested.get("stock_code") or code)
                    if not nested_code:
                        continue
                    emitted_nested = True
                    _put_desk_outcome(result, nested_code, {
                        "desk": desk,
                        "status": str(row.get("status") or status or "unknown"),
                        "stance": nested.get("stance") or stance_default,
                        "decision": nested.get("decision") or decision_default,
                        "reason": nested.get("reason") or nested.get("summary") or nested.get("reject_reason") or "",
                        "risks": nested.get("risks") or nested.get("risk_factors") or [],
                        "evidence": nested.get("evidence") or [],
                        "metrics": nested.get("metrics") or {},
                        "errors": nested.get("errors") or row.get("errors") or packet.get("errors") or [],
                        "elapsed_ms": row.get("elapsed_ms") or elapsed_ms,
                    }, priority=25)
            if emitted_nested:
                continue
            if not code:
                continue
            status_text = str(row.get("status") or status or "unknown")
            diagnostics = [item for item in row.get("diagnostics") or [] if isinstance(item, dict)]
            errors = [str(item) for item in row.get("errors") or [] if str(item)]
            if desk in result.get(code, {}):
                continue
            _put_desk_outcome(result, code, {
                "desk": desk,
                "status": status_text,
                "stance": "missing",
                "decision": "timeout" if status_text == "timeout" else "not_evaluated",
                "reason": _per_seed_packet_reason(row, code=code, desk=desk),
                "risks": [],
                "evidence": diagnostics,
                "metrics": {},
                "errors": errors or packet.get("errors") or [],
                "elapsed_ms": row.get("elapsed_ms") or elapsed_ms,
            }, priority=10)
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        code = _normalize_code(cand.get("code") or cand.get("stock_code"))
        stance_items = (cand.get("stance_by_desk") or {}).items() if isinstance(cand.get("stance_by_desk"), dict) else []
        for desk, stance in stance_items:
            _put_desk_outcome(result, code, {
                "desk": str(desk),
                "status": "ok",
                "stance": str(stance),
                "decision": "accepted" if str(stance) in {"support", "watch"} else "rejected",
                "reason": cand.get("reason") or "",
                "risks": cand.get("risks") or [],
                "evidence": cand.get("evidence") or [],
                "metrics": cand.get("metrics") or {},
                "errors": [],
            }, priority=20)
    return {
        code: [_strip_internal_outcome_fields(outcome) for outcome in outcomes.values()]
        for code, outcomes in result.items()
    }


def _put_desk_outcome(
    result: Dict[str, Dict[str, Dict[str, Any]]],
    code: str,
    outcome: Dict[str, Any],
    *,
    priority: int,
) -> None:
    desk = str(outcome.get("desk") or outcome.get("expert") or "unknown")
    if not code or not desk:
        return
    normalized = dict(outcome)
    normalized["desk"] = desk
    normalized["_source_priority"] = priority
    by_desk = result.setdefault(code, {})
    existing = by_desk.get(desk)
    if existing is None or _desk_outcome_score(normalized) > _desk_outcome_score(existing):
        by_desk[desk] = normalized


def _desk_outcome_score(outcome: Dict[str, Any]) -> int:
    stance = str(outcome.get("stance") or "")
    decision = str(outcome.get("decision") or "")
    score = int(outcome.get("_source_priority") or 0)
    if stance and stance not in {"missing", "not_in_scope"}:
        score += 20
    if decision and decision not in {"missing", "not_evaluated"}:
        score += 10
    if str(outcome.get("reason") or "").strip():
        score += 15
    if outcome.get("evidence"):
        score += 5
    if outcome.get("risks"):
        score += 3
    if outcome.get("errors"):
        score += 1
    return score


def _strip_internal_outcome_fields(outcome: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in outcome.items() if not str(key).startswith("_")}


def _per_seed_packet_code(row: Dict[str, Any]) -> str:
    for key in ("code", "stock_code", "symbol"):
        if row.get(key):
            return str(row.get(key) or "")
    for item in row.get("diagnostics") or []:
        if isinstance(item, dict) and item.get("code"):
            return str(item.get("code") or "")
    return ""


def _per_seed_packet_reason(row: Dict[str, Any], *, code: str, desk: str) -> str:
    diagnostics = [item for item in row.get("diagnostics") or [] if isinstance(item, dict)]
    errors = [str(item) for item in row.get("errors") or [] if str(item)]
    for item in diagnostics:
        reason = str(item.get("reason") or item.get("error") or "").strip()
        status = str(item.get("status") or "").strip()
        source = str(item.get("source") or "").strip()
        if reason:
            if status:
                return f"{desk} 未产出结构化结论：{status}，原因 {reason}。"
            return f"{desk} 未产出结构化结论：{reason}。"
        if status in {"timeout", "skipped"}:
            return f"{desk} 未产出结构化结论：{source or status}。"
    if errors:
        return f"{desk} 未产出结构化结论：{errors[0]}。"
    status = str(row.get("status") or "missing").strip()
    return f"{desk} 对 {code} 未产出 candidates/rejected 结构化结论，状态 {status}。"


def infer_seed_pool_snapshot_date(payload: Dict[str, Any]) -> Optional[date]:
    """Infer the trading date represented by a seed-pool payload."""

    for value in (
        payload.get("seed_date"),
        payload.get("trade_date"),
        (payload.get("seed_pool_summary") or {}).get("seed_date") if isinstance(payload.get("seed_pool_summary"), dict) else None,
    ):
        parsed = _parse_date(value)
        if parsed:
            return parsed
    seed_dates: List[date] = []
    for item in _extract_seed_items(payload):
        parsed = _infer_seed_date_from_item(item)
        if parsed:
            seed_dates.append(parsed)
    if not seed_dates:
        return None
    counts = Counter(seed_dates)
    return sorted(counts.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)[0][0]


def _infer_seed_date_from_item(item: Dict[str, Any]) -> Optional[date]:
    for key in ("seed_date", "trade_date", "as_of", "freshness", "date", "target_date"):
        parsed = _parse_date(item.get(key))
        if parsed:
            return parsed
    for signal in item.get("trigger_signals") or []:
        if not isinstance(signal, dict):
            continue
        for key in ("seed_date", "trade_date", "as_of", "freshness", "date", "target_date"):
            parsed = _parse_date(signal.get(key))
            if parsed:
                return parsed
    return None


def effective_seed_pool_date(value: Optional[datetime] = None) -> date:
    """Return the Seed Pool attribution date under Beijing pre-open semantics.

    Candidate pools generated before 09:00 Beijing time belong to the previous
    natural day. Explicit user-provided seed_date values bypass this helper.
    """

    current = value or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    else:
        current = current.astimezone(CN_TZ)
    if current.time() < SEED_POOL_ROLLOVER_TIME:
        return current.date() - timedelta(days=1)
    return current.date()


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _bar_from_stock_daily(row: Any, *, requested_code: str) -> DailyBar:
    return DailyBar(
        code=requested_code,
        trade_date=row.date,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=row.volume,
        amount=row.amount,
        source=row.data_source or "stock_daily",
    )


def _row_has_ohlc(row: Any) -> bool:
    return all(_safe_float(getattr(row, key, None)) is not None for key in ("open", "high", "low", "close"))


def _merge_daily_bars(bars: Iterable[DailyBar]) -> List[DailyBar]:
    by_date: Dict[date, DailyBar] = {}
    for bar in bars:
        by_date[bar.trade_date] = bar
    return [by_date[key] for key in sorted(by_date)]


def _trading_window(bars: List[DailyBar], seed_date: date, *, before: int, after: int) -> List[DailyBar]:
    if not bars:
        return []
    ordered = sorted(bars, key=lambda bar: bar.trade_date)
    seed_idx = next(
        (i for i in range(len(ordered) - 1, -1, -1) if ordered[i].trade_date <= seed_date),
        0,
    )
    next_trade_idx = next((i for i, bar in enumerate(ordered) if bar.trade_date > seed_date), seed_idx)
    start_idx = max(0, seed_idx - before)
    end_idx = min(len(ordered), max(seed_idx + after, next_trade_idx) + 1)
    return ordered[start_idx:end_idx]


def _return_pct(current: float, base: float) -> float:
    if base == 0:
        return 0.0
    return round((current / base - 1.0) * 100.0, 4)


def _liquidity_status(eval_bar: DailyBar, next_return_pct: float) -> str:
    if _price_equal(eval_bar.open, eval_bar.high) and _price_equal(eval_bar.high, eval_bar.close) and next_return_pct >= 9.8:
        return "LIMIT_UP_UNABLE_BUY"
    if _price_equal(eval_bar.open, eval_bar.low) and _price_equal(eval_bar.low, eval_bar.close) and next_return_pct <= -9.8:
        return "LIMIT_DOWN_RISK"
    return "NORMAL"


def _price_equal(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0, abs_tol=0.0001)


def _next_weekday(value: date) -> date:
    cur = value + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _code_variants(symbol: str) -> List[str]:
    text = str(symbol or "").strip()
    variants = [text]
    upper = text.upper()
    if upper == BENCHMARK_CODE:
        return [BENCHMARK_CODE, BENCHMARK_BAOSTOCK_CODE, "sh000001"]
    if "." in text:
        variants.append(text.split(".")[0])
    return list(dict.fromkeys(v for v in variants if v))


def _fetch_variants(symbol: str) -> List[str]:
    text = str(symbol or "").strip()
    if text.upper() == BENCHMARK_CODE:
        return [BENCHMARK_CODE, BENCHMARK_BAOSTOCK_CODE, "sh000001"]
    return [text]


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _load_from_sequoia_db(symbol: str, start_date: date, end_date: date) -> List[DailyBar]:
    db_path = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "Sequoia-X/data/sequoia_v2.db"
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    variants = _sequoia_symbol_variants(symbol)
    try:
        placeholders = ",".join("?" * len(variants))
        params = [*variants, start_date.isoformat(), end_date.isoformat()]
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT symbol, date, open, high, low, close, volume, turnover
                    FROM stock_daily
                    WHERE symbol IN ({placeholders})
                      AND date >= ?
                      AND date <= ?
                    ORDER BY date""",
                params,
            ).fetchall()
    except Exception as exc:
        logger.debug("seed quality Sequoia DB read failed for %s: %s", symbol, exc)
        return []
    bars: List[DailyBar] = []
    for row in rows:
        parsed_date = _parse_date(row["date"])
        if parsed_date is None:
            continue
        try:
            bars.append(
                DailyBar(
                    code=symbol,
                    trade_date=parsed_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=_safe_float(row["volume"]),
                    amount=_safe_float(row["turnover"]),
                    source=f"sequoia_stock_daily:{row['symbol']}",
                )
            )
        except (TypeError, ValueError):
            continue
    return bars


def _sequoia_symbol_variants(symbol: str) -> List[str]:
    text = str(symbol or "").strip()
    upper = text.upper()
    if upper == BENCHMARK_CODE:
        return [BENCHMARK_CODE, BENCHMARK_BAOSTOCK_CODE, "sh000001"]
    variants = [text]
    if "." in text:
        variants.append(text.split(".", 1)[0])
    return list(dict.fromkeys(v for v in variants if v))


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None
