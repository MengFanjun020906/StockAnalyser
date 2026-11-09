# -*- coding: utf-8 -*-
"""Build P3 research snapshots from the full-market Sequoia database."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.storage import DatabaseManager

from src.backtest.calendar import CnTradingCalendar
from src.backtest.schemas import DataQuality, ExperimentConfig, PriceBasis, ResearchUniverseMode
from src.backtest.snapshot import SnapshotBuildResult, SnapshotBuilder

from .sequoia import SequoiaDailySource, SequoiaCoverage

logger = logging.getLogger(__name__)


@dataclass
class ResearchDataset:
    snapshot: SnapshotBuildResult
    universe: list[str]
    universe_source: str
    data_source: str
    coverage: SequoiaCoverage | None = None
    warnings: list[str] = field(default_factory=list)


def resolve_research_universe(
    config: ExperimentConfig,
    *,
    db_manager: DatabaseManager | None,
    source: SequoiaDailySource | None = None,
    override: list[str] | None = None,
) -> tuple[list[str], str]:
    research = config.research
    if research is None:
        return [], "none"
    if override:
        return [_normalize_code(item) for item in override if _normalize_code(item)][: research.auto_universe_limit], "override"
    if research.universe_mode is ResearchUniverseMode.EXPLICIT:
        return list(research.universe), "explicit"
    source = source or SequoiaDailySource()
    if source.available:
        symbols = source.list_symbols(
            start_date=config.start_date,
            end_date=config.end_date,
            min_bars=research.min_history_bars,
            limit=research.auto_universe_limit,
        )
        if symbols:
            return symbols, "sequoia_local_coverage"
    # Fallback for test/CI environments without the Sequoia DB.
    if db_manager is None:
        return [], "unavailable"
    from .universe import resolve_research_universe as resolve_from_main

    symbols, _ = resolve_from_main(
        config,
        db_manager=db_manager,
        override=None,
    )
    return symbols, "main_db_local_coverage"


def build_research_dataset(
    config: ExperimentConfig,
    *,
    calendar: CnTradingCalendar,
    universe: list[str],
    db_manager: DatabaseManager | None,
    source: SequoiaDailySource | None = None,
    created_at: datetime | None = None,
) -> ResearchDataset:
    research = config.research
    if research is None:
        raise ValueError("research config is required")
    source = source or SequoiaDailySource()
    coverage = source.coverage() if source.available else None
    warnings: list[str] = []
    if source.available:
        lookback_start = config.start_date - timedelta(days=max(120, research.min_history_bars * 4))
        bars, rebased = source.daily_bars(universe, start_date=lookback_start, end_date=config.end_date)
        benchmark = {}
        benchmark_symbol = None
        if config.benchmark is not None:
            benchmark_symbol = _sequoia_index_symbol(config.benchmark.symbol)
            benchmark = source.benchmark(
                benchmark_symbol,
                start_date=config.start_date,
                end_date=config.end_date,
            )
            if not benchmark:
                benchmark_symbol = None
        builder = SnapshotBuilder(
            run_id="research-dataset",
            config=config,
            plans=[],
            calendar=calendar,
            db_manager=None,
            minute_bars=[],
            daily_bars=bars,
            rules=[],
            actions=[],
            benchmark=benchmark or None,
            benchmark_symbol=benchmark_symbol,
            symbols_override=universe,
            created_at=created_at,
        )
        snapshot = builder.build()
        flags = {"research_data_source:sequoia", "local_coverage_universe"}
        if rebased:
            flags.add("research_price_basis_rebased")
            flags.add("research_price_basis_unverified")
            snapshot.manifest.price_basis = PriceBasis.DAILY_BASIS_UNVERIFIED
            snapshot.manifest.quality = DataQuality.PARTIAL
        _append_quality_flags(snapshot, flags)
        if coverage is not None:
            if coverage.last_completed_date and coverage.last_completed_date < config.end_date:
                warnings.append(
                    f"research_data_end_lag:{coverage.last_completed_date.isoformat()}<{config.end_date.isoformat()}"
                )
            if coverage.min_date and coverage.min_date > config.start_date - timedelta(days=research.min_history_bars):
                warnings.append("research_data_warmup_limited")
        return ResearchDataset(
            snapshot=snapshot,
            universe=universe,
            universe_source=(
                "explicit"
                if research.universe_mode is ResearchUniverseMode.EXPLICIT
                else "sequoia_local_coverage"
            ),
            data_source=f"sequoia:{source.path}",
            coverage=coverage,
            warnings=warnings,
        )
    # Fallback: main StockDaily cache.
    builder = SnapshotBuilder(
        run_id="research-dataset",
        config=config,
        plans=[],
        calendar=calendar,
        db_manager=db_manager,
        symbols_override=universe,
        created_at=created_at,
    )
    snapshot = builder.build()
    _append_quality_flags(snapshot, {"research_data_source:main_db", "local_coverage_universe"})
    warnings.append("research_sequoia_unavailable")
    return ResearchDataset(
        snapshot=snapshot,
        universe=universe,
        universe_source="main_db_local_coverage",
        data_source="main_db",
        coverage=coverage,
        warnings=warnings,
    )


def _append_quality_flags(snapshot: SnapshotBuildResult, flags: set[str]) -> None:
    all_flags = sorted(set(snapshot.quality_flags) | flags)
    snapshot.quality_flags = all_flags
    snapshot.manifest.quality_flags = all_flags
    snapshot.data.quality_flags = all_flags


def _sequoia_index_symbol(symbol: str) -> str:
    digits = _normalize_code(symbol)
    if digits in {"000001", "000300", "000905", "000852"}:
        return f"{digits}.SH"
    if digits in {"399001", "399006"}:
        return f"{digits}.SZ"
    return symbol


def _normalize_code(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-6:]


__all__ = [
    "ResearchDataset",
    "build_research_dataset",
    "resolve_research_universe",
]
