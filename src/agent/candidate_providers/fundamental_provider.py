# -*- coding: utf-8 -*-
"""Fundamental candidate provider backed by local SQLite snapshots.

P2 design principle: Trace-time discovery must read a precomputed table instead
of pulling full-market financial statements synchronously.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)

FUNDAMENTAL_CANDIDATE_TABLE = "fundamental_candidate_snapshot"
FUNDAMENTAL_EVENT_TABLE = "fundamental_candidate_events"
FUNDAMENTAL_DB_ENV = "AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH"

_SNAPSHOT_COLUMNS = {
    "code",
    "report_period",
    "roe",
    "gross_margin",
    "net_margin",
    "revenue_growth",
    "profit_growth",
    "operating_cashflow_ratio",
    "debt_ratio",
    "pe_ttm",
    "pb",
}


class FundamentalCandidateProvider:
    """Generate candidates from precomputed fundamental metrics."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or _default_db_path())

    def discover(
        self,
        *,
        limit: int = 8,
        strategy_names: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit or 8), 50))
        strategy_set = set(_normalize_strategy_names(strategy_names))
        validation_error = self._validate_db()
        if validation_error:
            return {
                "status": "unavailable",
                "provider": "fundamental",
                "db_path": self.db_path,
                "table": FUNDAMENTAL_CANDIDATE_TABLE,
                "candidates": [],
                "diagnostics": [{"source": "fundamental_candidate_db", "status": "unavailable", "error": validation_error}],
                "error": validation_error,
            }

        try:
            snapshots = self._load_snapshots()
            events = self._load_events()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("Fundamental candidate DB load failed: %s", message)
            return {
                "status": "failed",
                "provider": "fundamental",
                "db_path": self.db_path,
                "table": FUNDAMENTAL_CANDIDATE_TABLE,
                "candidates": [],
                "diagnostics": [{"source": "fundamental_candidate_db", "status": "failed", "error": message}],
                "error": message,
            }
        if snapshots.empty:
            return {
                "status": "empty",
                "provider": "fundamental",
                "db_path": self.db_path,
                "table": FUNDAMENTAL_CANDIDATE_TABLE,
                "candidates": [],
                "diagnostics": [{"source": "fundamental_candidate_snapshot", "status": "empty"}],
            }

        scored = _score_snapshots(snapshots, events, strategy_set)
        selected = scored[scored["screen_score"] >= 58].sort_values(
            ["screen_score", "quality_score", "growth_score", "code"],
            ascending=[False, False, False, False],
        ).head(effective_limit)
        candidates = [_candidate_payload(row) for _, row in selected.iterrows()]
        latest_period = str(snapshots["report_period"].max()) if "report_period" in snapshots.columns and not snapshots.empty else None
        latest_updated = str(snapshots["updated_at"].max()) if "updated_at" in snapshots.columns and not snapshots.empty else None
        return {
            "status": "ok" if candidates else "empty",
            "provider": "fundamental",
            "db_path": self.db_path,
            "table": FUNDAMENTAL_CANDIDATE_TABLE,
            "latest_period": latest_period,
            "updated_at": latest_updated,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "diagnostics": [{
                "source": "fundamental_candidate_snapshot",
                "status": "ok",
                "row_count": int(len(snapshots)),
                "event_count": int(len(events)),
                "strategies": sorted(strategy_set) if strategy_set else ["quality_growth", "value_quality", "cashflow_safety"],
            }],
        }

    def _validate_db(self) -> Optional[str]:
        path = Path(self.db_path).expanduser()
        if not path.exists():
            return f"fundamental candidate DB not found: {path}"
        try:
            with sqlite3.connect(str(path)) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (FUNDAMENTAL_CANDIDATE_TABLE,),
                ).fetchone()
                if not row:
                    return f"{FUNDAMENTAL_CANDIDATE_TABLE} table not found"
                columns = {item[1] for item in conn.execute(f"PRAGMA table_info({FUNDAMENTAL_CANDIDATE_TABLE})").fetchall()}
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        missing = sorted(_SNAPSHOT_COLUMNS - columns)
        if missing:
            return f"{FUNDAMENTAL_CANDIDATE_TABLE} missing columns: {', '.join(missing)}"
        return None

    def _load_snapshots(self) -> pd.DataFrame:
        with sqlite3.connect(str(Path(self.db_path).expanduser())) as conn:
            df = pd.read_sql(f"SELECT * FROM {FUNDAMENTAL_CANDIDATE_TABLE}", conn)
        if df.empty:
            return df
        df["code"] = df["code"].astype(str).str.strip()
        df = df[df["code"].str.fullmatch(r"\d{6}", na=False)]
        for col in (
            "roe",
            "gross_margin",
            "net_margin",
            "revenue_growth",
            "profit_growth",
            "operating_cashflow_ratio",
            "debt_ratio",
            "pe_ttm",
            "pb",
            "market_cap",
            "dividend_yield",
        ):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _load_events(self) -> pd.DataFrame:
        with sqlite3.connect(str(Path(self.db_path).expanduser())) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (FUNDAMENTAL_EVENT_TABLE,),
            ).fetchone()
            if not row:
                return pd.DataFrame(columns=["code", "event_type", "direction", "summary", "event_date"])
            return pd.read_sql(f"SELECT * FROM {FUNDAMENTAL_EVENT_TABLE}", conn)


