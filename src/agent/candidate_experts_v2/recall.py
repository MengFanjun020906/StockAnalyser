# -*- coding: utf-8 -*-
"""召回层 — 策略→特征 (选股链路重构 P3).

三件事:
1. 调用现有数据源(复用 _build_seed_pool_result 的结果),将每只股票的
   SeedItem 转换为 FeatureRow + FeatureFlag.  召回层只陈述可证伪事实,
   **不打分、不排序**.
2. 对全部召回 row 跑 FactSheet Phase A(纯本地 SQLite,无网络请求).
3. 按「命中探测器数量(hit count)」做粗筛截断(RECALL_COARSE_CAP);
   并列边界全保,不靠分数做二次裁剪.

回滚: AGENT_CANDIDATE_DISCOVERY_MODE=deterministic 即退回旧链路,
本模块不影响 llm_expert_committee / deterministic 两条现有路径.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.agent.candidate_experts_v2.schemas import (
    FactSheet,
    FeatureFlag,
    FeatureRow,
    RecallResult,
    SeedItem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source → detector key 映射
# ---------------------------------------------------------------------------

# detector key 规范: {source}:{signal} 或直接 source 名
_SOURCE_DETECTOR_MAP: Dict[str, str] = {
    "daily_screener":            "screener:ma_breakout",
    "local_price_volume":        "screener:price_volume",
    "capital_flow_anomaly":      "moneyflow",       # 细化见 _api_label_detector
    "dragon_tiger":              "dragon_tiger:net_buy",
    "limit_up_pool":             "limit_list",
    "alphasift":                 "alphasift:high_tight_flag",
    "sequoia":                   "sequoia:volume_breakout",
    "low_base_structure":        "low_base:range_low",
    "fundamental_snapshot":      "fundamental:turnaround",
    "sector_theme":              "sector:strong",
    "hot_rank":                  "hot_rank:popularity",
    "northbound_stock_connect":  "northbound:net_buy",
    "margin_financing":          "margin:financing_buy",
    "block_trade":               "block_trade:large_trade",
    "valuation_liquidity":       "valuation:low_pe",
    "event_impact":              "news:event_impact",
    "news_momentum":             "news:momentum",
    "user_watchlist":            "watchlist:user",
    "fallback":                  "fallback:generic",
}

_DETECTOR_KIND_MAP: Dict[str, str] = {
    "screener:ma_breakout":      "pattern",
    "screener:price_volume":     "pattern",
    "moneyflow_ths":             "capital",
    "moneyflow_dc":              "capital",
    "moneyflow":                 "capital",
    "dragon_tiger:net_buy":      "capital",
    "limit_list":                "limit",
    "limit_step:first_board":    "limit",
    "alphasift:high_tight_flag": "pattern",
    "sequoia:volume_breakout":   "pattern",
    "low_base:range_low":        "position",
    "fundamental:turnaround":    "fundamental",
    "sector:strong":             "sector",
    "hot_rank:popularity":       "capital",
    "northbound:net_buy":        "capital",
    "margin:financing_buy":      "capital",
    "block_trade:large_trade":   "capital",
    "valuation:low_pe":          "fundamental",
    "news:event_impact":         "news",
    "news:momentum":             "news",
    "watchlist:user":            "pattern",
    "fallback:generic":          "unknown",
}


def _source_to_detector(source: str, metrics: Dict[str, Any]) -> str:
    """Resolve detector key, using api_label for capital_flow_anomaly sources."""
    if source == "capital_flow_anomaly":
        api_label = str(metrics.get("api_label") or "")
        if api_label in ("moneyflow_ths", "moneyflow_dc"):
            return api_label
    return _SOURCE_DETECTOR_MAP.get(source, source)


def _seed_to_flags(seed: SeedItem) -> List[FeatureFlag]:
    """Convert one (already-deduped, merged) SeedItem → list of FeatureFlags.

    One FeatureFlag is produced per recall source recorded in
    ``seed.extras["recall_sources"]``.  All flags for the same code share the
    same ``metrics`` dict (derived from ``seed.extras["metrics"]``); the
    ``detector`` key differentiates them.
    """
    raw_metrics: Dict[str, Any] = {}
    if isinstance(seed.extras, dict):
        m = seed.extras.get("metrics")
        if isinstance(m, dict):
            raw_metrics = dict(m)

    # Enrich metrics with top-level trigger_signal values (signal_type → value).
    for sig in seed.trigger_signals or []:
        if not isinstance(sig, dict):
            continue
        stype = str(sig.get("signal_type") or "")
        val = sig.get("value")
        if stype and val is not None and stype not in raw_metrics:
            raw_metrics[stype] = val

    # Add within-source rank as normalized_rank (event fact, not global rank).
    rank = raw_metrics.get("rank")
    if rank is not None:
        raw_metrics.setdefault("normalized_rank", rank)

    recall_sources: List[str] = list(seed.extras.get("recall_sources") or [seed.source])
    freshness = seed.freshness or "unknown"
    hint = seed.hint or ""

    flags: List[FeatureFlag] = []
    seen_detectors: set = set()
    for src in recall_sources:
        detector = _source_to_detector(src, raw_metrics)
        if detector in seen_detectors:
            continue
        seen_detectors.add(detector)
        kind = _DETECTOR_KIND_MAP.get(detector, "unknown")
        flags.append(
            FeatureFlag(
                detector=detector,
                kind=kind,        # type: ignore[arg-type]
                summary=hint,
                metrics=dict(raw_metrics),
                as_of=freshness,
            )
        )
    return flags


def _seeds_to_rows(seeds: Sequence[SeedItem]) -> Dict[str, FeatureRow]:
    """Build a {code: FeatureRow} map from the assembled seed pool."""
    rows: Dict[str, FeatureRow] = {}
    for seed in seeds:
        code = str(seed.code or "").strip()
        if not code:
            continue
        flags = _seed_to_flags(seed)
        recall_sources: List[str] = list(
            (seed.extras.get("recall_sources") or []) if isinstance(seed.extras, dict) else []
        ) or [seed.source]
        if code not in rows:
            rows[code] = FeatureRow(
                code=code,
                name=seed.name or "",
                market=seed.market or "cn",
                flags=flags,
                recall_sources=recall_sources,
            )
        else:
            # Should not happen after committee._dedupe_by_code, but be safe.
            existing = rows[code]
            existing.flags.extend(flags)
            for s in recall_sources:
                if s not in existing.recall_sources:
                    existing.recall_sources.append(s)
    return rows


# ---------------------------------------------------------------------------
# FactSheet Phase A — 纯本地, 批量读取 SQLite
# ---------------------------------------------------------------------------

def _db_path() -> Optional[Path]:
    raw = (
        os.getenv("SEQUOIA_CANDIDATE_DB_PATH")
        or os.getenv("ALPHASIFT_CANDIDATE_DB_PATH")
        or "Sequoia-X/data/sequoia_v2.db"
    )
    p = Path(raw).expanduser()
    return p if p.exists() else None


def _load_daily_bars_batch(codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Load OHLCV bars for *codes* from local SQLite (no network).

    Returns ``{code: [oldest→newest bar dicts]}``.  Missing codes → empty list.
    """
    if not codes:
        return {}
    db = _db_path()
    if db is None:
        return {}
    try:
        import pandas as pd

        placeholders = ",".join("?" * len(codes))
        with sqlite3.connect(str(db)) as conn:
            df = pd.read_sql(
                f"SELECT symbol, date, open, high, low, close, volume, turnover "
                f"FROM stock_daily WHERE symbol IN ({placeholders}) ORDER BY symbol, date",
                conn,
                params=codes,
            )
        if df.empty:
            return {}
        for col in ("open", "high", "low", "close", "volume", "turnover"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        result: Dict[str, List[Dict[str, Any]]] = {}
        for sym, grp in df.groupby("symbol", sort=False):
            result[str(sym)] = grp.to_dict("records")
        return result
    except Exception as exc:
        logger.warning("recall: failed to load daily bars from SQLite: %s", exc)
        return {}


def _build_fact_sheet_phase_a(
    code: str,
    bars: List[Dict[str, Any]],
    *,
    market: str = "cn",
    min_avg_turnover: Optional[float] = None,
    violent_outflow_threshold: Optional[float] = None,
    breakdown_accel_threshold: Optional[float] = None,
) -> FactSheet:
    """Call build_fact_sheet with local bars only (Phase A, no network)."""
    try:
        import pandas as pd
        from src.agent.candidate_experts_v2.fact_sheet import build_fact_sheet

        df = pd.DataFrame(bars) if bars else None
        return build_fact_sheet(
            code,
            df,
            market=market,
            min_avg_turnover=min_avg_turnover,
            violent_outflow_threshold=violent_outflow_threshold,
            breakdown_accel_threshold=breakdown_accel_threshold,
            freshness="local_phase_a",
        )
    except Exception as exc:
        logger.debug("recall: fact_sheet phase A failed for %s: %s", code, exc)
        return FactSheet(code=code, warnings=[f"phase_a_error: {exc}"])


# ---------------------------------------------------------------------------
# Coarse cap — by hit count (fact, not score)
# ---------------------------------------------------------------------------

def _apply_coarse_cap(rows: List[FeatureRow], cap: int) -> List[FeatureRow]:
    """Mark rows as coarse_kept=False when hit-count falls below the cap boundary.

    Ties at the boundary are **always kept** (prefer over-cap to unfair culling).
    """
    if len(rows) <= cap:
        return rows

    sorted_rows = sorted(rows, key=lambda r: len(r.flags), reverse=True)
    # find the hit-count of the last kept item at position cap-1
    boundary_count = len(sorted_rows[cap - 1].flags)

    for r in sorted_rows:
        if len(r.flags) < boundary_count:
            r.coarse_kept = False
            r.coarse_drop_reason = (
                f"hit_count={len(r.flags)} < boundary={boundary_count} at cap={cap}"
            )
    return sorted_rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Module-level wrapper — kept separate so unit tests can patch it without
# triggering the full committee.py import at module load time.
def _build_seed_pool_result(*args: Any, **kwargs: Any) -> Any:
    from src.agent.candidate_experts_v2.committee import (
        _build_seed_pool_result as _impl,
    )
    return _impl(*args, **kwargs)


def build_recall_pool(
    *,
    market: str,
    seed_symbols: Sequence[str],
    tool_registry: Any,
    today: Optional[str] = None,
    coarse_cap: int = 120,
    prebuilt_pool: Optional[Any] = None,
    min_avg_turnover: Optional[float] = None,
    violent_outflow_threshold: Optional[float] = None,
    breakdown_accel_threshold: Optional[float] = None,
) -> RecallResult:
    """Build the P3 recall pool: features + FactSheet Phase A + coarse cap.

    Parameters
    ----------
    market:
        Market identifier ("cn" / "hk" / "us").
    seed_symbols:
        User-provided watchlist symbols passed through to the seed builder.
    tool_registry:
        The shared tool_registry used by the seed builder (online calls).
    today:
        ISO date string (YYYY-MM-DD) used by the seed builder for trade dates.
    coarse_cap:
        Maximum number of FeatureRows kept for downstream processing.
        Corresponds to ``AGENT_RECALL_COARSE_CAP``.  Boundary ties are fully
        preserved, so the actual kept count may slightly exceed this value.
    prebuilt_pool:
        Optional SeedPoolBuildResult from the caller. When provided, recall
        reuses those seeds instead of rebuilding the seed pool, preserving trace
        consistency and avoiding a second round of online source calls.
    min_avg_turnover, violent_outflow_threshold, breakdown_accel_threshold:
        Forwarded to ``build_fact_sheet`` / FactSheet red-line logic.
    """
    # --- Step 1: call the existing seed builder to collect candidates ----------
    # Use a generous total_limit so the coarse cap, not the seed-builder cap,
    # is the binding constraint (seed builder sums to ~126 across all sources).
    seed_total_limit = max(coarse_cap + 60, 180)
    pool = prebuilt_pool
    if pool is None:
        pool = _build_seed_pool_result(
            market=market,
            seed_symbols=list(seed_symbols),
            tool_registry=tool_registry,
            today=today,
            limit_per_source=20,         # give each source slightly more headroom
            total_limit=seed_total_limit,
        )

    # --- Step 2: SeedItems → FeatureRows / FeatureFlags -----------------------
    rows_by_code = _seeds_to_rows(pool.seeds)
    all_rows: List[FeatureRow] = list(rows_by_code.values())

    logger.info(
        "recall: converted %d seeds → %d feature rows",
        len(pool.seeds),
        len(all_rows),
    )

    # --- Step 3: FactSheet Phase A (local SQLite, batch, no network) -----------
    codes = [r.code for r in all_rows]
    bars_by_code = _load_daily_bars_batch(codes)
    for row in all_rows:
        bars = bars_by_code.get(row.code, [])
        row.fact_sheet = _build_fact_sheet_phase_a(
            row.code,
            bars,
            market=row.market,
            min_avg_turnover=min_avg_turnover,
            violent_outflow_threshold=violent_outflow_threshold,
            breakdown_accel_threshold=breakdown_accel_threshold,
        )

    # --- Step 4: coarse cap by hit count (not priority score) ------------------
    all_rows = _apply_coarse_cap(all_rows, coarse_cap)
    kept_rows = [r for r in all_rows if r.coarse_kept]
    coarse_truncated = len(kept_rows) < len(all_rows)

    # --- Step 5: diagnostics ---------------------------------------------------
    hit_count_hist: Dict[int, int] = {}
    for r in all_rows:
        n = len(r.flags)
        hit_count_hist[n] = hit_count_hist.get(n, 0) + 1

    sources: Dict[str, int] = {}
    for r in kept_rows:
        for s in r.recall_sources:
            sources[s] = sources.get(s, 0) + 1

    if coarse_truncated:
        logger.info(
            "recall: coarse cap=%d applied, kept=%d / total=%d; "
            "hit_count_hist=%s",
            coarse_cap,
            len(kept_rows),
            len(all_rows),
            hit_count_hist,
        )

    return RecallResult(
        rows=kept_rows,
        all_rows=all_rows,
        diagnostics=pool.diagnostics,
        coarse_truncated=coarse_truncated,
        hit_count_hist=hit_count_hist,
        sources=sources,
        total_in=len(all_rows),
        total_kept=len(kept_rows),
    )
