# -*- coding: utf-8 -*-
"""In-memory as-of data view over a frozen experiment snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from .entities import Bar, CorporateAction, DailyBar
from .rules import MarketRule, derive_rule
from .schemas import CoverageRow, SnapshotManifest


@dataclass
class InMemoryDataView:
    """A read-only view whose maps are fully populated from a snapshot.

    Strategies see daily bars only up to ``session``.  The execution matcher
    receives the current bar explicitly and is the only component allowed to
    read its OHLC for fills.
    """

    granularity: str
    start_date: date
    end_date: date
    sessions: list[date]
    minute_bars: dict[str, dict[datetime, Bar]] = field(default_factory=dict)
    daily_bars_by_symbol: dict[str, dict[date, DailyBar]] = field(default_factory=dict)
    rules: dict[tuple[str, date], MarketRule] = field(default_factory=dict)
    actions: dict[date, list[CorporateAction]] = field(default_factory=dict)
    benchmark: dict[date, Decimal] = field(default_factory=dict)
    benchmark_symbol: str | None = None
    coverage: list[CoverageRow] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    downgraded_sessions: set[tuple[str, date]] = field(default_factory=set)
    manifest: SnapshotManifest | None = None
    _bars_by_time: dict[datetime, dict[str, Bar]] = field(default_factory=dict, init=False)
    _daily_sorted: dict[str, list[DailyBar]] = field(default_factory=dict, init=False)
    _last_prices: dict[str, tuple[Decimal, date]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if not self._bars_by_time:
            for symbol, bars in self.minute_bars.items():
                for timestamp, bar in bars.items():
                    self._bars_by_time.setdefault(timestamp, {})[symbol] = bar
        if not self._daily_sorted:
            for symbol, bars in self.daily_bars_by_symbol.items():
                self._daily_sorted[symbol] = [bars[key] for key in sorted(bars)]
        self._recompute_last_prices()

    @classmethod
    def from_records(
        cls,
        *,
        granularity: str,
        start_date: date,
        end_date: date,
        sessions: list[date],
        bars: Iterable[Bar],
        daily: Iterable[DailyBar],
        rules: Iterable[MarketRule],
        actions: Iterable[CorporateAction] = (),
        benchmark: dict[date, Decimal] | None = None,
        benchmark_symbol: str | None = None,
        coverage: list[CoverageRow] | None = None,
        quality_flags: list[str] | None = None,
        downgraded_sessions: set[tuple[str, date]] | None = None,
        manifest: SnapshotManifest | None = None,
    ) -> "InMemoryDataView":
        minute_map: dict[str, dict[datetime, Bar]] = {}
        for bar in bars:
            minute_map.setdefault(bar.symbol, {})[bar.end_at] = bar
        daily_map: dict[str, dict[date, DailyBar]] = {}
        for item in daily:
            daily_map.setdefault(item.symbol, {})[item.session] = item
        rule_map = {(rule.symbol, rule.session): rule for rule in rules}
        action_map: dict[date, list[CorporateAction]] = {}
        for action in actions:
            action_map.setdefault(action.ex_date, []).append(action)
        return cls(
            granularity=granularity,
            start_date=start_date,
            end_date=end_date,
            sessions=sessions,
            minute_bars=minute_map,
            daily_bars_by_symbol=daily_map,
            rules=rule_map,
            actions=action_map,
            benchmark=benchmark or {},
            benchmark_symbol=benchmark_symbol,
            coverage=coverage or [],
            quality_flags=quality_flags or [],
            downgraded_sessions=downgraded_sessions or set(),
            manifest=manifest,
        )

    # ------------------------------------------------------------------
    # Execution-facing API
    # ------------------------------------------------------------------
    def bars_at(self, timestamp: datetime) -> dict[str, Bar]:
        bars = self._bars_by_time.get(timestamp)
        if bars is not None:
            return bars
        if self.granularity == "1d":
            return {symbol: bar for symbol, bar in self._daily_as_bars(timestamp).items()}
        return {}

    def bar_for(self, symbol: str, timestamp: datetime) -> Bar | None:
        bars = self.bars_at(timestamp)
        return bars.get(symbol)

    def daily_bars(self, symbol: str, up_to_session: date) -> list[DailyBar]:
        bars = self._daily_sorted.get(symbol, [])
        return [item for item in bars if item.session <= up_to_session]

    def daily_bar(self, symbol: str, session: date) -> DailyBar | None:
        return self.daily_bars_by_symbol.get(symbol, {}).get(session)

    def previous_close(self, symbol: str, session: date) -> Decimal | None:
        bars = self._daily_sorted.get(symbol, [])
        previous = None
        for bar in bars:
            if bar.session >= session:
                break
            previous = bar
        if previous is not None:
            return previous.close
        minute = self.minute_bars.get(symbol, {})
        prior = [key for key in minute if key.date() < session]
        if prior:
            return minute[max(prior)].close
        return None

    def last_price(self, symbol: str, session: date) -> tuple[Decimal | None, bool]:
        daily = self.daily_bars_by_symbol.get(symbol, {}).get(session)
        if daily is not None and daily.close > 0:
            return daily.close, False
        minute = self.minute_bars.get(symbol, {})
        same_day = [key for key in minute if key.date() == session]
        if same_day:
            return minute[max(same_day)].close, False
        cached = self._last_prices.get(symbol)
        if cached is not None:
            return cached[0], True
        return None, True

    def rule(self, symbol: str, session: date) -> MarketRule:
        existing = self.rules.get((symbol, session))
        if existing is not None:
            return existing
        rule = derive_rule(
            symbol,
            session,
            previous_close=self.previous_close(symbol, session),
            flags=("market_rules_missing_snapshot",),
        )
        self.rules[(symbol, session)] = rule
        return rule

    def corporate_actions(self, session: date) -> list[CorporateAction]:
        return self.actions.get(session, [])

    def benchmark_close(self, session: date) -> Decimal | None:
        return self.benchmark.get(session)

    def has_benchmark(self) -> bool:
        return bool(self.benchmark)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _daily_as_bars(self, timestamp: datetime) -> dict[str, Bar]:
        result: dict[str, Bar] = {}
        for symbol, bars in self.daily_bars_by_symbol.items():
            item = bars.get(timestamp.date())
            if item is not None:
                result[symbol] = Bar(
                    symbol=symbol,
                    end_at=timestamp,
                    session=item.session,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.volume,
                    amount=item.amount,
                    frequency="1d",
                    source=item.source,
                    volume_unit=item.volume_unit,
                )
        return result

    def _recompute_last_prices(self) -> None:
        self._last_prices.clear()
        for symbol, bars in self._daily_sorted.items():
            if bars:
                self._last_prices[symbol] = (bars[-1].close, bars[-1].session)


__all__ = ["InMemoryDataView"]
