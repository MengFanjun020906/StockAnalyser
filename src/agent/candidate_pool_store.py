# -*- coding: utf-8 -*-
"""Persistent store for Agent L1 candidate-pool runs."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

RUNS_TABLE = "agent_candidate_pool_runs"
ITEMS_TABLE = "agent_candidate_pool_items"


def _default_db_path() -> str:
    try:
        from src.config import get_config

        config = get_config()
        return str(getattr(config, "agent_candidate_pool_db_path", "") or getattr(config, "database_path", "./data/stock_analysis.db"))
    except Exception:
        return "./data/stock_analysis.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _source_family(item: Dict[str, Any]) -> str:
    sources = [str(src or "").strip() for src in item.get("recall_sources") or [] if str(src or "").strip()]
    source = str(item.get("source") or "").strip()
    if source:
        sources.append(source)
    for prefix, family in (
        ("alphasift:", "alphasift"),
        ("sequoia:", "sequoia"),
        ("fundamental:", "fundamental"),
        ("akshare:", "sector"),
        ("event_impact:", "event_impact"),
        ("news_momentum:", "news_momentum"),
        ("news_sentiment:", "news_sentiment"),
        ("user_seed", "user_seed"),
        ("fallback_seed_pool", "fallback"),
    ):
        if any(src == prefix or src.startswith(prefix) for src in sources):
            return family
    return source or "unknown"


def _extract_dimensions(item: Dict[str, Any]) -> List[str]:
    dimensions = item.get("candidate_dimensions")
    if isinstance(dimensions, list):
        result = [str(value) for value in dimensions if str(value or "").strip()]
        if result:
            return list(dict.fromkeys(result))
    result: List[str] = []
    for entry in item.get("reason_dimensions") or []:
        if not isinstance(entry, dict):
            continue
        dimension = str(entry.get("dimension") or "").strip()
        if dimension:
            result.append(dimension)
    return list(dict.fromkeys(result)) or ["unknown"]


def _reason_text(item: Dict[str, Any]) -> str:
    parts: List[str] = []
    for entry in item.get("reason_dimensions") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        detail = str(entry.get("detail") or "").strip()
        if detail:
            parts.append(f"{label}：{detail}" if label else detail)
    if parts:
        return "；".join(parts[:5])
    return str(item.get("reason") or "").strip()


def _fundamental_status_from_run(run: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    packets = run.get("expert_packets") or []
    packet = next(
        (
            item for item in packets
            if isinstance(item, dict) and item.get("expert") == "fundamental_expert"
        ),
        None,
    )
    fundamental_items = [
        item for item in items
        if "fundamental" in (item.get("candidate_dimensions") or [])
        or "fundamental_expert" in (item.get("candidate_experts") or [])
    ]
    status: Dict[str, Any] = {
        "enabled": packet is not None,
        "expert": "fundamental_expert",
        "status": "missing_packet",
        "candidate_count": len(fundamental_items),
        "latest_period": None,
        "updated_at": None,
        "db_path": None,
        "table": None,
        "row_count": None,
        "event_count": None,
        "warnings": [],
        "errors": [],
        "diagnostics": [],
    }
    if not isinstance(packet, dict):
        return status

    data_quality = packet.get("data_quality") if isinstance(packet.get("data_quality"), dict) else {}
    source_chain = data_quality.get("source_chain") if isinstance(data_quality.get("source_chain"), list) else []
    first_source = next((item for item in source_chain if isinstance(item, dict)), {})
    diagnostics = [item for item in (packet.get("diagnostics") or []) if isinstance(item, dict)]
    snapshot_diag = next(
        (
            item for item in diagnostics
            if str(item.get("source") or "").startswith("fundamental_candidate")
        ),
        {},
    )
    status.update({
        "enabled": True,
        "status": str(packet.get("status") or "unknown"),
        "latest_period": data_quality.get("as_of"),
        "db_path": first_source.get("db_path"),
        "table": first_source.get("table"),
        "row_count": snapshot_diag.get("row_count"),
        "event_count": snapshot_diag.get("event_count"),
        "warnings": data_quality.get("warnings") or packet.get("warnings") or [],
        "errors": packet.get("errors") or [],
        "diagnostics": diagnostics[:5],
    })
    return status


def ensure_candidate_pool_schema(db_path: Optional[str] = None) -> str:
    """Create candidate-pool persistence tables if needed."""
    path = Path(db_path or _default_db_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                run_id TEXT PRIMARY KEY,
                session_id TEXT,
                created_at TEXT NOT NULL,
                market TEXT,
                candidate_source TEXT,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                fallback_used INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                quality_json TEXT,
                hard_exclusion_json TEXT,
                discovery_steps_json TEXT,
                expert_packets_json TEXT,
                themes_json TEXT,
                capacity_json TEXT,
                note TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                market TEXT,
                source TEXT,
                signal_score REAL,
                candidate_experts_json TEXT,
                candidate_dimensions_json TEXT,
                recall_sources_json TEXT,
                reason TEXT,
                reason_dimensions_json TEXT,
                metrics_json TEXT,
                lifecycle_status TEXT,
                valid_until TEXT,
                recurrence_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES {RUNS_TABLE}(run_id) ON DELETE CASCADE,
                UNIQUE(run_id, code)
            )
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{RUNS_TABLE}_created_at ON {RUNS_TABLE}(created_at DESC)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{ITEMS_TABLE}_code_created_at ON {ITEMS_TABLE}(code, created_at DESC)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{ITEMS_TABLE}_run_id ON {ITEMS_TABLE}(run_id)")
        conn.commit()
    return str(path)


class CandidatePoolStore:
    """SQLite-backed candidate-pool run store."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = ensure_candidate_pool_schema(db_path)

    def save_run(self, payload: Dict[str, Any], *, session_id: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
        created_at = _now_iso()
        run_id = run_id or f"cpr-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        candidates = [item for item in payload.get("candidates") or [] if isinstance(item, dict)]
        market = str(payload.get("market") or "cn")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            recurrence_by_code = self._load_recent_code_counts(conn, [str(item.get("code") or "") for item in candidates])
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {RUNS_TABLE} (
                    run_id, session_id, created_at, market, candidate_source, candidate_count,
                    fallback_used, status, quality_json, hard_exclusion_json, discovery_steps_json,
                    expert_packets_json, themes_json, capacity_json, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    created_at,
                    market,
                    str(payload.get("candidate_source") or ""),
                    int(payload.get("candidate_count") or len(candidates)),
                    1 if payload.get("fallback_used") else 0,
                    str(payload.get("status") or ""),
                    _json_dumps(payload.get("quality") or {}),
                    _json_dumps(payload.get("hard_exclusion") or {}),
                    _json_dumps(payload.get("discovery_steps") or []),
                    _json_dumps(payload.get("expert_packets") or []),
                    _json_dumps(payload.get("themes") or []),
                    _json_dumps(payload.get("capacity") or {}),
                    str(payload.get("note") or ""),
                ),
            )
            for item in candidates:
                code = str(item.get("code") or item.get("stock_code") or "").strip()
                if not code:
                    continue
                recurrence_count = int(recurrence_by_code.get(code, 0)) + 1
                lifecycle_status = str(item.get("lifecycle_status") or ("new" if recurrence_count <= 1 else "active"))
                dimensions = _extract_dimensions(item)
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {ITEMS_TABLE} (
                        run_id, code, name, market, source, signal_score, candidate_experts_json,
                        candidate_dimensions_json, recall_sources_json, reason, reason_dimensions_json,
                        metrics_json, lifecycle_status, valid_until, recurrence_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        code,
                        str(item.get("name") or item.get("stock_name") or code),
                        market,
                        str(item.get("source") or _source_family(item)),
                        self._safe_float(item.get("signal_score")),
                        _json_dumps(item.get("candidate_experts") or []),
                        _json_dumps(dimensions),
                        _json_dumps(item.get("recall_sources") or ([item.get("source")] if item.get("source") else [])),
                        _reason_text(item),
                        _json_dumps(item.get("reason_dimensions") or []),
                        _json_dumps(item.get("metrics") or {}),
                        lifecycle_status,
                        str((item.get("expiry") or {}).get("valid_until") or item.get("valid_until") or ""),
                        recurrence_count,
                        created_at,
                    ),
                )
            conn.commit()

        return {"run_id": run_id, "created_at": created_at, "saved_count": len(candidates)}

    def list_runs(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        effective_limit = max(1, min(int(limit or 20), 100))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM {RUNS_TABLE} ORDER BY created_at DESC LIMIT ?",
                (effective_limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_latest(self) -> Optional[Dict[str, Any]]:
        runs = self.list_runs(limit=1)
        if not runs:
            return None
        return self.get_run(str(runs[0]["run_id"]))

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            run_row = conn.execute(f"SELECT * FROM {RUNS_TABLE} WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                return None
            item_rows = conn.execute(
                f"SELECT * FROM {ITEMS_TABLE} WHERE run_id = ? ORDER BY signal_score DESC, id ASC",
                (run_id,),
            ).fetchall()
        run = self._run_from_row(run_row)
        items = [self._item_from_row(row) for row in item_rows]
        return {
            "run": run,
            "items": items,
            "quality": run.get("quality") or {},
            "hard_exclusion": run.get("hard_exclusion") or {},
            "summary": self._summary(run, items),
            "fundamental_status": _fundamental_status_from_run(run, items),
        }

    def _load_recent_code_counts(self, conn: sqlite3.Connection, codes: Iterable[str]) -> Dict[str, int]:
        code_list = sorted({str(code or "").strip() for code in codes if str(code or "").strip()})
        if not code_list:
            return {}
        placeholders = ",".join("?" for _ in code_list)
        rows = conn.execute(
            f"""
            SELECT code, COUNT(DISTINCT run_id) AS cnt
            FROM {ITEMS_TABLE}
            WHERE code IN ({placeholders})
            GROUP BY code
            """,
            code_list,
        ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "created_at": row["created_at"],
            "market": row["market"],
            "candidate_source": row["candidate_source"],
            "candidate_count": int(row["candidate_count"] or 0),
            "fallback_used": bool(row["fallback_used"]),
            "status": row["status"],
            "quality": _json_loads(row["quality_json"], {}),
            "hard_exclusion": _json_loads(row["hard_exclusion_json"], {}),
            "discovery_steps": _json_loads(row["discovery_steps_json"], []),
            "expert_packets": _json_loads(row["expert_packets_json"], []),
            "themes": _json_loads(row["themes_json"], []),
            "capacity": _json_loads(row["capacity_json"], {}),
            "note": row["note"],
        }

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "code": row["code"],
            "name": row["name"],
            "market": row["market"],
            "source": row["source"],
            "signal_score": row["signal_score"],
            "candidate_experts": _json_loads(row["candidate_experts_json"], []),
            "candidate_dimensions": _json_loads(row["candidate_dimensions_json"], []),
            "recall_sources": _json_loads(row["recall_sources_json"], []),
            "reason": row["reason"],
            "reason_dimensions": _json_loads(row["reason_dimensions_json"], []),
            "metrics": _json_loads(row["metrics_json"], {}),
            "lifecycle_status": row["lifecycle_status"],
            "valid_until": row["valid_until"],
            "recurrence_count": int(row["recurrence_count"] or 1),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _summary(run: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
        dimension_counts: Dict[str, int] = {}
        source_counts: Dict[str, int] = {}
        lifecycle_counts: Dict[str, int] = {}
        recurring_count = 0
        multi_source_count = 0
        for item in items:
            family = _source_family(item)
            source_counts[family] = source_counts.get(family, 0) + 1
            lifecycle = str(item.get("lifecycle_status") or "new")
            lifecycle_counts[lifecycle] = lifecycle_counts.get(lifecycle, 0) + 1
            if int(item.get("recurrence_count") or 1) > 1:
                recurring_count += 1
            if len(item.get("recall_sources") or []) > 1:
                multi_source_count += 1
            for dimension in item.get("candidate_dimensions") or ["unknown"]:
                key = str(dimension or "unknown")
                dimension_counts[key] = dimension_counts.get(key, 0) + 1
        hard_exclusion = run.get("hard_exclusion") or {}
        quality = run.get("quality") or {}
        return {
            "candidate_count": len(items),
            "dimension_counts": dimension_counts,
            "source_counts": source_counts,
            "lifecycle_counts": lifecycle_counts,
            "recurring_count": recurring_count,
            "multi_source_count": multi_source_count,
            "fallback_count": source_counts.get("fallback", 0),
            "hard_exclusion_count": int(hard_exclusion.get("excluded_count") or quality.get("hard_exclusion_count") or 0),
            "hard_strategy_trunk_missing": bool(quality.get("hard_strategy_trunk_missing")),
        }
