# -*- coding: utf-8 -*-
"""Independent worker for durable Graphiti projection jobs."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.repositories.graphiti_outbox_repo import GraphitiOutboxRepository
from src.repositories.news_signal_repo import NewsSignalRepository
from src.services.graphiti.graph_service import get_graphiti_service


class GraphitiOutboxWorker:
    def __init__(
        self,
        *,
        outbox_repo: Optional[GraphitiOutboxRepository] = None,
        news_repo: Optional[NewsSignalRepository] = None,
        graphiti: Any = None,
        max_attempts: int = 5,
        retry_base_seconds: int = 30,
        job_timeout_seconds: int = 120,
    ) -> None:
        self.outbox_repo = outbox_repo or GraphitiOutboxRepository()
        self.news_repo = news_repo or NewsSignalRepository()
        self.graphiti = graphiti or get_graphiti_service()
        self.max_attempts = max(1, int(max_attempts or 5))
        self.retry_base_seconds = max(1, int(retry_base_seconds or 30))
        self.job_timeout_seconds = max(10, min(int(job_timeout_seconds or 120), 600))

    def run_once(self, *, limit: int = 10) -> Dict[str, Any]:
        jobs = self.outbox_repo.claim_batch(limit=limit)
        succeeded = 0
        failed = 0
        dead = 0
        details = []
        for job in jobs:
            try:
                result = self._process(job)
                self.outbox_repo.mark_succeeded(job["id"])
                succeeded += 1
                details.append({"id": job["id"], "status": "succeeded", "result": result})
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}".rstrip(": ")
                status = self.outbox_repo.mark_failed(
                    job["id"],
                    error=error_text,
                    max_attempts=self.max_attempts,
                    retry_base_seconds=self.retry_base_seconds,
                )
                failed += 1
                dead += int(status == "dead")
                details.append({"id": job["id"], "status": status, "error": error_text})
        return {
            "status": "ok" if failed == 0 else ("partial" if succeeded else "failed"),
            "claimed": len(jobs),
            "succeeded": succeeded,
            "failed": failed,
            "dead": dead,
            "details": details,
            "queue": self.outbox_repo.metrics(),
        }

    def _process(self, job: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(job.get("event_type") or "")
        if event_type == "news_signal_card_episode":
            return self._process_news_signal_card(job)
        if event_type == "news_signal_card_delete":
            return self._process_news_signal_card_delete(job)
        if event_type == "news_signal_edge_projection":
            return self._process_news_signal_edges(job)
        raise ValueError(f"unsupported Graphiti outbox event_type: {event_type}")

    def _process_news_signal_card(self, job: Dict[str, Any]) -> Dict[str, Any]:
        card_id = str(job.get("payload", {}).get("card_id") or job.get("aggregate_id") or "")
        card = self.news_repo.get_card(card_id)
        if not card:
            raise ValueError(f"news signal card not found: {card_id}")
        if str(card.get("status") or "active") != "active":
            return {"status": "skipped", "reason": "card_not_active", "card_id": card_id}
        result = self.graphiti.ingest_news_signal_card_sync(
            card=card,
            market=str(job.get("market") or "cn"),
            timeout_seconds=self.job_timeout_seconds,
        )
        if str(result.get("status") or "") != "synced":
            error = str(result.get("error") or result.get("reason") or "Graphiti episode sync failed")
            self.news_repo.update_graph_sync_status(card_id, status="failed", error=error)
            raise RuntimeError(error)
        self.news_repo.update_graph_sync_status(card_id, status="synced")
        return result

    def _process_news_signal_card_delete(self, job: Dict[str, Any]) -> Dict[str, Any]:
        card_id = str(job.get("payload", {}).get("card_id") or job.get("aggregate_id") or "")
        card = self.news_repo.get_card(card_id)
        if card and str(card.get("status") or "active") == "active":
            return {"status": "skipped", "reason": "card_is_active", "card_id": card_id}
        result = self.graphiti.remove_news_signal_card_sync(
            card_id=card_id,
            market=str(job.get("market") or "cn"),
        )
        if str(result.get("status") or "") not in {"removed", "skipped"}:
            raise RuntimeError(str(result.get("error") or result.get("reason") or "Graphiti episode removal failed"))
        return result

    def _process_news_signal_edges(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        signal_date = str(payload.get("signal_date") or "")
        cards = self.news_repo.list_cards(signal_date=signal_date or None, limit=500)
        edges = self.news_repo.list_edges(signal_date=signal_date or None, limit=10000)
        result = self.graphiti.sync_news_signal_edges_sync(
            cards=cards,
            edges=edges,
            market=str(job.get("market") or "cn"),
        )
        if str(result.get("status") or "") not in {"ok", "skipped"}:
            raise RuntimeError(str(result.get("error") or result.get("reason") or "Graphiti edge projection failed"))
        return result
