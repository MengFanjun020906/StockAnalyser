# -*- coding: utf-8 -*-
"""Graphiti service wrapper."""

from __future__ import annotations

import asyncio
import logging
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from src.config import get_config

logger = logging.getLogger(__name__)

_service_lock = Lock()
_service_instance: "GraphitiService | None" = None


def _safe_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(item) for item in value]
    if is_dataclass(value):
        return _safe_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        try:
            return _safe_jsonable(value.model_dump())
        except Exception:
            return str(value)
    if hasattr(value, "dict"):
        try:
            return _safe_jsonable(value.dict())
        except Exception:
            return str(value)
    return str(value)


def _safe_group_token(value: str | None, default: str) -> str:
    candidate = (value or default).strip()
    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "_", candidate)
    return candidate.strip("_") or default


class GraphitiService:
    """Best-effort Graphiti facade with lazy initialization."""

    def __init__(self) -> None:
        self.config = get_config()
        self.enabled = bool(self.config.graphiti_enabled)
        self._client = None
        self._graphiti_cls = None
        self._indices_ready = False
        if not self.enabled:
            logger.info("Graphiti disabled by config")
            return

        try:
            from graphiti_core.graphiti import Graphiti
            from .litellm_client import LiteLLMGraphitiClient
            from .litellm_embedder import LiteLLMGraphitiEmbedder

            self._graphiti_cls = Graphiti
            self._client = Graphiti(
                uri=self.config.graphiti_neo4j_uri,
                user=self.config.graphiti_neo4j_user,
                password=self.config.graphiti_neo4j_password,
                llm_client=LiteLLMGraphitiClient(),
                embedder=LiteLLMGraphitiEmbedder(),
            )
            logger.info(
                "Graphiti initialized (uri=%s, group_strategy=%s)",
                self.config.graphiti_neo4j_uri,
                self.config.graphiti_group_strategy,
            )
        except Exception as exc:
            self.enabled = False
            self._client = None
            logger.warning("Graphiti initialization failed, integration disabled: %s", exc, exc_info=True)

    def is_available(self) -> bool:
        return self.enabled and self._client is not None

    async def _ensure_indices(self) -> bool:
        """Create Graphiti's Neo4j schema before first write/search."""
        if not self.is_available():
            return False
        if self._indices_ready:
            return True

        try:
            await self._client.build_indices_and_constraints()
            self._indices_ready = True
            logger.info("Graphiti indices and constraints are ready")
            return True
        except Exception as exc:
            logger.warning("Graphiti index initialization failed: %s", exc, exc_info=True)
            return False

    def _resolve_group_id(self, market: str | None = None, user_id: str | None = None) -> str:
        strategy = self.config.graphiti_group_strategy
        if strategy == "single":
            return "daily_stock_analysis"
        if strategy == "user":
            return f"user_{_safe_group_token(user_id, 'default')}"
        return f"market_{_safe_group_token((market or 'cn').lower(), 'cn')}"

    async def ingest_analysis(
        self,
        *,
        code: str,
        stock_name: str,
        report_type: str,
        result: Any,
        context: dict[str, Any],
        news_context: str | None = None,
        market: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if not self.is_available():
            return
        if not await self._ensure_indices():
            return

        episode_body = {
            "code": code,
            "stock_name": stock_name,
            "report_type": report_type,
            "result": _safe_jsonable(result),
            "context": _safe_jsonable(context),
            "news_context": news_context,
        }
        group_id = self._resolve_group_id(market=market, user_id=user_id)
        episode_name = f"analysis:{code}:{datetime.now(timezone.utc).date().isoformat()}"

        try:
            from .ontology import DEFAULT_ENTITY_TYPES

            await self._client.add_episode(
                name=episode_name,
                episode_body=json.dumps(_safe_jsonable(episode_body), ensure_ascii=False, default=str),
                source_description="stock_analysis_pipeline",
                reference_time=datetime.now(timezone.utc),
                group_id=group_id,
                entity_types=DEFAULT_ENTITY_TYPES,
            )
            logger.info("Graphiti ingested analysis episode for %s (group_id=%s)", code, group_id)
        except Exception as exc:
            logger.warning("Graphiti analysis ingestion failed for %s: %s", code, exc, exc_info=True)

    async def ingest_trace(
        self,
        *,
        session_id: str,
        trace_type: str,
        title: str,
        result: Any,
        context: dict[str, Any],
        artifact_dir: str | None = None,
        market: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if not self.is_available():
            return
        if not await self._ensure_indices():
            return

        episode_body = {
            "session_id": session_id,
            "trace_type": trace_type,
            "title": title,
            "artifact_dir": artifact_dir,
            "result": _safe_jsonable(result),
            "context": _safe_jsonable(context),
        }
        group_id = self._resolve_group_id(market=market, user_id=user_id)
        episode_name = f"trace:{session_id}"

        try:
            from .ontology import DEFAULT_ENTITY_TYPES

            await self._client.add_episode(
                name=episode_name,
                episode_body=json.dumps(_safe_jsonable(episode_body), ensure_ascii=False, default=str),
                source_description="agent_trace",
                reference_time=datetime.now(timezone.utc),
                group_id=group_id,
                entity_types=DEFAULT_ENTITY_TYPES,
            )
            logger.info("Graphiti ingested trace episode for %s (group_id=%s)", session_id, group_id)
        except Exception as exc:
            logger.warning("Graphiti trace ingestion failed for %s: %s", session_id, exc, exc_info=True)

    def ingest_analysis_sync(self, **kwargs: Any) -> None:
        if not self.is_available():
            return

        try:
            asyncio.run(self.ingest_analysis(**kwargs))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.ingest_analysis(**kwargs))
            finally:
                loop.close()

    def ingest_trace_sync(self, **kwargs: Any) -> None:
        if not self.is_available():
            return

        try:
            asyncio.run(self.ingest_trace(**kwargs))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.ingest_trace(**kwargs))
            finally:
                loop.close()

    async def search(self, query: str, *, market: str | None = None, limit: int = 10) -> dict[str, Any]:
        if not self.is_available():
            return {"success": False, "error": "Graphiti is disabled"}
        if not await self._ensure_indices():
            return {"success": False, "error": "Graphiti index initialization failed", "query": query}

        try:
            results = await self._client.search_(
                query=query,
                group_ids=[self._resolve_group_id(market=market)],
            )
            edges = getattr(results, "edges", []) or []
            nodes = getattr(results, "nodes", []) or []
            episodes = getattr(results, "episodes", []) or []
            return {
                "success": True,
                "query": query,
                "edges": [_safe_jsonable(edge) for edge in edges[:limit]],
                "nodes": [_safe_jsonable(node) for node in nodes[:limit]],
                "episodes": [_safe_jsonable(item) for item in episodes[:limit]],
            }
        except Exception as exc:
            logger.warning("Graphiti search failed for query=%r: %s", query, exc, exc_info=True)
            return {"success": False, "error": str(exc), "query": query}

    def search_sync(self, query: str, *, market: str | None = None, limit: int = 10) -> dict[str, Any]:
        if not self.is_available():
            return {"success": False, "error": "Graphiti is disabled", "query": query}

        try:
            return asyncio.run(self.search(query, market=market, limit=limit))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self.search(query, market=market, limit=limit))
            finally:
                loop.close()

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            await close()


def get_graphiti_service() -> GraphitiService:
    global _service_instance
    if _service_instance is not None:
        return _service_instance
    with _service_lock:
        if _service_instance is None:
            _service_instance = GraphitiService()
    return _service_instance
