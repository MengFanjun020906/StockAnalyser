# -*- coding: utf-8 -*-
"""Resolve explicit vs local-coverage research universes."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from src.storage import DatabaseManager

from src.backtest.schemas import ExperimentConfig, ResearchUniverseMode

_A_SHARE_PREFIX = re.compile(r"^(600|601|603|605|688|689|000|001|002|003|300|301)")


class ResearchUniverseError(RuntimeError):
    pass


def resolve_research_universe(
    config: ExperimentConfig,
    *,
    db_manager: DatabaseManager | None,
    override: list[str] | None = None,
) -> tuple[list[str], str]:
    """Return (symbols, source_label).

    ``auto_local_coverage`` is explicitly a local-coverage research universe, not
    a claim of full-market point-in-time coverage.  It only uses bars available
    through the experiment end date and never reads today's stock list.
    """
    research = config.research
    if research is None:
        return [], "none"
    if override:
        symbols = _normalize(override)[: research.auto_universe_limit]
        return symbols, "override"
    if research.universe_mode is ResearchUniverseMode.EXPLICIT:
        return list(research.universe), "explicit"
    if db_manager is None:
        raise ResearchUniverseError("auto universe requires a database manager")
    lookback_start = config.start_date - timedelta(days=max(90, research.min_history_bars * 4))
    sql = """
        SELECT code,
               COUNT(*) AS bar_count,
               SUM(CASE WHEN amount > 0 THEN amount ELSE volume * close END) AS turnover
        FROM stock_daily
        WHERE date >= ? AND date <= ?
        GROUP BY code
        HAVING bar_count >= ?
        ORDER BY turnover DESC, bar_count DESC, code ASC
        LIMIT ?
    """
    with db_manager._engine.connect() as conn:
        rows = conn.exec_driver_sql(
            sql,
            (
                lookback_start.isoformat(),
                config.end_date.isoformat(),
                int(research.min_history_bars),
                int(research.auto_universe_limit),
            ),
        ).fetchall()
    symbols = []
    for row in rows:
        code = _normalize_code(row[0])
        if code and code not in symbols:
            symbols.append(code)
    return symbols, "local_coverage"


def _normalize(values: list[str]) -> list[str]:
    result = []
    for item in values:
        code = _normalize_code(item)
        if code and code not in result:
            result.append(code)
    return result


def _normalize_code(value: Any) -> str:
    code = "".join(ch for ch in str(value or "") if ch.isdigit())[-6:]
    if len(code) != 6 or not _A_SHARE_PREFIX.match(code):
        return ""
    return code


__all__ = ["ResearchUniverseError", "resolve_research_universe"]
