# -*- coding: utf-8 -*-
"""Repository for persistent news signal cards."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, desc, func, or_, select

from src.storage import (
    DatabaseManager,
    NewsExtractedEvent,
    NewsSignalCard,
    NewsSignalFeedback,
    NewsSignalEdge,
    NewsSignalOutcome,
    NewsSignalSeedLink,
    RawNewsEpisode,
    SelectionSeedPoolEvaluation,
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


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    return None


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text[: len(fmt)], fmt)
                except ValueError:
                    continue
    return None


def _json_list(value: Any) -> List[Any]:
    loaded = _json_loads(value, [])
    return loaded if isinstance(loaded, list) else []


def _json_dict(value: Any) -> Dict[str, Any]:
    loaded = _json_loads(value, {})
    return loaded if isinstance(loaded, dict) else {}


def _unique_values(values: Iterable[Any]) -> List[Any]:
    result: List[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _unique_dicts(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [item for item in _unique_values(values) if isinstance(item, dict)]


class NewsSignalRepository:
    """Database access for raw news episodes, signal cards and overlays."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def existing_raw_episode_ids(self, episode_ids: Iterable[str]) -> set[str]:
        keys = {str(value or "").strip() for value in episode_ids if str(value or "").strip()}
        if not keys:
            return set()
        with self.db.get_session() as session:
            rows = session.execute(
                select(RawNewsEpisode.episode_id).where(RawNewsEpisode.episode_id.in_(keys))
            ).scalars().all()
            return {str(value) for value in rows}

    def latest_raw_episode_cursor(self, source: str) -> Dict[str, Any]:
        source_key = str(source or "").strip()
        if not source_key:
            return {"published_at": None, "episode_id": None}
        with self.db.get_session() as session:
            row = session.execute(
                select(RawNewsEpisode)
                .where(RawNewsEpisode.source == source_key)
                .order_by(desc(RawNewsEpisode.published_at), desc(RawNewsEpisode.id))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return {"published_at": None, "episode_id": None}
            return {
                "published_at": _iso(row.published_at),
                "episode_id": row.episode_id,
            }

    def upsert_raw_episodes(self, episodes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payloads = [item for item in episodes if item.get("episode_id") and item.get("dedup_key")]
        if not payloads:
            return []

        def _write(session):
            rows: List[RawNewsEpisode] = []
            rows_by_episode_id: Dict[str, RawNewsEpisode] = {}
            rows_by_dedup_key: Dict[str, RawNewsEpisode] = {}
            for item in payloads:
                episode_id = str(item["episode_id"])
                dedup_key = str(item["dedup_key"])
                row = rows_by_episode_id.get(episode_id) or rows_by_dedup_key.get(dedup_key)
                if row is None:
                    matches = session.execute(
                        select(RawNewsEpisode).where(
                            or_(
                                RawNewsEpisode.episode_id == episode_id,
                                RawNewsEpisode.dedup_key == dedup_key,
                            )
                        )
                    ).scalars().all()
                    row = next((match for match in matches if match.episode_id == episode_id), None)
                    if row is None:
                        row = matches[0] if matches else None
                    if row is None:
                        row = RawNewsEpisode(episode_id=episode_id, dedup_key=dedup_key)
                        session.add(row)
                    else:
                        self._refresh_raw_episode_identity(
                            row,
                            episode_id=episode_id,
                            dedup_key=dedup_key,
                            matches=matches,
                        )
                self._assign_raw_episode(row, item)
                rows_by_episode_id[str(row.episode_id)] = row
                rows_by_dedup_key[str(row.dedup_key)] = row
                rows.append(row)
            session.flush()
            return [self.raw_episode_to_dict(row) for row in rows]

        return self.db._run_write_transaction("news_signal.upsert_raw_episodes", _write)

    def upsert_extracted_events(self, events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payloads = [item for item in events if item.get("event_id") and item.get("raw_episode_id")]
        if not payloads:
            return []

        def _write(session):
            rows: List[NewsExtractedEvent] = []
            for item in payloads:
                event_id = str(item["event_id"])
                row = session.execute(
                    select(NewsExtractedEvent).where(NewsExtractedEvent.event_id == event_id)
                ).scalar_one_or_none()
                if row is None:
                    row = session.execute(
                        select(NewsExtractedEvent).where(
                            (NewsExtractedEvent.raw_episode_id == str(item.get("raw_episode_id") or ""))
                            & (NewsExtractedEvent.event_type == str(item.get("event_type") or "unknown"))
                            & (NewsExtractedEvent.trigger == str(item.get("trigger") or ""))
                            & (NewsExtractedEvent.evidence_sentence == str(item.get("evidence_sentence") or ""))
                        )
                    ).scalar_one_or_none()
                if row is None:
                    row = NewsExtractedEvent(event_id=event_id)
                    session.add(row)
                self._assign_extracted_event(row, item)
                rows.append(row)
            session.flush()
            return [self.extracted_event_to_dict(row) for row in rows]

        return self.db._run_write_transaction("news_signal.upsert_extracted_events", _write)

    def upsert_cards(self, cards: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payloads = [item for item in cards if item.get("card_id")]
        if not payloads:
            return []

        def _write(session):
            rows: List[NewsSignalCard] = []
            for item in payloads:
                row = session.execute(
                    select(NewsSignalCard).where(NewsSignalCard.card_id == str(item["card_id"]))
                ).scalar_one_or_none()
                if row is None:
                    row = NewsSignalCard(card_id=str(item["card_id"]))
                    session.add(row)
                self._assign_card(row, item)
                rows.append(row)
            session.flush()
            return [self.card_to_dict(row) for row in rows]

        return self.db._run_write_transaction("news_signal.upsert_cards", _write)

    def list_cards(
        self,
        *,
        signal_date: Optional[Any] = None,
        signal_layer: str = "",
        industry: str = "",
        horizon: str = "",
        status: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        target_date = _coerce_date(signal_date)
        layer_text = str(signal_layer or "").strip()
        industry_text = str(industry or "").strip()
        horizon_text = str(horizon or "").strip()
        status_text = str(status or "").strip()
        capped_limit = max(1, min(int(limit or 100), 500))

        with self.db.get_session() as session:
            stmt = select(NewsSignalCard)
            if target_date:
                stmt = stmt.where(NewsSignalCard.signal_date == target_date)
            if layer_text:
                stmt = stmt.where(NewsSignalCard.signal_layer == layer_text)
            if status_text:
                stmt = stmt.where(NewsSignalCard.status == status_text)
            if horizon_text:
                stmt = stmt.where(NewsSignalCard.impact_horizon == horizon_text)
            if industry_text:
                like = f"%{industry_text}%"
                stmt = stmt.where(
                    (NewsSignalCard.primary_industries_json.like(like))
                    | (NewsSignalCard.secondary_industries_json.like(like))
                )
            rows = session.execute(
                stmt.order_by(desc(NewsSignalCard.signal_date), desc(NewsSignalCard.signal_score), desc(NewsSignalCard.updated_at))
                .limit(capped_limit)
            ).scalars().all()
            feedback_counts = self._feedback_counts(session, [row.card_id for row in rows])

        items = []
        for row in rows:
            item = self.card_to_dict(row)
            counts = feedback_counts.get(row.card_id, {})
            item["feedback_counts"] = counts
            item["adjusted_signal_score"] = _adjusted_score(item.get("signal_score"), counts)
            items.append(item)
        return sorted(
            items,
            key=lambda item: (
                str(item.get("signal_date") or ""),
                float(item.get("adjusted_signal_score") or 0.0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        key = str(card_id or "").strip()
        if not key:
            return None
        with self.db.get_session() as session:
            row = session.execute(select(NewsSignalCard).where(NewsSignalCard.card_id == key)).scalar_one_or_none()
            if row is None:
                return None
            item = self.card_to_dict(row)
            item["feedback_counts"] = self._feedback_counts(session, [key]).get(key, {})
            item["raw_episodes"] = self.raw_episodes_for_card(session, item)
            item["extracted_events"] = self.extracted_events_for_card(session, item)
            seed_links = session.execute(
                select(NewsSignalSeedLink)
                .where(NewsSignalSeedLink.card_id == key)
                .order_by(desc(NewsSignalSeedLink.created_at), desc(NewsSignalSeedLink.id))
            ).scalars().all()
            item["seed_links"] = [self.seed_link_to_dict(link) for link in seed_links]
            return item

    def add_feedback(
        self,
        *,
        card_id: str,
        feedback_type: str,
        note: str = "",
        payload: Optional[Dict[str, Any]] = None,
        user_id: str = "",
    ) -> Dict[str, Any]:
        allowed = {"useful", "wrong", "noisy", "duplicate", "adjust_industries", "remove_company", "note"}
        feedback = str(feedback_type or "").strip()
        if feedback not in allowed:
            raise ValueError(f"unsupported feedback_type: {feedback_type}")
        key = str(card_id or "").strip()
        if not key:
            raise ValueError("card_id is required")

        def _write(session):
            card = session.execute(select(NewsSignalCard).where(NewsSignalCard.card_id == key)).scalar_one_or_none()
            if card is None:
                raise ValueError(f"news signal card not found: {key}")
            row = NewsSignalFeedback(
                card_id=key,
                feedback_type=feedback,
                note=str(note or ""),
                payload_json=_json_dumps(payload or {}),
                user_id=str(user_id or ""),
            )
            session.add(row)
            if feedback in {"wrong", "noisy"} and card.status == "active":
                card.status = "suppressed"
            card.updated_at = datetime.now()
            session.flush()
            return self.feedback_to_dict(row)

        return self.db._run_write_transaction("news_signal.add_feedback", _write)

    def save_seed_link(self, link: Dict[str, Any]) -> Dict[str, Any]:
        card_id = str(link.get("card_id") or "").strip()
        if not card_id:
            raise ValueError("card_id is required")
        seed_item_id = link.get("seed_item_id")
        source_desk = str(link.get("source_desk") or "").strip() or "unknown"

        def _write(session):
            row = session.execute(
                select(NewsSignalSeedLink).where(
                    NewsSignalSeedLink.card_id == card_id,
                    NewsSignalSeedLink.seed_item_id == seed_item_id,
                    NewsSignalSeedLink.source_desk == source_desk,
                )
            ).scalar_one_or_none()
            if row is None:
                row = NewsSignalSeedLink(card_id=card_id, seed_item_id=seed_item_id, source_desk=source_desk)
                session.add(row)
            row.gate_result = str(link.get("gate_result") or "unknown")
            row.signal_score_snapshot = _float_or_none(link.get("signal_score_snapshot"))
            row.mapping_confidence = _float_or_none(link.get("mapping_confidence"))
            row.evidence_grade = str(link.get("evidence_grade") or "")
            session.flush()
            return self.seed_link_to_dict(row)

        return self.db._run_write_transaction("news_signal.save_seed_link", _write)

    def refresh_outcomes_from_seed_evaluations(self) -> Dict[str, Any]:
        """Refresh news signal outcome projection from existing seed-pool evaluations."""

        def _write(session):
            rows = session.execute(
                select(NewsSignalSeedLink, SelectionSeedPoolEvaluation)
                .join(SelectionSeedPoolEvaluation, NewsSignalSeedLink.seed_item_id == SelectionSeedPoolEvaluation.item_id)
            ).all()
            updated = 0
            for link, evaluation in rows:
                if not link.card_id or not evaluation.evaluation_date:
                    continue
                existing = session.execute(
                    select(NewsSignalOutcome).where(
                        NewsSignalOutcome.card_id == link.card_id,
                        NewsSignalOutcome.seed_item_id == link.seed_item_id,
                        NewsSignalOutcome.evaluation_date == evaluation.evaluation_date,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = NewsSignalOutcome(
                        card_id=link.card_id,
                        seed_item_id=link.seed_item_id,
                        evaluation_date=evaluation.evaluation_date,
                    )
                    session.add(existing)
                existing.alpha_return_pct = evaluation.alpha_return_pct
                existing.mfe_pct = evaluation.mfe_pct
                existing.mae_pct = evaluation.mae_pct
                existing.liquidity_status = evaluation.liquidity_status
                existing.data_status = evaluation.data_status
                existing.updated_at = datetime.now()
                updated += 1
            return {"linked_evaluations": len(rows), "outcomes_upserted": updated}

        return self.db._run_write_transaction("news_signal.refresh_outcomes", _write)

    def metrics(self, *, signal_date: Optional[Any] = None) -> Dict[str, Any]:
        target_date = _coerce_date(signal_date)
        with self.db.get_session() as session:
            base = select(NewsSignalCard)
            if target_date:
                base = base.where(NewsSignalCard.signal_date == target_date)
            rows = session.execute(base).scalars().all()
            feedback_rows = session.execute(
                select(NewsSignalFeedback.feedback_type, func.count(NewsSignalFeedback.id)).group_by(NewsSignalFeedback.feedback_type)
            ).all()
        return {
            "total_cards": len(rows),
            "active_cards": sum(1 for row in rows if row.status == "active"),
            "suppressed_cards": sum(1 for row in rows if row.status == "suppressed"),
            "avg_signal_score": round(sum(float(row.signal_score or 0.0) for row in rows) / len(rows), 4) if rows else None,
            "layer_counts": _count_by(rows, "signal_layer"),
            "mapping_counts": _count_by(rows, "mapping_status"),
            "horizon_counts": _count_by(rows, "impact_horizon"),
            "graph_sync_counts": _count_by(rows, "graph_sync_status"),
            "feedback_counts": {str(row[0] or "unknown"): int(row[1] or 0) for row in feedback_rows},
        }

    def list_graph_sync_candidates(
        self,
        *,
        signal_date: Optional[Any] = None,
        limit: int = 100,
        retry_limit: int = 3,
    ) -> List[Dict[str, Any]]:
        target_date = _coerce_date(signal_date)
        capped_limit = max(1, min(int(limit or 100), 500))
        max_retries = max(0, int(retry_limit or 0))
        with self.db.get_session() as session:
            stmt = (
                select(NewsSignalCard)
                .where(NewsSignalCard.status == "active")
                .where(NewsSignalCard.graph_sync_status.in_(["pending", "failed"]))
                .where(NewsSignalCard.graph_retry_count <= max_retries)
            )
            if target_date:
                stmt = stmt.where(NewsSignalCard.signal_date == target_date)
            rows = session.execute(
                stmt.order_by(desc(NewsSignalCard.signal_date), desc(NewsSignalCard.signal_score), desc(NewsSignalCard.updated_at))
                .limit(capped_limit)
            ).scalars().all()
            cards = []
            for row in rows:
                item = self.card_to_dict(row)
                item["raw_episodes"] = self.raw_episodes_for_card(session, item)
                cards.append(item)
            return cards

    def update_graph_sync_status(self, card_id: str, *, status: str, error: str = "") -> Optional[Dict[str, Any]]:
        key = str(card_id or "").strip()
        if not key:
            return None
        normalized_status = str(status or "").strip() or "pending"

        def _write(session):
            row = session.execute(select(NewsSignalCard).where(NewsSignalCard.card_id == key)).scalar_one_or_none()
            if row is None:
                return None
            row.graph_sync_status = normalized_status
            if normalized_status == "synced":
                row.graph_last_error = ""
            elif normalized_status == "failed":
                row.graph_retry_count = int(row.graph_retry_count or 0) + 1
                row.graph_last_error = str(error or "")[:2000]
            elif error:
                row.graph_last_error = str(error)[:2000]
            row.updated_at = datetime.now()
            session.flush()
            return self.card_to_dict(row)

        return self.db._run_write_transaction("news_signal.update_graph_sync_status", _write)

    def update_event_projection(
        self,
        card_id: str,
        *,
        transmission_paths: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        key = str(card_id or "").strip()
        if not key:
            return None

        def _write(session):
            row = session.execute(select(NewsSignalCard).where(NewsSignalCard.card_id == key)).scalar_one_or_none()
            if row is None:
                return None
            row.transmission_paths_json = _json_dumps(transmission_paths)
            row.diagnostics_json = _json_dumps(diagnostics)
            row.updated_at = datetime.now()
            session.flush()
            return self.card_to_dict(row)

        return self.db._run_write_transaction("news_signal.update_event_projection", _write)

    def update_company_mapping_projection(
        self,
        card_id: str,
        *,
        company_impacts: List[Dict[str, Any]],
        mapping_status: str,
        mapping_confidence: float,
        signal_layer: str,
        signal_score: float,
        evidence_grade: str,
        status: str,
        transmission_paths: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        key = str(card_id or "").strip()
        if not key:
            return None
        allowed_symbols = {
            str(item.get("symbol") or item.get("code") or "").strip().upper()
            for item in company_impacts
            if isinstance(item, dict) and str(item.get("symbol") or item.get("code") or "").strip()
        }
        allowed_names = {
            str(item.get("name") or "").strip()
            for item in company_impacts
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }

        def _write(session):
            row = session.execute(select(NewsSignalCard).where(NewsSignalCard.card_id == key)).scalar_one_or_none()
            if row is None:
                return None
            row.company_impacts_json = _json_dumps(company_impacts)
            row.mapping_status = str(mapping_status or "industry_only")
            row.mapping_confidence = float(mapping_confidence or 0.0)
            row.signal_layer = str(signal_layer or "industry")
            row.signal_score = float(signal_score or 0.0)
            row.evidence_grade = str(evidence_grade or "plausible")
            row.status = str(status or row.status or "active")
            row.transmission_paths_json = _json_dumps(transmission_paths)
            row.diagnostics_json = _json_dumps(diagnostics)
            row.graph_sync_status = "pending"
            row.updated_at = datetime.now()

            event_rows = session.execute(
                select(NewsExtractedEvent).where(NewsExtractedEvent.card_id == key)
            ).scalars().all()
            for event in event_rows:
                links = _json_loads(event.entity_links_json, [])
                if not isinstance(links, list):
                    continue
                filtered = []
                for link in links:
                    if not isinstance(link, dict):
                        continue
                    if str(link.get("entity_type") or "").strip().lower() != "company":
                        filtered.append(link)
                        continue
                    symbol = str(link.get("symbol") or link.get("code") or "").strip().upper()
                    name = str(link.get("name") or "").strip()
                    if (symbol and symbol in allowed_symbols) or (name and name in allowed_names):
                        filtered.append(link)
                event.entity_links_json = _json_dumps(filtered)
                event.updated_at = datetime.now()
            session.flush()
            return self.card_to_dict(row)

        return self.db._run_write_transaction("news_signal.update_company_mapping_projection", _write)

    def merge_same_event_cluster(
        self,
        *,
        canonical_card_id: str,
        duplicate_card_ids: Iterable[str],
        cluster_id: str,
    ) -> Optional[Dict[str, Any]]:
        canonical_key = str(canonical_card_id or "").strip()
        duplicate_keys = sorted(
            {
                str(value or "").strip()
                for value in duplicate_card_ids
                if str(value or "").strip() and str(value or "").strip() != canonical_key
            }
        )
        if not canonical_key or not duplicate_keys:
            return None

        def _write(session):
            canonical = session.execute(
                select(NewsSignalCard).where(NewsSignalCard.card_id == canonical_key)
            ).scalar_one_or_none()
            duplicates = session.execute(
                select(NewsSignalCard).where(NewsSignalCard.card_id.in_(duplicate_keys))
            ).scalars().all()
            if canonical is None or not duplicates:
                return None

            raw_episode_ids = _json_loads(canonical.raw_episode_ids_json, [])
            source_chain = _json_loads(canonical.source_chain_json, [])
            transmission_paths = _json_loads(canonical.transmission_paths_json, [])
            raw_episode_ids = raw_episode_ids if isinstance(raw_episode_ids, list) else []
            source_chain = source_chain if isinstance(source_chain, list) else []
            transmission_paths = transmission_paths if isinstance(transmission_paths, list) else []
            member_ids = [canonical_key]

            for duplicate in duplicates:
                member_ids.append(str(duplicate.card_id))
                raw_episode_ids.extend(_json_loads(duplicate.raw_episode_ids_json, []))
                source_chain.extend(_json_loads(duplicate.source_chain_json, []))
                transmission_paths.extend(_json_loads(duplicate.transmission_paths_json, []))
                diagnostics = _json_dict(duplicate.diagnostics_json)
                diagnostics["event_cluster"] = {
                    "cluster_id": cluster_id,
                    "role": "duplicate",
                    "merged_into_card_id": canonical_key,
                    "member_card_ids": sorted(member_ids + duplicate_keys),
                    "merged_at": datetime.now().isoformat(),
                }
                duplicate.diagnostics_json = _json_dumps(diagnostics)
                duplicate.status = "suppressed"
                duplicate.graph_sync_status = "pending"
                duplicate.updated_at = datetime.now()

            canonical_diagnostics = _json_dict(canonical.diagnostics_json)
            canonical_diagnostics["event_cluster"] = {
                "cluster_id": cluster_id,
                "role": "canonical",
                "canonical_card_id": canonical_key,
                "member_card_ids": sorted(set(member_ids)),
                "merged_at": datetime.now().isoformat(),
            }
            canonical.raw_episode_ids_json = _json_dumps(_unique_values(raw_episode_ids))
            canonical.source_chain_json = _json_dumps(_unique_dicts(source_chain))
            canonical.transmission_paths_json = _json_dumps(_unique_dicts(transmission_paths))
            canonical.source_count = len(_unique_values(raw_episode_ids))
            canonical.diagnostics_json = _json_dumps(canonical_diagnostics)
            canonical.graph_sync_status = "pending"
            canonical.updated_at = datetime.now()

            event_rows = session.execute(
                select(NewsExtractedEvent).where(NewsExtractedEvent.card_id.in_(duplicate_keys))
            ).scalars().all()
            for event in event_rows:
                event.card_id = canonical_key
                event.updated_at = datetime.now()
            session.execute(
                delete(NewsSignalEdge).where(
                    or_(
                        NewsSignalEdge.source_card_id.in_(duplicate_keys),
                        NewsSignalEdge.target_card_id.in_(duplicate_keys),
                    )
                )
            )
            session.flush()
            return self.card_to_dict(canonical)

        return self.db._run_write_transaction("news_signal.merge_same_event_cluster", _write)

    def replace_edges_for_cards(self, card_ids: Iterable[str], edges: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        keys = sorted({str(card_id or "").strip() for card_id in card_ids if str(card_id or "").strip()})
        payloads = [item for item in edges if item.get("source_card_id") and item.get("target_id") and item.get("edge_type")]
        if not keys:
            return {"cards": 0, "edges_upserted": 0}

        def _write(session):
            session.execute(
                delete(NewsSignalEdge).where(
                    (NewsSignalEdge.source_card_id.in_(keys))
                    | (NewsSignalEdge.target_card_id.in_(keys))
                )
            )
            rows: List[NewsSignalEdge] = []
            for item in payloads:
                row = NewsSignalEdge(edge_id=str(item.get("edge_id") or ""))
                self._assign_edge(row, item)
                session.add(row)
                rows.append(row)
            session.flush()
            return {"cards": len(keys), "edges_upserted": len(rows)}

        return self.db._run_write_transaction("news_signal.replace_edges", _write)

    def list_edges(
        self,
        *,
        card_id: str = "",
        signal_date: Optional[Any] = None,
        edge_class: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        key = str(card_id or "").strip()
        target_date = _coerce_date(signal_date)
        edge_class_text = str(edge_class or "").strip()
        capped_limit = max(1, min(int(limit or 200), 1000))
        with self.db.get_session() as session:
            stmt = select(NewsSignalEdge)
            if key:
                stmt = stmt.where((NewsSignalEdge.source_card_id == key) | (NewsSignalEdge.target_card_id == key))
            if target_date:
                card_rows = session.execute(
                    select(NewsSignalCard.card_id).where(NewsSignalCard.signal_date == target_date)
                ).all()
                date_card_ids = [str(row[0]) for row in card_rows]
                if not date_card_ids:
                    return []
                stmt = stmt.where(
                    (NewsSignalEdge.source_card_id.in_(date_card_ids))
                    | (NewsSignalEdge.target_card_id.in_(date_card_ids))
                )
            if edge_class_text:
                stmt = stmt.where(NewsSignalEdge.edge_class == edge_class_text)
            rows = session.execute(
                stmt.order_by(
                    desc(NewsSignalEdge.edge_quality),
                    desc(NewsSignalEdge.weight),
                    desc(NewsSignalEdge.updated_at),
                ).limit(capped_limit)
            ).scalars().all()
            return [self.edge_to_dict(row) for row in rows]

    def raw_episodes_for_card(self, session, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_ids = card.get("raw_episode_ids") if isinstance(card.get("raw_episode_ids"), list) else []
        if not raw_ids:
            return []
        rows = session.execute(select(RawNewsEpisode).where(RawNewsEpisode.episode_id.in_([str(item) for item in raw_ids]))).scalars().all()
        return [self.raw_episode_to_dict(row) for row in rows]

    def extracted_events_for_card(self, session, card: Dict[str, Any]) -> List[Dict[str, Any]]:
        card_id = str(card.get("card_id") or "").strip()
        raw_ids = [str(item) for item in (card.get("raw_episode_ids") or []) if str(item).strip()]
        if not card_id and not raw_ids:
            return []
        stmt = select(NewsExtractedEvent)
        if card_id and raw_ids:
            stmt = stmt.where((NewsExtractedEvent.card_id == card_id) | (NewsExtractedEvent.raw_episode_id.in_(raw_ids)))
        elif card_id:
            stmt = stmt.where(NewsExtractedEvent.card_id == card_id)
        else:
            stmt = stmt.where(NewsExtractedEvent.raw_episode_id.in_(raw_ids))
        rows = session.execute(
            stmt.order_by(desc(NewsExtractedEvent.confidence), desc(NewsExtractedEvent.updated_at))
        ).scalars().all()
        return [self.extracted_event_to_dict(row) for row in rows]

    @staticmethod
    def edge_to_dict(row: NewsSignalEdge) -> Dict[str, Any]:
        return {
            "id": row.id,
            "edge_id": row.edge_id,
            "source_card_id": row.source_card_id,
            "target_card_id": row.target_card_id,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "edge_class": row.edge_class,
            "edge_type": row.edge_type,
            "weight": row.weight,
            "edge_quality": row.edge_quality,
            "quality_grade": row.quality_grade,
            "quality_flags": _json_list(row.quality_flags_json),
            "method": row.method,
            "rationale": row.rationale,
            "evidence": _json_dict(row.evidence_json),
            "embedding_model": row.embedding_model,
            "threshold_profile": row.threshold_profile,
            "decay_rule": row.decay_rule,
            "status": row.status,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def raw_episode_to_dict(row: RawNewsEpisode) -> Dict[str, Any]:
        return {
            "id": row.id,
            "episode_id": row.episode_id,
            "dedup_key": row.dedup_key,
            "source": row.source,
            "provider": row.provider,
            "source_id": row.source_id,
            "url": row.url,
            "title": row.title,
            "summary": row.summary,
            "content": row.content,
            "normalized_content": row.normalized_content,
            "quality_score": row.quality_score,
            "quality_grade": row.quality_grade,
            "quality_flags": _json_list(row.quality_flags_json),
            "published_at": _iso(row.published_at),
            "ingested_at": _iso(row.ingested_at),
            "signal_date": _iso(row.signal_date),
            "session": row.session,
            "subjects": _json_list(row.subjects_json),
            "stocks": _json_list(row.stocks_json),
            "source_chain": _json_list(row.source_chain_json),
            "raw_payload": _json_dict(row.raw_payload_json),
            "status": row.status,
            "errors": _json_list(row.errors_json),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def extracted_event_to_dict(row: NewsExtractedEvent) -> Dict[str, Any]:
        return {
            "id": row.id,
            "event_id": row.event_id,
            "raw_episode_id": row.raw_episode_id,
            "card_id": row.card_id,
            "signal_date": _iso(row.signal_date),
            "event_time": _iso(row.event_time),
            "event_type": row.event_type,
            "trigger": row.trigger,
            "subject": row.subject,
            "object": row.object,
            "direction": row.direction,
            "metric_value": row.metric_value,
            "evidence_sentence": row.evidence_sentence,
            "source_url": row.source_url,
            "source": row.source,
            "extractor": row.extractor,
            "confidence": row.confidence,
            "verification_status": row.verification_status,
            "verification_sources": _json_list(row.verification_sources_json),
            "entity_links": _json_list(row.entity_links_json),
            "diagnostics": _json_dict(row.diagnostics_json),
            "status": row.status,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def card_to_dict(row: NewsSignalCard) -> Dict[str, Any]:
        return {
            "id": row.id,
            "card_id": row.card_id,
            "signal_date": _iso(row.signal_date),
            "session": row.session,
            "signal_layer": getattr(row, "signal_layer", None) or "industry",
            "summary_short": row.summary_short,
            "news_tone": row.news_tone,
            "market_impact": row.market_impact,
            "impact_horizon": row.impact_horizon,
            "valid_from": _iso(row.valid_from),
            "valid_until": _iso(row.valid_until),
            "decay_rule": row.decay_rule,
            "refresh_trigger": row.refresh_trigger,
            "staleness_score": row.staleness_score,
            "evidence_grade": row.evidence_grade,
            "inference_level": row.inference_level,
            "mapping_status": row.mapping_status,
            "mapping_confidence": row.mapping_confidence,
            "signal_score": row.signal_score,
            "status": row.status,
            "primary_industries": _json_list(row.primary_industries_json),
            "secondary_industries": _json_list(row.secondary_industries_json),
            "explicit_entities": _json_list(row.explicit_entities_json),
            "industry_impacts": _json_list(row.industry_impacts_json),
            "company_impacts": _json_list(row.company_impacts_json),
            "transmission_paths": _json_list(row.transmission_paths_json),
            "raw_episode_ids": _json_list(row.raw_episode_ids_json),
            "source_chain": _json_list(row.source_chain_json),
            "diagnostics": _json_dict(row.diagnostics_json),
            "source_count": row.source_count,
            "graph_sync_status": row.graph_sync_status,
            "graph_retry_count": row.graph_retry_count,
            "graph_last_error": row.graph_last_error,
            "embedding_model": row.embedding_model,
            "embedding_dimension": row.embedding_dimension,
            "threshold_profile": row.threshold_profile,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    @staticmethod
    def feedback_to_dict(row: NewsSignalFeedback) -> Dict[str, Any]:
        return {
            "id": row.id,
            "card_id": row.card_id,
            "feedback_type": row.feedback_type,
            "note": row.note,
            "payload": _json_dict(row.payload_json),
            "user_id": row.user_id,
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def seed_link_to_dict(row: NewsSignalSeedLink) -> Dict[str, Any]:
        return {
            "id": row.id,
            "card_id": row.card_id,
            "seed_item_id": row.seed_item_id,
            "source_desk": row.source_desk,
            "gate_result": row.gate_result,
            "signal_score_snapshot": row.signal_score_snapshot,
            "mapping_confidence": row.mapping_confidence,
            "evidence_grade": row.evidence_grade,
            "created_at": _iso(row.created_at),
        }

    @staticmethod
    def _refresh_raw_episode_identity(
        row: RawNewsEpisode,
        *,
        episode_id: str,
        dedup_key: str,
        matches: List[RawNewsEpisode],
    ) -> None:
        if row.episode_id != episode_id and not any(match is not row and match.episode_id == episode_id for match in matches):
            row.episode_id = episode_id
        if row.dedup_key != dedup_key and not any(match is not row and match.dedup_key == dedup_key for match in matches):
            row.dedup_key = dedup_key

    @staticmethod
    def _assign_raw_episode(row: RawNewsEpisode, item: Dict[str, Any]) -> None:
        row.source = str(item.get("source") or "unknown")
        row.provider = str(item.get("provider") or "")
        row.source_id = str(item.get("source_id") or "")
        row.url = str(item.get("url") or "")
        row.title = str(item.get("title") or "")[:300]
        row.summary = str(item.get("summary") or "")
        row.content = str(item.get("content") or "")
        row.normalized_content = str(item.get("normalized_content") or "")
        row.quality_score = _float_or_none(item.get("quality_score")) or 0.0
        row.quality_grade = str(item.get("quality_grade") or "unknown")
        row.quality_flags_json = _json_dumps(item.get("quality_flags") or [])
        row.published_at = _coerce_datetime(item.get("published_at"))
        row.ingested_at = _coerce_datetime(item.get("ingested_at")) or datetime.now()
        row.signal_date = _coerce_date(item.get("signal_date")) or datetime.now().date()
        row.session = str(item.get("session") or "unknown")
        row.subjects_json = _json_dumps(item.get("subjects") or [])
        row.stocks_json = _json_dumps(item.get("stocks") or [])
        row.source_chain_json = _json_dumps(item.get("source_chain") or [])
        row.raw_payload_json = _json_dumps(item.get("raw_payload") or {})
        row.status = str(item.get("status") or "ok")
        row.errors_json = _json_dumps(item.get("errors") or [])
        row.updated_at = datetime.now()

    @staticmethod
    def _assign_extracted_event(row: NewsExtractedEvent, item: Dict[str, Any]) -> None:
        row.event_id = str(item.get("event_id") or "")
        row.raw_episode_id = str(item.get("raw_episode_id") or "")
        row.card_id = str(item.get("card_id") or "")
        row.signal_date = _coerce_date(item.get("signal_date")) or datetime.now().date()
        row.event_time = _coerce_datetime(item.get("event_time"))
        row.event_type = str(item.get("event_type") or "unknown")
        row.trigger = str(item.get("trigger") or "")[:120]
        row.subject = str(item.get("subject") or "")[:200]
        row.object = str(item.get("object") or "")[:300]
        row.direction = str(item.get("direction") or "neutral")
        row.metric_value = str(item.get("metric_value") or "")[:120]
        row.evidence_sentence = str(item.get("evidence_sentence") or "")
        row.source_url = str(item.get("source_url") or "")
        row.source = str(item.get("source") or "")
        row.extractor = str(item.get("extractor") or "rule_fallback")
        row.confidence = _float_or_none(item.get("confidence")) or 0.0
        row.verification_status = str(item.get("verification_status") or "source_only")
        row.verification_sources_json = _json_dumps(item.get("verification_sources") or [])
        row.entity_links_json = _json_dumps(item.get("entity_links") or [])
        row.diagnostics_json = _json_dumps(item.get("diagnostics") or {})
        row.status = str(item.get("status") or "active")
        row.updated_at = datetime.now()

    @staticmethod
    def _assign_card(row: NewsSignalCard, item: Dict[str, Any]) -> None:
        row.signal_date = _coerce_date(item.get("signal_date")) or datetime.now().date()
        row.session = str(item.get("session") or "unknown")
        row.signal_layer = str(item.get("signal_layer") or "industry")
        row.summary_short = str(item.get("summary_short") or "")[:300]
        row.news_tone = str(item.get("news_tone") or "neutral")
        row.market_impact = str(item.get("market_impact") or "unknown")
        row.impact_horizon = str(item.get("impact_horizon") or "short")
        row.valid_from = item.get("valid_from")
        row.valid_until = item.get("valid_until")
        row.decay_rule = str(item.get("decay_rule") or "3d")
        row.refresh_trigger = str(item.get("refresh_trigger") or "")[:300]
        row.staleness_score = _float_or_none(item.get("staleness_score")) or 0.0
        row.evidence_grade = str(item.get("evidence_grade") or "plausible")
        row.inference_level = str(item.get("inference_level") or "first_order")
        row.mapping_status = str(item.get("mapping_status") or "industry_only")
        row.mapping_confidence = _float_or_none(item.get("mapping_confidence")) or 0.0
        row.signal_score = _float_or_none(item.get("signal_score")) or 0.0
        row.status = str(item.get("status") or "active")
        row.primary_industries_json = _json_dumps(item.get("primary_industries") or [])
        row.secondary_industries_json = _json_dumps(item.get("secondary_industries") or [])
        row.explicit_entities_json = _json_dumps(item.get("explicit_entities") or [])
        row.industry_impacts_json = _json_dumps(item.get("industry_impacts") or [])
        row.company_impacts_json = _json_dumps(item.get("company_impacts") or [])
        row.transmission_paths_json = _json_dumps(item.get("transmission_paths") or [])
        row.raw_episode_ids_json = _json_dumps(item.get("raw_episode_ids") or [])
        row.source_chain_json = _json_dumps(item.get("source_chain") or [])
        row.diagnostics_json = _json_dumps(item.get("diagnostics") or {})
        row.source_count = int(item.get("source_count") or len(item.get("raw_episode_ids") or []) or 1)
        row.graph_sync_status = str(item.get("graph_sync_status") or row.graph_sync_status or "pending")
        row.graph_retry_count = int(item.get("graph_retry_count") or row.graph_retry_count or 0)
        row.graph_last_error = str(item.get("graph_last_error") or row.graph_last_error or "")
        row.embedding_model = item.get("embedding_model")
        row.embedding_dimension = item.get("embedding_dimension")
        row.threshold_profile = item.get("threshold_profile")
        row.updated_at = datetime.now()

    @staticmethod
    def _assign_edge(row: NewsSignalEdge, item: Dict[str, Any]) -> None:
        row.edge_id = str(item.get("edge_id") or "")
        row.source_card_id = str(item.get("source_card_id") or "")
        target_card_id = str(item.get("target_card_id") or "").strip()
        row.target_card_id = target_card_id or None
        row.target_type = str(item.get("target_type") or "unknown")
        row.target_id = str(item.get("target_id") or "")
        row.edge_class = str(item.get("edge_class") or "typed_relation")
        row.edge_type = str(item.get("edge_type") or "related_to")
        row.weight = _float_or_none(item.get("weight")) or 0.0
        row.edge_quality = _float_or_none(item.get("edge_quality")) or 0.0
        row.quality_grade = str(item.get("quality_grade") or "unknown")
        row.quality_flags_json = _json_dumps(item.get("quality_flags") or [])
        row.method = str(item.get("method") or "rule")
        row.rationale = str(item.get("rationale") or "")
        row.evidence_json = _json_dumps(item.get("evidence") or {})
        row.embedding_model = item.get("embedding_model")
        row.threshold_profile = item.get("threshold_profile")
        row.decay_rule = str(item.get("decay_rule") or "none")
        row.status = str(item.get("status") or "active")
        row.updated_at = datetime.now()

    @staticmethod
    def _feedback_counts(session, card_ids: List[str]) -> Dict[str, Dict[str, int]]:
        if not card_ids:
            return {}
        rows = session.execute(
            select(NewsSignalFeedback.card_id, NewsSignalFeedback.feedback_type, func.count(NewsSignalFeedback.id))
            .where(NewsSignalFeedback.card_id.in_(card_ids))
            .group_by(NewsSignalFeedback.card_id, NewsSignalFeedback.feedback_type)
        ).all()
        counts: Dict[str, Dict[str, int]] = {}
        for card_id, feedback_type, count in rows:
            counts.setdefault(str(card_id), {})[str(feedback_type or "unknown")] = int(count or 0)
        return counts


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _adjusted_score(score: Any, feedback_counts: Dict[str, int]) -> float:
    base = _float_or_none(score) or 0.0
    penalty = 0.0
    penalty += 35.0 * int(feedback_counts.get("wrong") or 0)
    penalty += 18.0 * int(feedback_counts.get("noisy") or 0)
    penalty += 10.0 * int(feedback_counts.get("duplicate") or 0)
    bonus = 4.0 * int(feedback_counts.get("useful") or 0)
    return round(base + bonus - penalty, 4)


def _count_by(rows: Iterable[NewsSignalCard], attr: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, attr, None) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts
