# -*- coding: utf-8 -*-
"""Repository for Seed Pool quality monitoring."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, delete, desc, func, or_, select

from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import is_meaningful_stock_name
from src.storage import (
    DatabaseManager,
    NewsSignalOutcome,
    NewsSignalSeedLink,
    SelectionSeedPoolDeskOutcome,
    SelectionSeedPoolEvaluation,
    SelectionSeedPoolItem,
    SelectionSeedPoolSnapshot,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _iso_dt(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _pct(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), 4)
    except Exception:
        return None


class SeedPoolQualityRepository:
    """Database access for seed-pool snapshots, outcomes and evaluations."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def save_snapshot(
        self,
        *,
        run_id: str,
        trace_id: str,
        seed_date: date,
        generated_at: Optional[datetime],
        market: str,
        candidate_discovery_mode: str,
        status: str,
        error: str,
        source_summary: Dict[str, Any],
        diagnostics: List[Dict[str, Any]],
        items: List[Dict[str, Any]],
        desk_outcomes_by_code: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Idempotently write one seed-pool snapshot and all item/outcome rows."""

        def _write(session):
            existing = session.execute(
                select(SelectionSeedPoolSnapshot)
                .where(SelectionSeedPoolSnapshot.seed_date == seed_date)
                .order_by(desc(SelectionSeedPoolSnapshot.generated_at), desc(SelectionSeedPoolSnapshot.id))
                .limit(1)
            ).scalar_one_or_none()
            if existing is None:
                snapshot = SelectionSeedPoolSnapshot(
                    run_id=run_id,
                    trace_id=trace_id,
                    seed_date=seed_date,
                )
                session.add(snapshot)
                session.flush()
            else:
                snapshot = existing
                item_id_select = select(SelectionSeedPoolItem.id).where(SelectionSeedPoolItem.snapshot_id == snapshot.id)
                session.execute(
                    delete(NewsSignalOutcome).where(
                        NewsSignalOutcome.seed_item_id.in_(item_id_select)
                    )
                )
                session.execute(
                    delete(NewsSignalSeedLink).where(
                        NewsSignalSeedLink.seed_item_id.in_(item_id_select)
                    )
                )
                session.execute(
                    delete(SelectionSeedPoolEvaluation).where(
                        SelectionSeedPoolEvaluation.item_id.in_(item_id_select)
                    )
                )
                session.execute(
                    delete(SelectionSeedPoolDeskOutcome).where(
                        SelectionSeedPoolDeskOutcome.item_id.in_(item_id_select)
                    )
                )
                session.execute(delete(SelectionSeedPoolItem).where(SelectionSeedPoolItem.snapshot_id == snapshot.id))
                session.flush()

            stale_snapshot_ids = [
                row[0]
                for row in session.execute(
                    select(SelectionSeedPoolSnapshot.id).where(
                        and_(
                            SelectionSeedPoolSnapshot.seed_date == seed_date,
                            SelectionSeedPoolSnapshot.id != snapshot.id,
                        )
                    )
                ).all()
            ]
            if stale_snapshot_ids:
                stale_item_ids = [
                    row[0]
                    for row in session.execute(
                        select(SelectionSeedPoolItem.id).where(
                            SelectionSeedPoolItem.snapshot_id.in_(stale_snapshot_ids)
                        )
                    ).all()
                ]
                if stale_item_ids:
                    session.execute(
                        delete(NewsSignalOutcome).where(
                            NewsSignalOutcome.seed_item_id.in_(stale_item_ids)
                        )
                    )
                    session.execute(
                        delete(NewsSignalSeedLink).where(
                            NewsSignalSeedLink.seed_item_id.in_(stale_item_ids)
                        )
                    )
                    session.execute(
                        delete(SelectionSeedPoolEvaluation).where(
                            SelectionSeedPoolEvaluation.item_id.in_(stale_item_ids)
                        )
                    )
                    session.execute(
                        delete(SelectionSeedPoolDeskOutcome).where(
                            SelectionSeedPoolDeskOutcome.item_id.in_(stale_item_ids)
                        )
                    )
                session.execute(
                    delete(SelectionSeedPoolItem).where(SelectionSeedPoolItem.snapshot_id.in_(stale_snapshot_ids))
                )
                session.execute(delete(SelectionSeedPoolSnapshot).where(SelectionSeedPoolSnapshot.id.in_(stale_snapshot_ids)))
                session.flush()

            snapshot.run_id = run_id
            snapshot.trace_id = trace_id
            snapshot.seed_date = seed_date
            snapshot.generated_at = generated_at or datetime.now()
            snapshot.market = market or "cn"
            snapshot.candidate_discovery_mode = candidate_discovery_mode
            snapshot.seed_count = len(items)
            snapshot.status = status or "ok"
            snapshot.error = error or ""
            snapshot.source_summary_json = _json_dumps(source_summary)
            snapshot.diagnostics_json = _json_dumps(diagnostics)

            saved_items = 0
            seed_link_count = 0
            for idx, item in enumerate(items):
                code = str(item.get("code") or item.get("stock_code") or "").strip()
                if not code:
                    continue
                row = SelectionSeedPoolItem(
                    snapshot_id=snapshot.id,
                    code=code,
                    name=str(item.get("name") or item.get("stock_name") or code),
                    market=str(item.get("market") or market or "cn"),
                    source=str(item.get("source") or item.get("primary_source") or "unknown"),
                    source_diagnostics_json=_json_dumps(item.get("source_diagnostics") or item.get("diagnostics") or {}),
                    trigger_signals_json=_json_dumps(item.get("trigger_signals") or item.get("signals") or []),
                    catalyst_tags_json=_json_dumps(item.get("catalyst_tags") or _extract_catalyst_tags(item)),
                    catalyst_tier=_coerce_int(item.get("catalyst_tier"), default=0),
                    entry_reason=str(item.get("entry_reason") or item.get("reason") or item.get("hint") or ""),
                    freshness=str(item.get("freshness") or item.get("as_of") or ""),
                    seed_order=_coerce_int(item.get("seed_order"), default=idx),
                    entered_deep_dive=bool(item.get("entered_deep_dive")),
                    entered_final_report=bool(item.get("entered_final_report")),
                    raw_payload_json=_json_dumps(item),
                )
                session.add(row)
                session.flush()
                saved_items += 1
                for link in _extract_news_signal_seed_refs(item):
                    session.add(
                        NewsSignalSeedLink(
                            card_id=link["card_id"],
                            seed_item_id=row.id,
                            source_desk=str(item.get("source") or item.get("primary_source") or "unknown"),
                            gate_result=link["gate_result"],
                            signal_score_snapshot=link["signal_score_snapshot"],
                            mapping_confidence=link["mapping_confidence"],
                            evidence_grade=link["evidence_grade"],
                        )
                    )
                    seed_link_count += 1
                for outcome in desk_outcomes_by_code.get(_normalize_code(code), []):
                    session.add(
                        SelectionSeedPoolDeskOutcome(
                            item_id=row.id,
                            desk=str(outcome.get("desk") or outcome.get("expert") or "unknown"),
                            status=str(outcome.get("status") or "missing"),
                            stance=str(outcome.get("stance") or "missing"),
                            decision=str(outcome.get("decision") or "not_evaluated"),
                            reason=str(outcome.get("reason") or ""),
                            risks_json=_json_dumps(outcome.get("risks") or []),
                            evidence_json=_json_dumps(outcome.get("evidence") or []),
                            metrics_json=_json_dumps(outcome.get("metrics") or {}),
                            errors_json=_json_dumps(outcome.get("errors") or []),
                            elapsed_ms=_coerce_int(outcome.get("elapsed_ms"), default=None),
                        )
                    )
            return {
                "snapshot_id": snapshot.id,
                "item_count": saved_items,
                "seed_link_count": seed_link_count,
            }

        return self.db._run_write_transaction("seed_pool_quality.save_snapshot", _write)

    def list_dates(self, *, limit: int = 60) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(
                    SelectionSeedPoolSnapshot.seed_date,
                    func.count(SelectionSeedPoolSnapshot.id),
                    func.max(SelectionSeedPoolSnapshot.generated_at),
                )
                .group_by(SelectionSeedPoolSnapshot.seed_date)
                .order_by(desc(SelectionSeedPoolSnapshot.seed_date))
                .limit(max(1, min(int(limit or 60), 200)))
            ).all()
        return [
            {"seed_date": row[0].isoformat(), "snapshot_count": int(row[1] or 0), "latest_generated_at": _iso_dt(row[2])}
            for row in rows
        ]

    def get_quality_by_date(self, seed_date: date) -> Dict[str, Any]:
        snapshot = self._latest_snapshot_for_date(seed_date)
        if snapshot is None:
            return {"snapshot": None, "summary": {}, "source_stats": [], "desk_stats": [], "catalyst_tier_stats": [], "items": []}
        return self.get_snapshot(snapshot.id)

    def get_snapshot(self, snapshot_id: int) -> Dict[str, Any]:
        with self.db.get_session() as session:
            snapshot = session.execute(
                select(SelectionSeedPoolSnapshot).where(SelectionSeedPoolSnapshot.id == snapshot_id)
            ).scalar_one_or_none()
            if snapshot is None:
                return {}
            items = session.execute(
                select(SelectionSeedPoolItem)
                .where(SelectionSeedPoolItem.snapshot_id == snapshot.id)
                .order_by(SelectionSeedPoolItem.seed_order, SelectionSeedPoolItem.id)
            ).scalars().all()
            item_ids = [item.id for item in items]
            evaluations = {}
            outcomes_by_item: Dict[int, List[SelectionSeedPoolDeskOutcome]] = {}
            if item_ids:
                eval_rows = session.execute(
                    select(SelectionSeedPoolEvaluation).where(SelectionSeedPoolEvaluation.item_id.in_(item_ids))
                ).scalars().all()
                evaluations = {row.item_id: row for row in eval_rows}
                outcome_rows = session.execute(
                    select(SelectionSeedPoolDeskOutcome).where(SelectionSeedPoolDeskOutcome.item_id.in_(item_ids))
                ).scalars().all()
                for row in outcome_rows:
                    outcomes_by_item.setdefault(row.item_id, []).append(row)

        item_dicts = [
            self._item_to_dict(item, evaluations.get(item.id), outcomes_by_item.get(item.id, []))
            for item in items
        ]
        return {
            "snapshot": self._snapshot_to_dict(snapshot),
            "summary": _summary(item_dicts),
            "source_stats": _group_stats(item_dicts, "source"),
            "desk_stats": _desk_stats(item_dicts),
            "catalyst_tier_stats": _group_stats(item_dicts, "catalyst_tier"),
            "items": item_dicts,
        }

    def get_item_detail(self, item_id: int) -> Dict[str, Any]:
        with self.db.get_session() as session:
            item = session.execute(select(SelectionSeedPoolItem).where(SelectionSeedPoolItem.id == item_id)).scalar_one_or_none()
            if item is None:
                return {}
            evaluation = session.execute(
                select(SelectionSeedPoolEvaluation).where(SelectionSeedPoolEvaluation.item_id == item.id)
            ).scalar_one_or_none()
            outcomes = session.execute(
                select(SelectionSeedPoolDeskOutcome).where(SelectionSeedPoolDeskOutcome.item_id == item.id)
            ).scalars().all()
        return self._item_to_dict(item, evaluation, outcomes)

    def list_pending_items(self, *, seed_date: Optional[date] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = (
                select(SelectionSeedPoolItem, SelectionSeedPoolSnapshot)
                .join(SelectionSeedPoolSnapshot, SelectionSeedPoolItem.snapshot_id == SelectionSeedPoolSnapshot.id)
                .outerjoin(SelectionSeedPoolEvaluation, SelectionSeedPoolEvaluation.item_id == SelectionSeedPoolItem.id)
                .where(SelectionSeedPoolEvaluation.id.is_(None))
                .order_by(SelectionSeedPoolSnapshot.seed_date.desc(), SelectionSeedPoolItem.seed_order)
                .limit(max(1, min(int(limit or 200), 1000)))
            )
            if seed_date is not None:
                stmt = stmt.where(SelectionSeedPoolSnapshot.seed_date == seed_date)
            rows = session.execute(stmt).all()
        return [
            {
                "item_id": item.id,
                "snapshot_id": item.snapshot_id,
                "code": item.code,
                "name": item.name,
                "market": item.market,
                "seed_date": snapshot.seed_date,
            }
            for item, snapshot in rows
        ]

    def list_items_requiring_evaluation(self, *, seed_date: date, limit: int = 200, include_ok: bool = False) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            latest_snapshot = session.execute(
                select(SelectionSeedPoolSnapshot.id)
                .where(SelectionSeedPoolSnapshot.seed_date == seed_date)
                .order_by(desc(SelectionSeedPoolSnapshot.generated_at), desc(SelectionSeedPoolSnapshot.id))
                .limit(1)
            ).scalar_one_or_none()
            if latest_snapshot is None:
                return []
            stmt = (
                select(SelectionSeedPoolItem, SelectionSeedPoolSnapshot)
                .join(SelectionSeedPoolSnapshot, SelectionSeedPoolItem.snapshot_id == SelectionSeedPoolSnapshot.id)
                .outerjoin(SelectionSeedPoolEvaluation, SelectionSeedPoolEvaluation.item_id == SelectionSeedPoolItem.id)
                .where(SelectionSeedPoolSnapshot.seed_date == seed_date)
                .where(SelectionSeedPoolSnapshot.id == latest_snapshot)
                .order_by(SelectionSeedPoolSnapshot.generated_at.desc(), SelectionSeedPoolItem.seed_order)
                .limit(max(1, min(int(limit or 200), 1000)))
            )
            if not include_ok:
                stmt = stmt.where(or_(SelectionSeedPoolEvaluation.id.is_(None), SelectionSeedPoolEvaluation.data_status != "ok"))
            rows = session.execute(stmt).all()
        return [
            {
                "item_id": item.id,
                "snapshot_id": item.snapshot_id,
                "code": item.code,
                "name": item.name,
                "market": item.market,
                "seed_date": snapshot.seed_date,
            }
            for item, snapshot in rows
        ]

    def upsert_evaluation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        def _write(session):
            item_id = int(payload["item_id"])
            evaluation_date = payload["evaluation_date"]
            row = session.execute(
                select(SelectionSeedPoolEvaluation).where(
                    and_(
                        SelectionSeedPoolEvaluation.item_id == item_id,
                        SelectionSeedPoolEvaluation.evaluation_date == evaluation_date,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = SelectionSeedPoolEvaluation(item_id=item_id, evaluation_date=evaluation_date)
                session.add(row)
                session.flush()
            for key in (
                "seed_close",
                "evaluation_open",
                "evaluation_high",
                "evaluation_low",
                "evaluation_close",
                "next_close_return_pct",
                "benchmark_return_pct",
                "alpha_return_pct",
                "mfe_pct",
                "mae_pct",
            ):
                setattr(row, key, payload.get(key))
            row.benchmark_code = str(payload.get("benchmark_code") or "000001.SH")
            row.liquidity_status = str(payload.get("liquidity_status") or "UNKNOWN")
            row.data_status = str(payload.get("data_status") or "ok")
            row.error = str(payload.get("error") or "")
            row.updated_at = datetime.now()
            return {"evaluation_id": row.id, "item_id": item_id, "data_status": row.data_status}

        return self.db._run_write_transaction("seed_pool_quality.upsert_evaluation", _write)

    def get_item_with_snapshot(self, item_id: int) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = session.execute(
                select(SelectionSeedPoolItem, SelectionSeedPoolSnapshot)
                .join(SelectionSeedPoolSnapshot, SelectionSeedPoolItem.snapshot_id == SelectionSeedPoolSnapshot.id)
                .where(SelectionSeedPoolItem.id == item_id)
            ).one_or_none()
        if row is None:
            return {}
        item, snapshot = row
        return {
            "item_id": item.id,
            "snapshot_id": snapshot.id,
            "code": item.code,
            "name": item.name,
            "market": item.market,
            "seed_date": snapshot.seed_date,
            "snapshot": self._snapshot_to_dict(snapshot),
        }

    def _latest_snapshot_for_date(self, seed_date: date) -> Optional[SelectionSeedPoolSnapshot]:
        with self.db.get_session() as session:
            return session.execute(
                select(SelectionSeedPoolSnapshot)
                .where(SelectionSeedPoolSnapshot.seed_date == seed_date)
                .order_by(desc(SelectionSeedPoolSnapshot.generated_at), desc(SelectionSeedPoolSnapshot.id))
                .limit(1)
            ).scalar_one_or_none()

    @staticmethod
    def _snapshot_to_dict(row: SelectionSeedPoolSnapshot) -> Dict[str, Any]:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "trace_id": row.trace_id,
            "seed_date": row.seed_date.isoformat() if row.seed_date else None,
            "generated_at": _iso_dt(row.generated_at),
            "market": row.market,
            "candidate_discovery_mode": row.candidate_discovery_mode,
            "seed_count": row.seed_count,
            "status": row.status,
            "error": row.error,
            "source_summary": _json_loads(row.source_summary_json, {}),
            "diagnostics": _json_loads(row.diagnostics_json, []),
            "created_at": _iso_dt(row.created_at),
        }

    @staticmethod
    def _item_to_dict(
        item: SelectionSeedPoolItem,
        evaluation: Optional[SelectionSeedPoolEvaluation],
        outcomes: Iterable[SelectionSeedPoolDeskOutcome],
    ) -> Dict[str, Any]:
        display_name = _display_stock_name(item.code, item.name)
        return {
            "id": item.id,
            "snapshot_id": item.snapshot_id,
            "code": item.code,
            "name": display_name,
            "market": item.market,
            "source": item.source,
            "source_diagnostics": _json_loads(item.source_diagnostics_json, {}),
            "trigger_signals": _json_loads(item.trigger_signals_json, []),
            "catalyst_tags": _json_loads(item.catalyst_tags_json, []),
            "catalyst_tier": int(item.catalyst_tier or 0),
            "entry_reason": item.entry_reason,
            "freshness": item.freshness,
            "seed_order": item.seed_order,
            "entered_deep_dive": bool(item.entered_deep_dive),
            "entered_final_report": bool(item.entered_final_report),
            "evaluation": _evaluation_to_dict(evaluation),
            "desk_outcomes": [_outcome_to_dict(row) for row in outcomes],
        }


def _evaluation_to_dict(row: Optional[SelectionSeedPoolEvaluation]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": row.id,
        "item_id": row.item_id,
        "evaluation_date": row.evaluation_date.isoformat() if row.evaluation_date else None,
        "seed_close": _pct(row.seed_close),
        "evaluation_open": _pct(row.evaluation_open),
        "evaluation_high": _pct(row.evaluation_high),
        "evaluation_low": _pct(row.evaluation_low),
        "evaluation_close": _pct(row.evaluation_close),
        "next_close_return_pct": _pct(row.next_close_return_pct),
        "benchmark_code": row.benchmark_code,
        "benchmark_return_pct": _pct(row.benchmark_return_pct),
        "alpha_return_pct": _pct(row.alpha_return_pct),
        "mfe_pct": _pct(row.mfe_pct),
        "mae_pct": _pct(row.mae_pct),
        "liquidity_status": row.liquidity_status,
        "data_status": row.data_status,
        "error": row.error,
        "updated_at": _iso_dt(row.updated_at),
    }


def _outcome_to_dict(row: SelectionSeedPoolDeskOutcome) -> Dict[str, Any]:
    return {
        "id": row.id,
        "desk": row.desk,
        "status": row.status,
        "stance": row.stance,
        "decision": row.decision,
        "reason": row.reason,
        "risks": _json_loads(row.risks_json, []),
        "evidence": _json_loads(row.evidence_json, []),
        "metrics": _json_loads(row.metrics_json, {}),
        "errors": _json_loads(row.errors_json, []),
        "elapsed_ms": row.elapsed_ms,
    }


def _display_stock_name(code: str, stored_name: Optional[str]) -> str:
    if is_meaningful_stock_name(stored_name, code):
        return str(stored_name).strip()
    index_name = get_index_stock_name(code)
    if is_meaningful_stock_name(index_name, code):
        return str(index_name).strip()
    return str(code or "").strip()


def _summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    eval_items = [item for item in items if isinstance(item.get("evaluation"), dict)]
    tradable = [
        item for item in eval_items
        if item["evaluation"].get("data_status") == "ok"
        and item["evaluation"].get("liquidity_status") != "LIMIT_UP_UNABLE_BUY"
    ]
    return {
        "seed_count": len(items),
        "evaluated_count": len(eval_items),
        "tradable_count": len(tradable),
        "limit_up_unable_buy_count": sum(1 for item in eval_items if item["evaluation"].get("liquidity_status") == "LIMIT_UP_UNABLE_BUY"),
        "missing_price_count": sum(1 for item in items if not item.get("evaluation") or item["evaluation"].get("data_status") != "ok"),
        "up_count": sum(1 for item in tradable if _num(item["evaluation"].get("next_close_return_pct")) > 0),
        "down_count": sum(1 for item in tradable if _num(item["evaluation"].get("next_close_return_pct")) < 0),
        "win_rate_pct": _rate(sum(1 for item in tradable if _num(item["evaluation"].get("next_close_return_pct")) > 0), len(tradable)),
        "avg_return_pct": _avg(item["evaluation"].get("next_close_return_pct") for item in tradable),
        "median_return_pct": _median(item["evaluation"].get("next_close_return_pct") for item in tradable),
        "avg_alpha_return_pct": _avg(item["evaluation"].get("alpha_return_pct") for item in tradable),
        "median_alpha_return_pct": _median(item["evaluation"].get("alpha_return_pct") for item in tradable),
        "avg_mfe_pct": _avg(item["evaluation"].get("mfe_pct") for item in tradable),
        "avg_mae_pct": _avg(item["evaluation"].get("mae_pct") for item in tradable),
    }


def _group_stats(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        group_key = str(item.get(key) if item.get(key) not in (None, "") else "unknown")
        groups.setdefault(group_key, []).append(item)
    return [
        {"key": name, **_summary(rows)}
        for name, rows in sorted(groups.items(), key=lambda pair: pair[0])
    ]


def _desk_stats(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_desk: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        for outcome in item.get("desk_outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            row = dict(item)
            row["desk_outcome"] = outcome
            by_desk.setdefault(str(outcome.get("desk") or "unknown"), []).append(row)
    stats: List[Dict[str, Any]] = []
    for desk, rows in sorted(by_desk.items(), key=lambda pair: pair[0]):
        base = _summary(rows)
        base.update({
            "key": desk,
            "support_watch_count": sum(1 for row in rows if str(row.get("desk_outcome", {}).get("stance")) in {"support", "watch"}),
            "oppose_invalid_count": sum(1 for row in rows if str(row.get("desk_outcome", {}).get("stance")) in {"oppose", "invalid"}),
        })
        stats.append(base)
    return stats


def _extract_catalyst_tags(item: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    for value in item.get("trigger_signals") or item.get("signals") or []:
        if not isinstance(value, dict):
            continue
        for key in ("label", "signal_type", "theme", "event", "summary"):
            text = str(value.get(key) or "").strip()
            if text and text not in tags:
                tags.append(text)
    return tags[:8]


def _extract_news_signal_seed_refs(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for signal in item.get("trigger_signals") or item.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        if str(signal.get("signal_type") or "").strip() != "news_signal_card":
            continue
        value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
        card_id = str(value.get("card_id") or signal.get("card_id") or "").strip()
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        refs.append(
            {
                "card_id": card_id,
                "gate_result": str(value.get("gate_result") or "matched_existing_seed"),
                "signal_score_snapshot": _pct(value.get("signal_score")),
                "mapping_confidence": _pct(value.get("mapping_confidence")),
                "evidence_grade": str(value.get("evidence_grade") or ""),
            }
        )
    return refs


def _coerce_int(value: Any, *, default: Optional[int] = 0) -> Optional[int]:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _num(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _numbers(values: Iterable[Any]) -> List[float]:
    nums: List[float] = []
    for value in values:
        try:
            num = float(value)
        except Exception:
            continue
        nums.append(num)
    return nums


def _avg(values: Iterable[Any]) -> Optional[float]:
    nums = _numbers(values)
    if not nums:
        return None
    return round(sum(nums) / len(nums), 4)


def _median(values: Iterable[Any]) -> Optional[float]:
    nums = sorted(_numbers(values))
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return round(nums[mid], 4)
    return round((nums[mid - 1] + nums[mid]) / 2, 4)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 4)