def ensure_fundamental_candidate_schema(db_path: Optional[str] = None) -> str:
    """Create fundamental candidate tables if they do not exist."""
    path = Path(db_path or _default_db_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {FUNDAMENTAL_CANDIDATE_TABLE} (
                code TEXT PRIMARY KEY,
                name TEXT,
                report_period TEXT,
                ann_date TEXT,
                roe REAL,
                gross_margin REAL,
                net_margin REAL,
                revenue_growth REAL,
                profit_growth REAL,
                operating_cashflow_ratio REAL,
                debt_ratio REAL,
                pe_ttm REAL,
                pb REAL,
                market_cap REAL,
                dividend_yield REAL,
                source TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {FUNDAMENTAL_EVENT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_date TEXT,
                direction TEXT,
                amount REAL,
                summary TEXT,
                source TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{FUNDAMENTAL_EVENT_TABLE}_code_date ON {FUNDAMENTAL_EVENT_TABLE}(code, event_date)")
        conn.commit()
    return str(path)


def upsert_fundamental_snapshots(rows: Iterable[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    """Upsert precomputed fundamental snapshot rows."""
    path = ensure_fundamental_candidate_schema(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    normalized = []
    for row in rows:
        code = str(row.get("code") or row.get("symbol") or "").strip()
        if not code:
            continue
        normalized.append({
            "code": code,
            "name": _display_stock_name(code, row.get("name")),
            "report_period": str(row.get("report_period") or row.get("end_date") or ""),
            "ann_date": str(row.get("ann_date") or ""),
            "roe": _safe_float(row.get("roe")),
            "gross_margin": _safe_float(row.get("gross_margin")),
            "net_margin": _safe_float(row.get("net_margin")),
            "revenue_growth": _safe_float(row.get("revenue_growth")),
            "profit_growth": _safe_float(row.get("profit_growth")),
            "operating_cashflow_ratio": _safe_float(row.get("operating_cashflow_ratio")),
            "debt_ratio": _safe_float(row.get("debt_ratio")),
            "pe_ttm": _safe_float(row.get("pe_ttm")),
            "pb": _safe_float(row.get("pb")),
            "market_cap": _safe_float(row.get("market_cap")),
            "dividend_yield": _safe_float(row.get("dividend_yield")),
            "source": str(row.get("source") or "manual"),
            "updated_at": str(row.get("updated_at") or now),
        })
    if not normalized:
        return 0
    with sqlite3.connect(path) as conn:
        conn.executemany(
            f"""
            INSERT INTO {FUNDAMENTAL_CANDIDATE_TABLE} (
                code, name, report_period, ann_date, roe, gross_margin, net_margin,
                revenue_growth, profit_growth, operating_cashflow_ratio, debt_ratio,
                pe_ttm, pb, market_cap, dividend_yield, source, updated_at
            ) VALUES (
                :code, :name, :report_period, :ann_date, :roe, :gross_margin, :net_margin,
                :revenue_growth, :profit_growth, :operating_cashflow_ratio, :debt_ratio,
                :pe_ttm, :pb, :market_cap, :dividend_yield, :source, :updated_at
            )
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                report_period=excluded.report_period,
                ann_date=excluded.ann_date,
                roe=excluded.roe,
                gross_margin=excluded.gross_margin,
                net_margin=excluded.net_margin,
                revenue_growth=excluded.revenue_growth,
                profit_growth=excluded.profit_growth,
                operating_cashflow_ratio=excluded.operating_cashflow_ratio,
                debt_ratio=excluded.debt_ratio,
                pe_ttm=excluded.pe_ttm,
                pb=excluded.pb,
                market_cap=excluded.market_cap,
                dividend_yield=excluded.dividend_yield,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            normalized,
        )
        conn.commit()
    return len(normalized)


def _default_db_path() -> str:
    return os.getenv(FUNDAMENTAL_DB_ENV) or os.getenv("DATABASE_PATH") or "./data/stock_analysis.db"


def _normalize_strategy_names(strategy_names: Optional[Sequence[str]]) -> List[str]:
    raw = [str(item).strip().lower() for item in (strategy_names or []) if str(item or "").strip()]
    aliases = {
        "quality": "quality_growth",
        "growth": "quality_growth",
        "value": "value_quality",
        "cashflow": "cashflow_safety",
        "fundamental": "quality_growth",
    }
    return [aliases.get(item, item) for item in raw if aliases.get(item, item) in {"quality_growth", "value_quality", "cashflow_safety"}]


def _score_snapshots(df: pd.DataFrame, events: pd.DataFrame, strategy_set: set[str]) -> pd.DataFrame:
    result = df.copy()
    result["quality_score"] = _quality_score(result)
    result["growth_score"] = _growth_score(result)
    result["value_score"] = _value_score(result)
    result["cashflow_score"] = _cashflow_score(result)
    event_scores, event_summaries = _event_scores(events)
    result["event_score"] = result["code"].map(event_scores).fillna(0.0)
    result["event_summary"] = result["code"].map(event_summaries).fillna("")
    if not strategy_set:
        strategy_set = {"quality_growth", "value_quality", "cashflow_safety"}
    components: List[pd.Series] = []
    if "quality_growth" in strategy_set:
        components.append(result["quality_score"] * 0.45 + result["growth_score"] * 0.35 + result["cashflow_score"] * 0.20)
    if "value_quality" in strategy_set:
        components.append(result["value_score"] * 0.45 + result["quality_score"] * 0.35 + result["cashflow_score"] * 0.20)
    if "cashflow_safety" in strategy_set:
        components.append(result["cashflow_score"] * 0.45 + result["quality_score"] * 0.30 + result["value_score"] * 0.25)
    result["screen_score"] = pd.concat(components, axis=1).max(axis=1) + result["event_score"]
    result["screen_score"] = result["screen_score"].clip(0, 100).round(2)
    return result


def _quality_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(45.0, index=df.index)
    score += _num(df, "roe", 0).clip(-10, 25) * 1.4
    score += _num(df, "gross_margin", 0).clip(0, 60) * 0.35
    score += _num(df, "net_margin", 0).clip(-10, 30) * 0.7
    score -= (_num(df, "debt_ratio", 50) - 65).clip(lower=0) * 0.8
    return score.clip(0, 100)


def _growth_score(df: pd.DataFrame) -> pd.Series:
    revenue = _num(df, "revenue_growth", 0).clip(-50, 80)
    profit = _num(df, "profit_growth", 0).clip(-80, 120)
    score = 50 + revenue * 0.35 + profit * 0.25
    score -= (-profit - 10).clip(lower=0) * 0.45
    return score.clip(0, 100)


def _value_score(df: pd.DataFrame) -> pd.Series:
    pe = _num(df, "pe_ttm", 35)
    pb = _num(df, "pb", 4)
    score = pd.Series(50.0, index=df.index)
    score += (35 - pe).clip(-30, 30) * 0.75
    score += (4 - pb).clip(-4, 4) * 5.0
    score += _num(df, "dividend_yield", 0).clip(0, 8) * 2.0
    score -= (pe <= 0).astype(float) * 20
    return score.clip(0, 100)


def _cashflow_score(df: pd.DataFrame) -> pd.Series:
    ratio = _num(df, "operating_cashflow_ratio", 0).clip(-100, 200)
    score = 50 + ratio * 0.25
    score -= (-ratio - 10).clip(lower=0) * 0.5
    return score.clip(0, 100)


def _num(df: pd.DataFrame, column: str, default: float) -> pd.Series:
    if column not in df.columns:
        return pd.Series(float(default), index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(float(default))


def _event_scores(events: pd.DataFrame) -> tuple[Dict[str, float], Dict[str, str]]:
    scores: Dict[str, float] = {}
    summaries: Dict[str, str] = {}
    if events is None or events.empty:
        return scores, summaries
    for _, row in events.iterrows():
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        direction = str(row.get("direction") or "").lower()
        delta = 5.0 if direction in {"positive", "support", "bullish", "增持", "回购", "预增"} else -5.0 if direction in {"negative", "risk", "bearish", "减持", "解禁"} else 0.0
        scores[code] = scores.get(code, 0.0) + delta
        summary = str(row.get("summary") or row.get("event_type") or "").strip()
        if summary and code not in summaries:
            summaries[code] = summary
    return scores, summaries


def _candidate_payload(row: pd.Series) -> Dict[str, Any]:
    code = str(row.get("code") or "").strip()
    metrics = {
        "roe": _jsonable(row.get("roe")),
        "gross_margin": _jsonable(row.get("gross_margin")),
        "net_margin": _jsonable(row.get("net_margin")),
        "revenue_growth": _jsonable(row.get("revenue_growth")),
        "profit_growth": _jsonable(row.get("profit_growth")),
        "operating_cashflow_ratio": _jsonable(row.get("operating_cashflow_ratio")),
        "debt_ratio": _jsonable(row.get("debt_ratio")),
        "pe_ttm": _jsonable(row.get("pe_ttm")),
        "pb": _jsonable(row.get("pb")),
        "quality_score": _jsonable(row.get("quality_score")),
        "growth_score": _jsonable(row.get("growth_score")),
        "value_score": _jsonable(row.get("value_score")),
        "cashflow_score": _jsonable(row.get("cashflow_score")),
    }
    reason_dimensions = _candidate_reason_dimensions(row, metrics)
    payload = {
        "code": code,
        "name": _display_stock_name(code, row.get("name")),
        "source": "fundamental:quality_snapshot",
        "matched_strategies": ["fundamental_quality"],
        "strategy_tags": ["quality", "growth", "value"],
        "reason": "基本面预计算表筛选：质量、成长、估值和现金流综合得分较高。",
        "signal_score": float(row.get("screen_score") or 0),
        "latest_date": str(row.get("report_period") or ""),
        "report_period": str(row.get("report_period") or ""),
        "ann_date": str(row.get("ann_date") or ""),
        "metrics": metrics,
        "reason_dimensions": reason_dimensions,
    }
    event_summary = str(row.get("event_summary") or "").strip()
    if event_summary:
        payload["event_summary"] = event_summary
    return payload


def _candidate_reason_dimensions(row: pd.Series, metrics: Dict[str, Any]) -> List[Dict[str, str]]:
    result = [{
        "dimension": "fundamental",
        "label": "基本面",
        "detail": (
            f"ROE={_fmt(metrics.get('roe'))}；营收增速={_fmt(metrics.get('revenue_growth'))}%；"
            f"利润增速={_fmt(metrics.get('profit_growth'))}%；经营现金流/利润={_fmt(metrics.get('operating_cashflow_ratio'))}%"
        ),
    }]
    result.append({
        "dimension": "fundamental",
        "label": "估值质量",
        "detail": f"PE(TTM)={_fmt(metrics.get('pe_ttm'))}；PB={_fmt(metrics.get('pb'))}；负债率={_fmt(metrics.get('debt_ratio'))}%",
    })
    event_summary = str(row.get("event_summary") or "").strip()
    if event_summary:
        result.append({"dimension": "fundamental", "label": "基本面事件", "detail": event_summary})
    return result


def _display_stock_name(code: Any, current_name: Any = None) -> str:
    code_text = str(code or "").strip()
    current_text = str(current_name or "").strip()
    if is_meaningful_stock_name(current_text, code_text):
        return current_text
    for name in (STOCK_NAME_MAP.get(code_text), get_index_stock_name(code_text)):
        if is_meaningful_stock_name(name, code_text):
            return str(name)
    return code_text


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _jsonable(value: Any) -> Optional[float]:
    number = _safe_float(value)
    return round(number, 4) if number is not None else None


def _fmt(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "-"
    return f"{number:.2f}".rstrip("0").rstrip(".")
