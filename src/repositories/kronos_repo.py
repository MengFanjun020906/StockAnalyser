# -*- coding: utf-8 -*-
"""Repository helpers for persisted Kronos K-line pattern results."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from src.storage import (
    DatabaseManager,
    KronosForecastPoint,
    KronosModelRun,
    KronosPatternInstance,
    KronosSignal,
)


class KronosRepository:
    """Persist and query Kronos analysis runs."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db_manager = db_manager or DatabaseManager.get_instance()

    def save_analysis_result(self, result: Dict[str, Any]) -> str:
        run_id = str(result.get("run_id") or "")
        if not run_id:
            raise ValueError("Kronos analysis result missing run_id")

        model_profile = result.get("model_profile") or {}
        forecast_summary = result.get("forecast_summary") or {}
        signal = result.get("signal") or {}
        data_quality = result.get("data_quality") or {}
        source_chain = result.get("source_chain") or []

        with self.db_manager.session_scope() as session:
            run = KronosModelRun(
                run_id=run_id,
                code=str(result.get("code") or ""),
                market=str(result.get("market") or "cn"),
                frequency=str(result.get("frequency") or "1d"),
                status=str(result.get("status") or "ok"),
                model_name=str(model_profile.get("model_name") or ""),
                tokenizer_name=str(model_profile.get("tokenizer_name") or ""),
                device=str(model_profile.get("device") or ""),
                max_context=_as_int(model_profile.get("max_context")),
                lookback=_as_int(data_quality.get("bar_count")),
                pred_len=_as_int(forecast_summary.get("horizon_bars")),
                history_source=str(data_quality.get("source") or ""),
                as_of=str(result.get("as_of") or ""),
                data_quality_json=_json(data_quality),
                diagnostics_json=_json(result.get("diagnostics") or {}),
                started_at=_parse_datetime(result.get("created_at")) or datetime.now(),
                finished_at=datetime.now(),
            )
            session.add(run)
            session.add(
                KronosSignal(
                    run_id=run_id,
                    code=run.code,
                    market=run.market,
                    frequency=run.frequency,
                    as_of=run.as_of,
                    bias=str(signal.get("bias") or ""),
                    confidence=_as_float(signal.get("confidence")),
                    expected_return_pct=_as_float(forecast_summary.get("expected_return_pct")),
                    risk_reward=_as_float(forecast_summary.get("risk_reward")),
                    pattern_count=len(result.get("pattern_instances") or []),
                    signal_json=_json(signal),
                    forecast_summary_json=_json(forecast_summary),
                    source_chain_json=_json(source_chain),
                )
            )
            for item in result.get("forecast") or []:
                session.add(
                    KronosForecastPoint(
                        run_id=run_id,
                        code=run.code,
                        horizon_index=_as_int(item.get("index")) or 0,
                        forecast_time=str(item.get("time") or ""),
                        open=_as_float(item.get("open")),
                        high=_as_float(item.get("high")),
                        low=_as_float(item.get("low")),
                        close=_as_float(item.get("close")),
                        volume=_as_float(item.get("volume")),
                        amount=_as_float(item.get("amount")),
                    )
                )
            for item in result.get("pattern_instances") or []:
                session.add(
                    KronosPatternInstance(
                        pattern_id=str(item.get("pattern_id") or ""),
                        run_id=run_id,
                        code=run.code,
                        pattern_type=str(item.get("pattern_type") or ""),
                        label=str(item.get("label") or ""),
                        direction=str(item.get("direction") or ""),
                        confidence=_as_float(item.get("confidence")),
                        start_time=str(item.get("start_time") or ""),
                        end_time=str(item.get("end_time") or ""),
                        price_low=_as_float(item.get("price_low")),
                        price_high=_as_float(item.get("price_high")),
                        red_box_json=_json(item.get("red_box") or {}),
                        summary=str(item.get("summary") or ""),
                        evidence_json=_json(item.get("evidence") or {}),
                    )
                )
        return run_id

    def latest_patterns(self, *, code: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db_manager.get_session() as session:
            stmt = select(KronosPatternInstance).order_by(desc(KronosPatternInstance.created_at)).limit(int(limit))
            if code:
                stmt = stmt.where(KronosPatternInstance.code == code)
            rows = session.execute(stmt).scalars().all()
            return [_pattern_to_dict(row) for row in rows]


def _pattern_to_dict(row: KronosPatternInstance) -> Dict[str, Any]:
    return {
        "pattern_id": row.pattern_id,
        "run_id": row.run_id,
        "code": row.code,
        "pattern_type": row.pattern_type,
        "label": row.label,
        "direction": row.direction,
        "confidence": row.confidence,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "price_low": row.price_low,
        "price_high": row.price_high,
        "red_box": _loads(row.red_box_json),
        "summary": row.summary,
        "evidence": _loads(row.evidence_json),
        "seed_gate_status": row.seed_gate_status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Optional[str]) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
