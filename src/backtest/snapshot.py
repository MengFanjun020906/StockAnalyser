# -*- coding: utf-8 -*-
"""Immutable experiment snapshots, coverage precheck and snapshot loading."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, desc, select

from src.agent.pricing.schemas import MarketRuleSnapshot
from src.storage import (
    BacktestCorporateActionRecord,
    DatabaseManager,
    MarketRuleSnapshotRecord,
    StockDaily,
    StockMinuteBar,
)

from .calendar import CnTradingCalendar, SHANGHAI
from .data_view import InMemoryDataView
from .entities import Bar, CorporateAction, DailyBar
from .rules import MarketRule, derive_rule, normalize_cn_symbol, rule_from_snapshot
from .schemas import (
    CoverageRow,
    DataQuality,
    ExperimentConfig,
    GapRecord,
    Granularity,
    MissingDataPolicy,
    PlanInputRecord,
    PrecheckResult,
    ReplayPlan,
    SessionStatus,
    SnapshotManifest,
    canonical_json,
)

logger = logging.getLogger(__name__)

MINUTE_BARS_PER_SESSION = 48
DAILY_BARS_PER_SESSION = 1


class SnapshotError(RuntimeError):
    pass


@dataclass
class SnapshotBuildResult:
    manifest: SnapshotManifest
    data: InMemoryDataView
    minute_bars: list[Bar]
    daily_bars: list[DailyBar]
    rules: list[MarketRule]
    actions: list[CorporateAction]
    coverage: list[CoverageRow]
    gaps: list[GapRecord]
    quality_flags: list[str]
    downgraded_sessions: set[tuple[str, date]]
    benchmark: dict[date, Decimal]
    plans: list[ReplayPlan]
    sessions: list[date]

    def to_precheck(
        self,
        *,
        inputs: list[PlanInputRecord],
        config_hash: str,
        generated_at: datetime,
        blocking_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
        diagnostics: dict[str, Any] | None = None,
        require_included_plans: bool = True,
    ) -> PrecheckResult:
        included = [item for item in inputs if item.included]
        excluded = [item for item in inputs if not item.included]
        blockers = list(blocking_reasons or [])
        warns = list(warnings or [])
        if require_included_plans and not included:
            blockers.append("no_included_plans")
        if not self.sessions:
            blockers.append("no_trading_sessions")
        if not self.minute_bars and not self.daily_bars:
            blockers.append("no_market_data")
        incomplete = [
            row for row in self.coverage
            if row.sessions_missing > 0 or row.sessions_partial > 0
        ]
        if incomplete:
            reason = (
                "incomplete_5m_coverage"
                if self.manifest.granularity is Granularity.MINUTE_5
                else "incomplete_daily_coverage"
            )
            warns.append(reason)
            if not blockers:
                blockers.append(reason)
        if self.manifest.benchmark_status == "unavailable":
            warns.append("benchmark_unavailable")
        if "market_rules_unverified" in self.quality_flags:
            warns.append("market_rules_unverified")
        if "corporate_action_coverage_unavailable" in self.quality_flags:
            warns.append("corporate_action_coverage_unavailable")
        status = "blocked" if blockers else ("warning" if warns else "ok")
        return PrecheckResult(
            config_hash=config_hash,
            generated_at=generated_at,
            status=status,
            included_plans=len(included),
            excluded_plans=len(excluded),
            symbols=self.manifest.symbols,
            sessions=len(self.sessions),
            expected_bars_per_session=MINUTE_BARS_PER_SESSION
            if self.manifest.granularity is Granularity.MINUTE_5
            else DAILY_BARS_PER_SESSION,
            coverage=self.coverage,
            gaps=self.gaps,
            inputs=inputs,
            blocking_reasons=sorted(set(blockers)),
            warnings=sorted(set(warns)),
            diagnostics=diagnostics or {},
            quality=self.manifest.quality,
        )


class SnapshotBuilder:
    """Build a frozen, content-addressed experiment snapshot.

    ``db_manager=None`` plus injected bars/rules is the preferred test path; it
    never touches the production database or network.
    """

    def __init__(
        self,
        *,
        run_id: str,
        config: ExperimentConfig,
        plans: Sequence[ReplayPlan],
        calendar: CnTradingCalendar | None = None,
        db_manager: DatabaseManager | None = None,
        minute_bars: Iterable[Bar] | None = None,
        daily_bars: Iterable[DailyBar] | None = None,
        rules: Iterable[MarketRule] | None = None,
        actions: Iterable[CorporateAction] | None = None,
        benchmark: dict[date, Decimal] | None = None,
        benchmark_symbol: str | None = None,
        symbols_override: Sequence[str] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.plans = list(plans)
        self.calendar = calendar or CnTradingCalendar()
        self.db = db_manager
        self._minute_input = list(minute_bars) if minute_bars is not None else None
        self._daily_input = list(daily_bars) if daily_bars is not None else None
        self._rules_input = list(rules) if rules is not None else None
        self._actions_input = list(actions) if actions is not None else None
        self._benchmark_input = dict(benchmark) if benchmark is not None else None
        self._benchmark_symbol = benchmark_symbol
        self._symbols_override = [normalize_cn_symbol(item) for item in (symbols_override or []) if normalize_cn_symbol(item)]
        self.created_at = created_at or datetime.now(SHANGHAI).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def build(self) -> SnapshotBuildResult:
        symbols = sorted(
            {normalize_cn_symbol(plan.symbol) for plan in self.plans if plan.symbol}
            | set(self._symbols_override)
        )
        sessions = self.calendar.sessions(self.config.start_date, self.config.end_date)
        if not sessions:
            raise SnapshotError("experiment range contains no trading sessions")
        if not symbols:
            raise SnapshotError("no symbols selected")
        quality_flags: set[str] = set()
        if self.calendar.fallback_used:
            quality_flags.add("calendar_weekday_fallback")
        daily_start = self._warmup_start(sessions[0])
        daily_bars = (
            self._daily_input
            if self._daily_input is not None
            else self._load_daily_bars(symbols, daily_start, self.config.end_date)
        )
        minute_bars = (
            self._minute_input
            if self._minute_input is not None
            else self._load_minute_bars(symbols, self.config.start_date, self.config.end_date)
        )
        daily_bars, dq_flags = _normalize_daily_bars(daily_bars)
        minute_bars, mq_flags = _normalize_minute_bars(minute_bars)
        quality_flags.update(dq_flags)
        quality_flags.update(mq_flags)
        daily_map = {(bar.symbol, bar.session): bar for bar in daily_bars}
        minute_by_symbol_session: dict[tuple[str, date], list[Bar]] = {}
        for bar in minute_bars:
            minute_by_symbol_session.setdefault((bar.symbol, bar.session), []).append(bar)
        for key in minute_by_symbol_session:
            minute_by_symbol_session[key].sort(key=lambda item: item.end_at)

        # Synthesize daily bars from complete-enough minute data for valuation and
        # previous close.  Confirmation still requires a real daily bar.
        synthesized = self._synthesize_daily(symbols, sessions, daily_map, minute_by_symbol_session)
        daily_bars.extend(synthesized)
        daily_map = {(bar.symbol, bar.session): bar for bar in daily_bars}
        if synthesized:
            quality_flags.add("daily_bars_synthesized")

        downgraded: set[tuple[str, date]] = set()
        if self.config.granularity is Granularity.MINUTE_5:
            minute_bars, minute_by_symbol_session, downgraded = self._apply_downgrade(
                symbols, sessions, daily_map, minute_by_symbol_session
            )
            if downgraded:
                quality_flags.add("mixed_granularity")
            if self.config.price_basis.value != "minute_unadjusted":
                pass
        else:
            quality_flags.add("daily_price_basis_unverified")
            if self.config.price_basis.value == "daily_declared_unadjusted":
                quality_flags.discard("daily_price_basis_unverified")

        rules = (
            self._rules_input
            if self._rules_input is not None
            else self._load_rules(symbols, sessions, daily_map)
        )
        rules = self._fill_missing_rules(symbols, sessions, rules, daily_map)
        if any(not rule.verified for rule in rules):
            quality_flags.add("market_rules_unverified")
        if any(rule.board == "bse" for rule in rules):
            quality_flags.add("unsupported_board")

        actions = (
            self._actions_input
            if self._actions_input is not None
            else self._load_corporate_actions(symbols, self.config.start_date, self.config.end_date)
        )
        actions = [item for item in actions if item.symbol in symbols]
        if not actions:
            if self.config.corporate_action_policy.value == "assume_none":
                quality_flags.add("corporate_action_assumed_none")
            else:
                quality_flags.add("corporate_action_coverage_unavailable")
        elif any(not item.verified for item in actions):
            quality_flags.add("corporate_action_unverified")

        benchmark, benchmark_status, benchmark_flags = self._resolve_benchmark(sessions)
        quality_flags.update(benchmark_flags)

        coverage, gaps = _build_coverage(
            symbols=symbols,
            sessions=sessions,
            daily_map=daily_map,
            minute_map=minute_by_symbol_session,
            rules=rules,
            downgraded=downgraded,
            granularity=self.config.granularity,
        )
        if any(row.sessions_missing or row.sessions_partial for row in coverage):
            quality_flags.add("incomplete_market_data")
        quality = _overall_quality(coverage, quality_flags)
        manifest_base = {
            "run_id": self.run_id,
            "granularity": self.config.granularity.value,
            "start_date": self.config.start_date,
            "end_date": self.config.end_date,
            "sessions": sessions,
            "symbols": symbols,
            "bar_count": len(minute_bars),
            "daily_bar_count": len(daily_bars),
            "minute_bar_count": len(minute_bars),
            "providers": sorted({bar.source for bar in minute_bars if bar.source} | {bar.source for bar in daily_bars if bar.source}),
            "price_basis": self.config.price_basis.value,
            "coverage": coverage,
            "gaps": gaps,
            "rules_version": "cn-market-rules.v1",
            "calendar_version": self.calendar.version,
            "corporate_action_policy": self.config.corporate_action_policy.value,
            "corporate_action_version": self.config.corporate_action_version,
            "corporate_action_count": len(actions),
            "benchmark_status": benchmark_status,
            "benchmark_symbol": self._benchmark_symbol,
            "quality": quality,
            "quality_flags": sorted(quality_flags),
        }
        content_hash = _content_hash(
            {key: value for key, value in manifest_base.items() if key != "run_id"},
            minute_bars=minute_bars,
            daily_bars=daily_bars,
            rules=rules,
            actions=actions,
            benchmark=benchmark,
            plans=self.plans,
        )
        manifest = SnapshotManifest(
            run_id=self.run_id,
            created_at=self.created_at,
            granularity=self.config.granularity,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            sessions=sessions,
            symbols=symbols,
            bar_count=len(minute_bars),
            daily_bar_count=len(daily_bars),
            minute_bar_count=len(minute_bars),
            first_bar_at=min((bar.end_at for bar in minute_bars), default=None),
            last_bar_at=max((bar.end_at for bar in minute_bars), default=None),
            providers=manifest_base["providers"],
            price_basis=self.config.price_basis,
            content_hash=content_hash,
            coverage=coverage,
            gaps=gaps,
            calendar_version=self.calendar.version,
            corporate_action_policy=self.config.corporate_action_policy,
            corporate_action_version=self.config.corporate_action_version,
            corporate_action_count=len(actions),
            benchmark_status=benchmark_status,
            benchmark_symbol=self._benchmark_symbol,
            quality=quality,
            quality_flags=sorted(quality_flags),
        )
        data = InMemoryDataView.from_records(
            granularity=self.config.granularity.value,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            sessions=sessions,
            bars=minute_bars,
            daily=daily_bars,
            rules=rules,
            actions=actions,
            benchmark=benchmark,
            benchmark_symbol=self._benchmark_symbol,
            coverage=coverage,
            quality_flags=sorted(quality_flags),
            downgraded_sessions=downgraded,
            manifest=manifest,
        )
        return SnapshotBuildResult(
            manifest=manifest,
            data=data,
            minute_bars=minute_bars,
            daily_bars=daily_bars,
            rules=rules,
            actions=actions,
            coverage=coverage,
            gaps=gaps,
            quality_flags=sorted(quality_flags),
            downgraded_sessions=downgraded,
            benchmark=benchmark,
            plans=self.plans,
            sessions=sessions,
        )

    # ------------------------------------------------------------------
    # DB loaders
    # ------------------------------------------------------------------
    def _warmup_start(self, start: date) -> date:
        if self.config.daily_warmup_sessions <= 0:
            return start
        calendar = self.calendar
        sessions = calendar.sessions(start - timedelta(days=self.config.daily_warmup_sessions * 3 + 30), start)
        if len(sessions) <= self.config.daily_warmup_sessions:
            return sessions[0] if sessions else start
        return sessions[-self.config.daily_warmup_sessions - 1]

    def _load_daily_bars(self, symbols: list[str], start: date, end: date) -> list[DailyBar]:
        if self.db is None:
            return []
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code.in_(symbols),
                        StockDaily.date >= start,
                        StockDaily.date <= end,
                    )
                )
                .order_by(StockDaily.code, StockDaily.date)
            ).scalars().all()
        result: list[DailyBar] = []
        for row in rows:
            bar = _daily_from_row(row)
            if bar is not None:
                result.append(bar)
        return result

    def _load_minute_bars(self, symbols: list[str], start: date, end: date) -> list[Bar]:
        if self.db is None:
            return []
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockMinuteBar)
                .where(
                    and_(
                        StockMinuteBar.code.in_(symbols),
                        StockMinuteBar.bar_date >= start,
                        StockMinuteBar.bar_date <= end,
                        StockMinuteBar.frequency == "5",
                        StockMinuteBar.adjustflag == "3",
                    )
                )
                .order_by(StockMinuteBar.code, StockMinuteBar.bar_datetime)
            ).scalars().all()
        result: list[Bar] = []
        for row in rows:
            bar = _minute_from_row(row)
            if bar is not None:
                result.append(bar)
        return result

    def _load_rules(
        self,
        symbols: list[str],
        sessions: list[date],
        daily_map: dict[tuple[str, date], DailyBar],
    ) -> list[MarketRule]:
        if self.db is None:
            return []
        with self.db.get_session() as session:
            rows = session.execute(
                select(MarketRuleSnapshotRecord)
                .where(
                    and_(
                        MarketRuleSnapshotRecord.symbol.in_(symbols),
                        MarketRuleSnapshotRecord.trade_date >= sessions[0],
                        MarketRuleSnapshotRecord.trade_date <= sessions[-1],
                    )
                )
                .order_by(
                    MarketRuleSnapshotRecord.symbol,
                    MarketRuleSnapshotRecord.trade_date,
                    desc(MarketRuleSnapshotRecord.id),
                )
            ).scalars().all()
        result: list[MarketRule] = []
        seen: set[tuple[str, date]] = set()
        for row in rows:
            key = (normalize_cn_symbol(row.symbol or ""), row.trade_date)
            if key in seen:
                continue
            try:
                snapshot = json.loads(row.snapshot_json)
                rule = rule_from_snapshot(MarketRuleSnapshot.model_validate(snapshot), row.trade_date)
            except Exception:
                continue
            seen.add(key)
            result.append(rule)
        return result

    def _fill_missing_rules(
        self,
        symbols: list[str],
        sessions: list[date],
        rules: list[MarketRule],
        daily_map: dict[tuple[str, date], DailyBar],
    ) -> list[MarketRule]:
        existing = {(rule.symbol, rule.session): rule for rule in rules}
        filled: list[MarketRule] = []
        for symbol in symbols:
            for index, session in enumerate(sessions):
                if (symbol, session) in existing:
                    filled.append(existing[(symbol, session)])
                    continue
                previous_close = None
                if index > 0:
                    previous = daily_map.get((symbol, sessions[index - 1]))
                    previous_close = previous.close if previous is not None else None
                filled.append(
                    derive_rule(
                        symbol,
                        session,
                        previous_close=previous_close,
                    )
                )
        return filled

    def _load_corporate_actions(self, symbols: list[str], start: date, end: date) -> list[CorporateAction]:
        if self.db is None:
            return []
        conditions = [
            BacktestCorporateActionRecord.symbol.in_(symbols),
            BacktestCorporateActionRecord.ex_date >= start,
            BacktestCorporateActionRecord.ex_date <= end,
        ]
        if self.config.corporate_action_version:
            conditions.append(
                BacktestCorporateActionRecord.version == self.config.corporate_action_version
            )
        with self.db.get_session() as session:
            rows = session.execute(
                select(BacktestCorporateActionRecord)
                .where(and_(*conditions))
                .order_by(BacktestCorporateActionRecord.symbol, BacktestCorporateActionRecord.ex_date)
            ).scalars().all()
        result = []
        for row in rows:
            result.append(
                CorporateAction(
                    symbol=normalize_cn_symbol(row.symbol),
                    ex_date=row.ex_date,
                    action_type=row.action_type,
                    cash_per_share=Decimal(str(row.cash_per_share or 0)),
                    stock_ratio=Decimal(str(row.stock_ratio or 0)),
                    split_ratio=Decimal(str(row.split_ratio or 1)),
                    version=row.version,
                    source=row.source or "",
                    verified=bool(row.verified),
                    payload=_json_or_empty(row.payload_json),
                )
            )
        return result

    def _synthesize_daily(
        self,
        symbols: list[str],
        sessions: list[date],
        daily_map: dict[tuple[str, date], DailyBar],
        minute_map: dict[tuple[str, date], list[Bar]],
    ) -> list[DailyBar]:
        if self.config.granularity is not Granularity.MINUTE_5:
            return []
        result: list[DailyBar] = []
        for symbol in symbols:
            for session in sessions:
                if (symbol, session) in daily_map:
                    continue
                bars = minute_map.get((symbol, session), [])
                if not bars:
                    continue
                result.append(
                    DailyBar(
                        symbol=symbol,
                        session=session,
                        open=bars[0].open,
                        high=max(bar.high for bar in bars),
                        low=min(bar.low for bar in bars),
                        close=bars[-1].close,
                        volume=sum((bar.volume for bar in bars), Decimal("0")),
                        amount=sum((bar.amount for bar in bars), Decimal("0")),
                        source="minute_aggregate",
                        volume_unit="shares",
                    )
                )
        return result

    def _apply_downgrade(
        self,
        symbols: list[str],
        sessions: list[date],
        daily_map: dict[tuple[str, date], DailyBar],
        minute_map: dict[tuple[str, date], list[Bar]],
    ) -> tuple[list[Bar], dict[tuple[str, date], list[Bar]], set[tuple[str, date]]]:
        if self.config.missing_data_policy is not MissingDataPolicy.DAILY_DOWNGRADE:
            bars = [bar for session_bars in minute_map.values() for bar in session_bars]
            return bars, minute_map, set()
        downgraded: set[tuple[str, date]] = set()
        new_map: dict[tuple[str, date], list[Bar]] = {}
        for symbol in symbols:
            for session in sessions:
                key = (symbol, session)
                bars = minute_map.get(key, [])
                if len(bars) >= MINUTE_BARS_PER_SESSION:
                    new_map[key] = bars
                    continue
                daily = daily_map.get(key)
                if daily is None:
                    if bars:
                        new_map[key] = bars
                    continue
                downgraded.add(key)
                new_map[key] = [
                    Bar(
                        symbol=symbol,
                        end_at=datetime.combine(session, datetime.min.time()).replace(hour=15),
                        session=session,
                        open=daily.open,
                        high=daily.high,
                        low=daily.low,
                        close=daily.close,
                        volume=daily.volume,
                        amount=daily.amount,
                        frequency="1d",
                        source=daily.source,
                        volume_unit=daily.volume_unit,
                    )
                ]
        bars = [bar for session_bars in new_map.values() for bar in session_bars]
        return bars, new_map, downgraded

    def _resolve_benchmark(
        self, sessions: list[date]
    ) -> tuple[dict[date, Decimal], str, list[str]]:
        if self.config.benchmark is None:
            return {}, "not_requested", []
        if self.config.benchmark.price_basis == "total_return":
            return {}, "unavailable", ["benchmark_total_return_unavailable"]
        symbol = normalize_cn_symbol(self.config.benchmark.symbol)
        self._benchmark_symbol = symbol
        if self._benchmark_input is not None:
            return self._benchmark_input, "available" if self._benchmark_input else "unavailable", []
        if self.db is None:
            return {}, "unavailable", ["benchmark_unavailable"]
        candidates = _benchmark_code_candidates(symbol)
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code.in_(candidates),
                        StockDaily.date >= sessions[0],
                        StockDaily.date <= sessions[-1],
                    )
                )
                .order_by(StockDaily.date)
            ).scalars().all()
        if not rows:
            return {}, "unavailable", ["benchmark_unavailable"]
        values: dict[date, Decimal] = {}
        for row in rows:
            try:
                close = Decimal(str(row.close))
            except Exception:
                continue
            if close > 0:
                values[row.date] = close
        return values, ("available" if values else "unavailable"), ([] if values else ["benchmark_unavailable"])


# ----------------------------------------------------------------------
# Snapshot writing / loading
# ----------------------------------------------------------------------
def write_snapshot(result: SnapshotBuildResult, output_dir: Path) -> SnapshotManifest:
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "snapshot.sqlite"
    tmp_path = output_dir / "snapshot.sqlite.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    try:
        _create_snapshot_schema(conn)
        conn.executemany(
            "INSERT INTO sessions(session) VALUES (?)",
            [(item.isoformat(),) for item in result.sessions],
        )
        conn.executemany(
            "INSERT INTO symbols(symbol) VALUES (?)",
            [(item,) for item in result.manifest.symbols],
        )
        conn.executemany(
            "INSERT INTO minute_bars(symbol,end_at,session,open,high,low,close,volume,amount,frequency,source,volume_unit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    bar.symbol,
                    bar.end_at.isoformat(sep=" "),
                    bar.session.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    str(bar.amount),
                    bar.frequency,
                    bar.source,
                    bar.volume_unit,
                )
                for bar in result.minute_bars
            ],
        )
        conn.executemany(
            "INSERT INTO daily_bars(symbol,session,open,high,low,close,volume,amount,source,volume_unit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    bar.symbol,
                    bar.session.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    str(bar.amount),
                    bar.source,
                    bar.volume_unit,
                )
                for bar in result.daily_bars
            ],
        )
        conn.executemany(
            "INSERT INTO rules(symbol,session,venue,board,tick_size,buy_lot_size,price_limit_ratio,previous_close,"
            "limit_up,limit_down,t_plus_one,suspended,st_status,st_status_verified,listing_status,"
            "listing_status_verified,instrument_type,instrument_type_verified,rule_source,rule_version,flags_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    rule.symbol,
                    rule.session.isoformat(),
                    rule.venue,
                    rule.board,
                    str(rule.tick_size),
                    rule.buy_lot_size,
                    str(rule.price_limit_ratio) if rule.price_limit_ratio is not None else None,
                    str(rule.previous_close) if rule.previous_close is not None else None,
                    str(rule.limit_up) if rule.limit_up is not None else None,
                    str(rule.limit_down) if rule.limit_down is not None else None,
                    int(rule.t_plus_one),
                    int(rule.suspended),
                    int(rule.st_status),
                    int(rule.st_status_verified),
                    rule.listing_status,
                    int(rule.listing_status_verified),
                    rule.instrument_type,
                    int(rule.instrument_type_verified),
                    rule.rule_source,
                    rule.rule_version,
                    json.dumps(list(rule.flags), ensure_ascii=True),
                )
                for rule in result.rules
            ],
        )
        conn.executemany(
            "INSERT INTO corporate_actions(symbol,ex_date,action_type,cash_per_share,stock_ratio,split_ratio,"
            "version,source,verified,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    action.symbol,
                    action.ex_date.isoformat(),
                    action.action_type,
                    str(action.cash_per_share),
                    str(action.stock_ratio),
                    str(action.split_ratio),
                    action.version,
                    action.source,
                    int(action.verified),
                    json.dumps(action.payload, ensure_ascii=True, sort_keys=True),
                )
                for action in result.actions
            ],
        )
        conn.executemany(
            "INSERT INTO benchmark(session,close,symbol) VALUES (?,?,?)",
            [
                (session.isoformat(), str(close), result.manifest.benchmark_symbol or "")
                for session, close in sorted(result.benchmark.items())
            ],
        )
        conn.executemany(
            "INSERT INTO coverage(symbol,session,status,actual_bars,expected_bars,first_bar_at,last_bar_at,downgraded) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    gap.symbol,
                    gap.session.isoformat(),
                    gap.status.value,
                    gap.actual_bars,
                    gap.expected_bars,
                    gap.first_bar_at.isoformat(sep=" ") if gap.first_bar_at else None,
                    gap.last_bar_at.isoformat(sep=" ") if gap.last_bar_at else None,
                    1 if (gap.symbol, gap.session) in result.downgraded_sessions else 0,
                )
                for gap in _coverage_rows_from_gaps(result.gaps, result.manifest.symbols, result.sessions)
            ],
        )
        conn.executemany(
            "INSERT INTO plans(plan_key,plan_json) VALUES (?,?)",
            [(plan.plan_key, plan.model_dump_json()) for plan in result.plans],
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_path, db_path)
    file_sha = _sha256_file(db_path)
    manifest = result.manifest.model_copy(update={"file_sha256": file_sha})
    (output_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    try:
        db_path.chmod(0o444)
        (output_dir / "manifest.json").chmod(0o444)
    except OSError:
        pass
    return manifest


def load_snapshot(output_dir: Path) -> tuple[InMemoryDataView, SnapshotManifest, list[ReplayPlan]]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        raise SnapshotError("snapshot manifest not found")
    manifest = SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    db_path = output_dir / "snapshot.sqlite"
    if not db_path.exists():
        raise SnapshotError("snapshot database not found")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions = [date.fromisoformat(row["session"]) for row in conn.execute("SELECT session FROM sessions ORDER BY session")]
        minute_bars = [
            Bar(
                symbol=row["symbol"],
                end_at=datetime.fromisoformat(row["end_at"]),
                session=date.fromisoformat(row["session"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                amount=Decimal(row["amount"]),
                frequency=row["frequency"],
                source=row["source"] or "",
                volume_unit=row["volume_unit"] or "shares",
            )
            for row in conn.execute("SELECT * FROM minute_bars ORDER BY symbol,end_at")
        ]
        daily_bars = [
            DailyBar(
                symbol=row["symbol"],
                session=date.fromisoformat(row["session"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                amount=Decimal(row["amount"]),
                source=row["source"] or "",
                volume_unit=row["volume_unit"] or "shares",
            )
            for row in conn.execute("SELECT * FROM daily_bars ORDER BY symbol,session")
        ]
        rules = [
            MarketRule(
                symbol=row["symbol"],
                session=date.fromisoformat(row["session"]),
                venue=row["venue"],
                board=row["board"],
                tick_size=Decimal(row["tick_size"]),
                buy_lot_size=int(row["buy_lot_size"]),
                price_limit_ratio=Decimal(row["price_limit_ratio"]) if row["price_limit_ratio"] else None,
                previous_close=Decimal(row["previous_close"]) if row["previous_close"] else None,
                limit_up=Decimal(row["limit_up"]) if row["limit_up"] else None,
                limit_down=Decimal(row["limit_down"]) if row["limit_down"] else None,
                t_plus_one=bool(row["t_plus_one"]),
                suspended=bool(row["suspended"]),
                st_status=bool(row["st_status"]),
                st_status_verified=bool(row["st_status_verified"]),
                listing_status=row["listing_status"],
                listing_status_verified=bool(row["listing_status_verified"]),
                instrument_type=row["instrument_type"],
                instrument_type_verified=bool(row["instrument_type_verified"]),
                rule_source=row["rule_source"],
                rule_version=row["rule_version"],
                flags=tuple(json.loads(row["flags_json"] or "[]")),
            )
            for row in conn.execute("SELECT * FROM rules ORDER BY symbol,session")
        ]
        actions = [
            CorporateAction(
                symbol=row["symbol"],
                ex_date=date.fromisoformat(row["ex_date"]),
                action_type=row["action_type"],
                cash_per_share=Decimal(row["cash_per_share"]),
                stock_ratio=Decimal(row["stock_ratio"]),
                split_ratio=Decimal(row["split_ratio"]),
                version=row["version"],
                source=row["source"] or "",
                verified=bool(row["verified"]),
                payload=json.loads(row["payload_json"] or "{}"),
            )
            for row in conn.execute("SELECT * FROM corporate_actions ORDER BY symbol,ex_date")
        ]
        benchmark = {
            date.fromisoformat(row["session"]): Decimal(row["close"])
            for row in conn.execute("SELECT session,close FROM benchmark")
        }
        plans = [
            ReplayPlan.model_validate_json(row["plan_json"])
            for row in conn.execute("SELECT plan_json FROM plans ORDER BY plan_key")
        ]
        downgraded = {
            (row["symbol"], date.fromisoformat(row["session"]))
            for row in conn.execute("SELECT symbol,session FROM coverage WHERE downgraded = 1")
        }
    finally:
        conn.close()
    coverage = manifest.coverage
    data = InMemoryDataView.from_records(
        granularity=manifest.granularity.value,
        start_date=manifest.start_date,
        end_date=manifest.end_date,
        sessions=sessions,
        bars=minute_bars,
        daily=daily_bars,
        rules=rules,
        actions=actions,
        benchmark=benchmark,
        benchmark_symbol=manifest.benchmark_symbol,
        coverage=coverage,
        quality_flags=manifest.quality_flags,
        downgraded_sessions=downgraded,
        manifest=manifest,
    )
    return data, manifest, plans


# ----------------------------------------------------------------------
# Coverage helpers
# ----------------------------------------------------------------------
def _build_coverage(
    *,
    symbols: list[str],
    sessions: list[date],
    daily_map: dict[tuple[str, date], DailyBar],
    minute_map: dict[tuple[str, date], list[Bar]],
    rules: list[MarketRule],
    downgraded: set[tuple[str, date]],
    granularity: Granularity,
) -> tuple[list[CoverageRow], list[GapRecord]]:
    rule_map = {(rule.symbol, rule.session): rule for rule in rules}
    first_dates: dict[str, date] = {}
    for (symbol, session), bars in minute_map.items():
        if bars and (symbol not in first_dates or session < first_dates[symbol]):
            first_dates[symbol] = session
    for (symbol, session) in daily_map:
        if symbol not in first_dates or session < first_dates[symbol]:
            first_dates[symbol] = session
    expected = MINUTE_BARS_PER_SESSION if granularity is Granularity.MINUTE_5 else DAILY_BARS_PER_SESSION
    gaps: list[GapRecord] = []
    rows: list[CoverageRow] = []
    for symbol in symbols:
        row = CoverageRow(
            symbol=symbol,
            sessions_expected=len(sessions),
            expected_bars_per_session=expected,
            first_session=None,
            last_session=None,
            flags=[],
        )
        for session in sessions:
            bars = minute_map.get((symbol, session), [])
            if granularity is Granularity.DAILY:
                daily = daily_map.get((symbol, session))
                actual = 1 if daily is not None else 0
                first_bar_at = None
                last_bar_at = None
            else:
                actual = len(bars)
                first_bar_at = bars[0].end_at if bars else None
                last_bar_at = bars[-1].end_at if bars else None
            rule = rule_map.get((symbol, session))
            first_seen = first_dates.get(symbol)
            downgraded_session = (symbol, session) in downgraded
            if downgraded_session:
                actual = expected
            if actual >= expected:
                status = SessionStatus.FULL
                row.sessions_full += 1
            elif actual > 0:
                status = SessionStatus.PARTIAL
                row.sessions_partial += 1
                row.missing_bars += expected - actual
            elif rule is not None and rule.suspended:
                status = SessionStatus.SUSPENDED
                row.sessions_suspended += 1
            elif first_seen is not None and first_seen > session:
                status = SessionStatus.PRE_LISTING
                row.sessions_pre_listing += 1
            else:
                status = SessionStatus.MISSING
                row.sessions_missing += 1
                row.missing_bars += expected
            if status is SessionStatus.FULL and downgraded_session:
                row.flags.append("downgraded")
            gaps.append(
                GapRecord(
                    symbol=symbol,
                    session=session,
                    status=status,
                    actual_bars=actual,
                    expected_bars=expected,
                    first_bar_at=first_bar_at,
                    last_bar_at=last_bar_at,
                )
            )
            if actual > 0:
                row.first_session = row.first_session or session
                row.last_session = session
        if row.sessions_missing == 0 and row.sessions_partial == 0:
            row.quality = DataQuality.COMPLETE
        elif row.sessions_full == 0 and row.sessions_suspended == 0 and row.sessions_pre_listing == 0:
            row.quality = DataQuality.UNAVAILABLE
        else:
            row.quality = DataQuality.PARTIAL
        rows.append(row)
    return rows, gaps[:5000]


def _coverage_rows_from_gaps(
    gaps: list[GapRecord], symbols: list[str], sessions: list[date]
) -> list[GapRecord]:
    return gaps


def _overall_quality(coverage: list[CoverageRow], flags: set[str]) -> DataQuality:
    if not coverage:
        return DataQuality.UNAVAILABLE
    if all(row.quality is DataQuality.COMPLETE for row in coverage) and not flags:
        return DataQuality.COMPLETE
    if all(row.quality is DataQuality.UNAVAILABLE for row in coverage):
        return DataQuality.UNAVAILABLE
    return DataQuality.PARTIAL


def _content_hash(
    manifest_base: dict[str, Any],
    *,
    minute_bars: list[Bar],
    daily_bars: list[DailyBar],
    rules: list[MarketRule],
    actions: list[CorporateAction],
    benchmark: dict[date, Decimal],
    plans: list[ReplayPlan],
) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(manifest_base).encode("utf-8"))
    for bar in sorted(minute_bars, key=lambda item: (item.symbol, item.end_at)):
        digest.update(
            f"{bar.symbol}|{bar.end_at.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|"
            f"{bar.volume}|{bar.amount}|{bar.frequency}|{bar.source}|{bar.volume_unit}\n".encode("utf-8")
        )
    for bar in sorted(daily_bars, key=lambda item: (item.symbol, item.session)):
        digest.update(
            f"D|{bar.symbol}|{bar.session.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|"
            f"{bar.volume}|{bar.amount}|{bar.source}\n".encode("utf-8")
        )
    for rule in sorted(rules, key=lambda item: (item.symbol, item.session)):
        digest.update(f"R|{canonical_json(rule.__dict__)}\n".encode("utf-8"))
    for action in sorted(actions, key=lambda item: (item.symbol, item.ex_date, item.action_type)):
        digest.update(f"A|{canonical_json(action.to_dict())}\n".encode("utf-8"))
    for session, close in sorted(benchmark.items()):
        digest.update(f"B|{session.isoformat()}|{close}\n".encode("utf-8"))
    for plan in sorted(plans, key=lambda item: item.plan_key):
        digest.update(f"P|{plan.plan_key}|{plan.fingerprint()}\n".encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _normalize_daily_bars(bars: Iterable[DailyBar]) -> tuple[list[DailyBar], list[str]]:
    dedup: dict[tuple[str, date], DailyBar] = {}
    flags: set[str] = set()
    for bar in bars:
        symbol = normalize_cn_symbol(bar.symbol)
        if not symbol or not _valid_ohlc(bar.open, bar.high, bar.low, bar.close):
            flags.add("invalid_daily_bar_skipped")
            continue
        volume, unit, volume_flag = _normalize_volume(bar.volume, bar.amount, bar.close)
        if volume_flag:
            flags.add(volume_flag)
        dedup[(symbol, bar.session)] = DailyBar(
            symbol=symbol,
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=volume,
            amount=bar.amount,
            source=bar.source,
            volume_unit=unit,
        )
    return [dedup[key] for key in sorted(dedup)], sorted(flags)


def _normalize_minute_bars(bars: Iterable[Bar]) -> tuple[list[Bar], list[str]]:
    dedup: dict[tuple[str, datetime], Bar] = {}
    flags: set[str] = set()
    for bar in bars:
        symbol = normalize_cn_symbol(bar.symbol)
        if not symbol or not _valid_ohlc(bar.open, bar.high, bar.low, bar.close):
            flags.add("invalid_minute_bar_skipped")
            continue
        volume, unit, volume_flag = _normalize_volume(bar.volume, bar.amount, bar.close)
        if volume_flag:
            flags.add(volume_flag)
        dedup[(symbol, bar.end_at)] = Bar(
            symbol=symbol,
            end_at=bar.end_at,
            session=bar.session,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=volume,
            amount=bar.amount,
            frequency=bar.frequency or "5m",
            source=bar.source,
            volume_unit=unit,
        )
    return [dedup[key] for key in sorted(dedup, key=lambda item: (item[0], item[1]))], sorted(flags)


def _valid_ohlc(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> bool:
    if min(open_price, high, low, close) <= 0:
        return False
    if high < max(open_price, close, low):
        return False
    if low > min(open_price, close, high):
        return False
    return True


def _normalize_volume(
    volume: Decimal, amount: Decimal, close: Decimal
) -> tuple[Decimal, str, str | None]:
    if volume <= 0:
        return volume, "shares", None
    if amount <= 0 or close <= 0:
        return volume, "shares", None
    vwap = amount / volume
    if Decimal("0.5") * close <= vwap <= Decimal("2") * close:
        return volume, "shares", None
    if Decimal("50") * close <= vwap <= Decimal("200") * close:
        return volume * Decimal("100"), "shares", "volume_unit_normalized_lots_to_shares"
    return volume, "unknown", "volume_unit_suspect"


def _daily_from_row(row: StockDaily) -> DailyBar | None:
    try:
        open_price = Decimal(str(row.open))
        high = Decimal(str(row.high))
        low = Decimal(str(row.low))
        close = Decimal(str(row.close))
        volume = Decimal(str(row.volume or 0))
        amount = Decimal(str(row.amount or 0))
    except Exception:
        return None
    if not _valid_ohlc(open_price, high, low, close):
        return None
    return DailyBar(
        symbol=normalize_cn_symbol(row.code),
        session=row.date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        source=row.data_source or "",
    )


def _minute_from_row(row: StockMinuteBar) -> Bar | None:
    try:
        open_price = Decimal(str(row.open))
        high = Decimal(str(row.high))
        low = Decimal(str(row.low))
        close = Decimal(str(row.close))
        volume = Decimal(str(row.volume or 0))
        amount = Decimal(str(row.amount or 0))
        end_at = row.bar_datetime
    except Exception:
        return None
    if end_at is None or not _valid_ohlc(open_price, high, low, close):
        return None
    return Bar(
        symbol=normalize_cn_symbol(row.code),
        end_at=end_at,
        session=row.bar_date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        frequency="5m",
        source=row.data_source or "",
    )


def _benchmark_code_candidates(symbol: str) -> list[str]:
    digits = normalize_cn_symbol(symbol)
    return sorted({digits, f"sh{digits}", f"sz{digits}", f"{digits}.SH", f"{digits}.SZ", symbol})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_or_empty(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _create_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE sessions(session TEXT PRIMARY KEY);
        CREATE TABLE symbols(symbol TEXT PRIMARY KEY);
        CREATE TABLE minute_bars(
            symbol TEXT NOT NULL, end_at TEXT NOT NULL, session TEXT NOT NULL,
            open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
            volume TEXT NOT NULL, amount TEXT NOT NULL, frequency TEXT NOT NULL,
            source TEXT, volume_unit TEXT,
            PRIMARY KEY(symbol, end_at)
        );
        CREATE INDEX ix_snapshot_minute_session ON minute_bars(session, symbol);
        CREATE TABLE daily_bars(
            symbol TEXT NOT NULL, session TEXT NOT NULL,
            open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
            volume TEXT NOT NULL, amount TEXT NOT NULL, source TEXT, volume_unit TEXT,
            PRIMARY KEY(symbol, session)
        );
        CREATE TABLE rules(
            symbol TEXT NOT NULL, session TEXT NOT NULL, venue TEXT, board TEXT,
            tick_size TEXT, buy_lot_size INTEGER, price_limit_ratio TEXT, previous_close TEXT,
            limit_up TEXT, limit_down TEXT, t_plus_one INTEGER, suspended INTEGER,
            st_status INTEGER, st_status_verified INTEGER, listing_status TEXT,
            listing_status_verified INTEGER, instrument_type TEXT, instrument_type_verified INTEGER,
            rule_source TEXT, rule_version TEXT, flags_json TEXT,
            PRIMARY KEY(symbol, session)
        );
        CREATE TABLE corporate_actions(
            symbol TEXT NOT NULL, ex_date TEXT NOT NULL, action_type TEXT NOT NULL,
            cash_per_share TEXT, stock_ratio TEXT, split_ratio TEXT,
            version TEXT, source TEXT, verified INTEGER, payload_json TEXT,
            PRIMARY KEY(symbol, ex_date, action_type, version)
        );
        CREATE TABLE benchmark(session TEXT PRIMARY KEY, close TEXT NOT NULL, symbol TEXT);
        CREATE TABLE coverage(
            symbol TEXT NOT NULL, session TEXT NOT NULL, status TEXT NOT NULL,
            actual_bars INTEGER, expected_bars INTEGER, first_bar_at TEXT, last_bar_at TEXT,
            downgraded INTEGER DEFAULT 0,
            PRIMARY KEY(symbol, session)
        );
        CREATE TABLE plans(plan_key TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
        """
    )


__all__ = [
    "DAILY_BARS_PER_SESSION",
    "MINUTE_BARS_PER_SESSION",
    "SnapshotBuildResult",
    "SnapshotBuilder",
    "SnapshotError",
    "load_snapshot",
    "write_snapshot",
]
