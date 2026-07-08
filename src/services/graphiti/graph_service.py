# -*- coding: utf-8 -*-
"""Graphiti service wrapper."""

from __future__ import annotations

import asyncio
import logging
import json
import re
import socket
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from threading import Lock, Thread
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


def _can_open_tcp(uri: str, timeout_seconds: float = 0.5) -> tuple[bool, str]:
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)(?::(\d+))?", uri or "")
    if not match:
        return False, "invalid_uri"
    host = match.group(1)
    port = int(match.group(2) or 7687)
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}:{exc}"


def _parse_reference_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _trim_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _join_values(values: Any, *, limit: int = 12) -> str:
    items: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            items.append(text)
    return "、".join(items[:limit]) if items else "无"


def _format_company_impact(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    symbol = str(item.get("symbol") or item.get("code") or "").strip()
    label = name or symbol or "未知公司"
    if symbol and name:
        label = f"{name}({symbol})"
    direction = str(item.get("direction") or "unknown")
    confidence = item.get("confidence")
    confidence_text = ""
    try:
        confidence_text = f"，置信度 {float(confidence):.2f}"
    except Exception:
        pass
    role = str(item.get("role") or "").strip()
    rationale = str(item.get("rationale") or "").strip()
    details = f"{direction}{confidence_text}"
    if role:
        details += f"，角色 {role}"
    if rationale:
        details += f"，依据：{_trim_text(rationale, 120)}"
    return f"- {label}: {details}"


def _news_signal_semantic_text(card: dict[str, Any]) -> str:
    industries = _join_values(card.get("primary_industries") or [])
    secondary = _join_values(card.get("secondary_industries") or [])
    entities = _join_values(card.get("explicit_entities") or [])
    companies = "、".join(
        (
            f"{item.get('name')}({item.get('symbol') or item.get('code')})"
            if item.get("name") and (item.get("symbol") or item.get("code"))
            else str(item.get("name") or item.get("symbol") or item.get("code") or "").strip()
        )
        for item in card.get("company_impacts") or []
        if isinstance(item, dict) and (item.get("name") or item.get("symbol") or item.get("code"))
    ) or "无"
    paths = []
    for item in card.get("transmission_paths") or []:
        if isinstance(item, dict):
            path = " -> ".join(str(part) for part in item.get("path") or [] if str(part).strip())
            if path:
                paths.append(path)
    parts = [
        f"新闻信号卡片 {card.get('card_id')}: {_trim_text(card.get('summary_short'), 220)}",
        f"信号日期 {card.get('signal_date')}，层级 {card.get('signal_layer')}，情绪 {card.get('news_tone')}，影响 {card.get('market_impact')}，周期 {card.get('impact_horizon')}。",
    ]
    if industries:
        parts.append(f"主要产业或主题：{industries}。")
    if secondary:
        parts.append(f"次要产业或相关板块：{secondary}。")
    if entities:
        parts.append(f"显式实体：{entities}。")
    if companies:
        parts.append(f"涉及公司：{companies}。")
    if paths:
        parts.append(f"传导路径：{'；'.join(paths[:5])}。")
    parts.append(
        f"证据等级 {card.get('evidence_grade')}，推理层级 {card.get('inference_level')}，映射状态 {card.get('mapping_status')}，信号分 {card.get('signal_score')}。"
    )
    return "\n".join(parts)


def _news_signal_episode_text(card: dict[str, Any], raw_episodes: list[dict[str, Any]]) -> str:
    lines = [
        "# 新闻信号卡片",
        f"卡片 ID: {card.get('card_id') or 'unknown'}",
        f"信号日期: {card.get('signal_date') or 'unknown'}",
        f"交易时段: {card.get('session') or 'unknown'}",
        f"层级: {card.get('signal_layer') or 'unknown'}",
        f"情绪/影响/周期: {card.get('news_tone') or 'unknown'} / {card.get('market_impact') or 'unknown'} / {card.get('impact_horizon') or 'unknown'}",
        f"有效期: {card.get('valid_from') or 'unknown'} 至 {card.get('valid_until') or 'unknown'}",
        f"证据等级: {card.get('evidence_grade') or 'unknown'}；推理层级: {card.get('inference_level') or 'unknown'}；映射状态: {card.get('mapping_status') or 'unknown'}；信号分: {card.get('signal_score') or 0}",
        "",
        "## 新闻摘要",
        _trim_text(card.get("summary_short"), 500) or "无",
        "",
        "## 产业与实体",
        f"- 主要产业: {_join_values(card.get('primary_industries') or [])}",
        f"- 次要产业: {_join_values(card.get('secondary_industries') or [])}",
        f"- 显式实体: {_join_values(card.get('explicit_entities') or [])}",
        "",
        "## 公司影响",
    ]
    company_lines = [
        _format_company_impact(item)
        for item in card.get("company_impacts") or []
        if isinstance(item, dict)
    ]
    lines.extend(company_lines[:12] or ["无"])

    path_lines: list[str] = []
    for item in card.get("transmission_paths") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        mechanism = str(item.get("mechanism") or "").strip()
        target = str(item.get("target") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        path_text = " -> ".join(part for part in [source, mechanism, target] if part)
        if not path_text:
            path_text = " -> ".join(str(part) for part in item.get("path") or [] if str(part).strip())
        if path_text or rationale:
            path_lines.append(f"- {path_text or '传导路径'}；依据：{_trim_text(rationale, 180) or '无'}")
    lines.extend(["", "## 传导路径"])
    lines.extend(path_lines[:8] or ["无"])

    lines.extend(["", "## 原始消息"])
    for raw in raw_episodes[:5]:
        title = _trim_text(raw.get("title") or raw.get("summary") or raw.get("content"), 240)
        lines.append(f"- 原始消息: {raw.get('episode_id') or 'unknown'}")
        lines.append(f"  来源: {raw.get('provider') or raw.get('source') or 'unknown'}")
        lines.append(f"  时间: {raw.get('published_at') or 'unknown'}")
        if raw.get("url"):
            lines.append(f"  链接: {raw.get('url')}")
        lines.append(f"  标题: {title or '无'}")

    lines.extend(["", "## 语义摘要", _news_signal_semantic_text(card)])
    return "\n".join(lines)


def _news_signal_graph_label(value: Any) -> str:
    text = str(value or "").strip()
    if ":" in text:
        return text.split(":", 1)[1] or text
    return text


def _news_signal_card_node_props(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": str(card.get("card_id") or ""),
        "summary_short": _trim_text(card.get("summary_short"), 300),
        "signal_date": str(card.get("signal_date") or ""),
        "session": str(card.get("session") or ""),
        "signal_layer": str(card.get("signal_layer") or ""),
        "news_tone": str(card.get("news_tone") or ""),
        "market_impact": str(card.get("market_impact") or ""),
        "impact_horizon": str(card.get("impact_horizon") or ""),
        "signal_score": float(card.get("signal_score") or 0.0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _news_signal_relation_type(edge: dict[str, Any]) -> str:
    edge_class = str(edge.get("edge_class") or "")
    if edge_class == "semantic_similarity":
        return "NEWS_SIGNAL_SEMANTIC_SIMILARITY"
    if edge_class == "event_clue":
        return "NEWS_SIGNAL_EVENT_CLUE"
    return "NEWS_SIGNAL_TYPED_RELATION"


def _news_signal_edge_props(edge: dict[str, Any], group_id: str) -> dict[str, Any]:
    return {
        "edge_id": str(edge.get("edge_id") or ""),
        "source_card_id": str(edge.get("source_card_id") or ""),
        "target_card_id": str(edge.get("target_card_id") or ""),
        "target_type": str(edge.get("target_type") or ""),
        "target_id": str(edge.get("target_id") or ""),
        "edge_class": str(edge.get("edge_class") or ""),
        "edge_type": str(edge.get("edge_type") or ""),
        "weight": float(edge.get("weight") or 0.0),
        "edge_quality": float(edge.get("edge_quality") or 0.0),
        "quality_grade": str(edge.get("quality_grade") or ""),
        "quality_flags_json": json.dumps(_safe_jsonable(edge.get("quality_flags") or []), ensure_ascii=False, default=str),
        "method": str(edge.get("method") or ""),
        "rationale": _trim_text(edge.get("rationale"), 1000),
        "evidence_json": json.dumps(_safe_jsonable(edge.get("evidence") or {}), ensure_ascii=False, default=str),
        "embedding_model": str(edge.get("embedding_model") or ""),
        "threshold_profile": str(edge.get("threshold_profile") or ""),
        "decay_rule": str(edge.get("decay_rule") or ""),
        "status": str(edge.get("status") or "active"),
        "group_id": group_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class GraphitiService:
    """Best-effort Graphiti facade with lazy initialization."""

    def __init__(self) -> None:
        self.config = get_config()
        self.enabled = bool(self.config.graphiti_enabled)
        self._client = None
        self._graphiti_cls = None
        self._indices_ready = False
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: Thread | None = None
        self._sync_loop_lock = Lock()
        if not self.enabled:
            logger.info("Graphiti disabled by config")
            return

        ok, error = _can_open_tcp(self.config.graphiti_neo4j_uri)
        if not ok:
            self.enabled = False
            logger.warning(
                "Graphiti disabled because Neo4j is unreachable at %s: %s",
                self.config.graphiti_neo4j_uri,
                error,
            )
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

    def _ensure_sync_loop(self) -> asyncio.AbstractEventLoop:
        with self._sync_loop_lock:
            if self._sync_loop is not None and self._sync_loop.is_running():
                return self._sync_loop

            loop = asyncio.new_event_loop()

            def _run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = Thread(target=_run_loop, name="graphiti-sync-loop", daemon=True)
            thread.start()
            self._sync_loop = loop
            self._sync_thread = thread
            return loop

    def _run_sync(self, coro) -> Any:
        loop = self._ensure_sync_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

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

    async def ingest_market_event(
        self,
        *,
        event_id: str,
        title: str,
        event_payload: dict[str, Any],
        market: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Best-effort write of a market event / event-impact watch episode."""
        if not self.is_available():
            return
        if not await self._ensure_indices():
            return

        episode_body = {
            "event_id": event_id,
            "title": title,
            "event_payload": _safe_jsonable(event_payload),
        }
        group_id = self._resolve_group_id(market=market, user_id=user_id)
        safe_event_id = _safe_group_token(event_id, "event")
        episode_name = f"market_event:{safe_event_id}:{datetime.now(timezone.utc).date().isoformat()}"

        try:
            from .ontology import DEFAULT_ENTITY_TYPES

            await self._client.add_episode(
                name=episode_name,
                episode_body=json.dumps(_safe_jsonable(episode_body), ensure_ascii=False, default=str),
                source_description="event_impact_candidate_discovery",
                reference_time=datetime.now(timezone.utc),
                group_id=group_id,
                entity_types=DEFAULT_ENTITY_TYPES,
            )
            logger.info("Graphiti ingested market event %s (group_id=%s)", event_id, group_id)
        except Exception as exc:
            logger.warning("Graphiti market event ingestion failed for %s: %s", event_id, exc, exc_info=True)

    async def ingest_news_signal_card(
        self,
        *,
        card: dict[str, Any],
        market: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Best-effort write of one persistent NewsSignalCard as a Graphiti episode."""
        if not self.is_available():
            return {"status": "skipped", "reason": "graphiti_unavailable"}
        if not await self._ensure_indices():
            return {"status": "failed", "error": "Graphiti index initialization failed"}

        card_id = str(card.get("card_id") or "").strip()
        if not card_id:
            return {"status": "failed", "error": "card_id is required"}

        raw_episodes = []
        for raw in card.get("raw_episodes") or []:
            if not isinstance(raw, dict):
                continue
            raw_episodes.append({
                "episode_id": raw.get("episode_id"),
                "source": raw.get("source"),
                "provider": raw.get("provider"),
                "url": raw.get("url"),
                "title": raw.get("title"),
                "summary": _trim_text(raw.get("summary"), 600),
                "content": _trim_text(raw.get("content"), 1200),
                "published_at": raw.get("published_at"),
                "signal_date": raw.get("signal_date"),
                "subjects": raw.get("subjects") or [],
                "stocks": raw.get("stocks") or [],
            })

        episode_body = _news_signal_episode_text(card, raw_episodes)
        group_id = self._resolve_group_id(market=market, user_id=user_id)
        episode_name = f"news_signal:{_safe_group_token(card_id, 'news_signal')}"
        reference_time = _parse_reference_time(card.get("valid_from") or card.get("signal_date"))

        try:
            from .ontology import DEFAULT_ENTITY_TYPES

            await self._client.add_episode(
                name=episode_name,
                episode_body=episode_body,
                source_description="news_signal_card",
                reference_time=reference_time,
                group_id=group_id,
                entity_types=DEFAULT_ENTITY_TYPES,
            )
            logger.info("Graphiti ingested news signal card %s (group_id=%s)", card_id, group_id)
            return {"status": "synced", "group_id": group_id, "episode_name": episode_name}
        except Exception as exc:
            logger.warning("Graphiti news signal ingestion failed for %s: %s", card_id, exc, exc_info=True)
            return {"status": "failed", "error": str(exc)}

    def ingest_analysis_sync(self, **kwargs: Any) -> None:
        if not self.is_available():
            return

        self._run_sync(self.ingest_analysis(**kwargs))

    def ingest_trace_sync(self, **kwargs: Any) -> None:
        if not self.is_available():
            return

        self._run_sync(self.ingest_trace(**kwargs))

    def ingest_market_event_sync(self, **kwargs: Any) -> None:
        if not self.is_available():
            return

        self._run_sync(self.ingest_market_event(**kwargs))

    def ingest_news_signal_card_sync(self, **kwargs: Any) -> dict[str, Any]:
        if not self.is_available():
            return {"status": "skipped", "reason": "graphiti_unavailable"}

        return self._run_sync(self.ingest_news_signal_card(**kwargs))

    def sync_news_signal_edges_sync(
        self,
        *,
        cards: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        market: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Project deterministic news-signal edges into Graphiti's Neo4j store."""

        if not self.config.graphiti_enabled:
            return {"status": "disabled", "reason": "graphiti_disabled", "edges": 0}
        if not edges:
            return {"status": "skipped", "reason": "no_edges", "edges": 0}

        try:
            from neo4j import GraphDatabase
        except Exception as exc:
            return {"status": "disabled", "reason": f"neo4j_driver_unavailable: {exc}", "edges": 0}

        group_id = self._resolve_group_id(market=market, user_id=user_id)
        card_by_id = {
            str(card.get("card_id") or ""): card
            for card in cards
            if str(card.get("card_id") or "").strip()
        }
        touched_card_ids = {
            str(edge.get("source_card_id") or "").strip()
            for edge in edges
            if str(edge.get("source_card_id") or "").strip()
        }
        touched_card_ids.update(
            str(edge.get("target_card_id") or "").strip()
            for edge in edges
            if str(edge.get("target_card_id") or "").strip()
        )
        for card_id in touched_card_ids:
            card_by_id.setdefault(card_id, {"card_id": card_id, "summary_short": card_id})

        driver = GraphDatabase.driver(
            self.config.graphiti_neo4j_uri,
            auth=(self.config.graphiti_neo4j_user, self.config.graphiti_neo4j_password or ""),
        )
        projected = 0
        try:
            with driver.session() as session:
                for query in (
                    "CREATE CONSTRAINT news_signal_card_id IF NOT EXISTS FOR (n:NewsSignalCard) REQUIRE n.card_id IS UNIQUE",
                    "CREATE INDEX news_signal_target_id IF NOT EXISTS FOR (n:NewsSignalTarget) ON (n.target_id)",
                ):
                    try:
                        session.run(query).consume()
                    except Exception as exc:
                        logger.debug("News signal graph schema statement skipped: %s", exc)

                if touched_card_ids:
                    session.run(
                        """
                        MATCH ()-[r:NEWS_SIGNAL_SEMANTIC_SIMILARITY|NEWS_SIGNAL_EVENT_CLUE|NEWS_SIGNAL_TYPED_RELATION]->()
                        WHERE r.source_card_id IN $card_ids OR r.target_card_id IN $card_ids
                        DELETE r
                        """,
                        card_ids=sorted(touched_card_ids),
                    ).consume()

                for card in card_by_id.values():
                    props = _news_signal_card_node_props(card)
                    if not props["card_id"]:
                        continue
                    session.run(
                        """
                        MERGE (n:NewsSignalCard {card_id: $card_id})
                        SET n += $props
                        """,
                        card_id=props["card_id"],
                        props=props,
                    ).consume()

                for edge in edges:
                    source_id = str(edge.get("source_card_id") or "").strip()
                    target_id = str(edge.get("target_id") or "").strip()
                    edge_id = str(edge.get("edge_id") or "").strip()
                    if not source_id or not target_id or not edge_id:
                        continue
                    rel_type = _news_signal_relation_type(edge)
                    rel_props = _news_signal_edge_props(edge, group_id)
                    if edge.get("target_type") == "card":
                        target_card_id = str(edge.get("target_card_id") or target_id).strip()
                        target_props = _news_signal_card_node_props(card_by_id.get(target_card_id, {"card_id": target_card_id}))
                        session.run(
                            f"""
                            MERGE (s:NewsSignalCard {{card_id: $source_card_id}})
                            MERGE (t:NewsSignalCard {{card_id: $target_card_id}})
                            SET t += $target_props
                            MERGE (s)-[r:{rel_type} {{edge_id: $edge_id}}]->(t)
                            SET r += $rel_props
                            """,
                            source_card_id=source_id,
                            target_card_id=target_card_id,
                            target_props=target_props,
                            edge_id=edge_id,
                            rel_props=rel_props,
                        ).consume()
                    else:
                        session.run(
                            f"""
                            MERGE (s:NewsSignalCard {{card_id: $source_card_id}})
                            MERGE (t:NewsSignalTarget {{target_id: $target_id}})
                            SET t.target_type = $target_type,
                                t.label = $target_label,
                                t.updated_at = $updated_at
                            MERGE (s)-[r:{rel_type} {{edge_id: $edge_id}}]->(t)
                            SET r += $rel_props
                            """,
                            source_card_id=source_id,
                            target_id=target_id,
                            target_type=str(edge.get("target_type") or "entity"),
                            target_label=_news_signal_graph_label(target_id),
                            updated_at=datetime.now(timezone.utc).isoformat(),
                            edge_id=edge_id,
                            rel_props=rel_props,
                        ).consume()
                    projected += 1
        except Exception as exc:
            logger.warning("News signal edge graph projection failed: %s", exc, exc_info=True)
            return {"status": "failed", "error": str(exc), "edges": projected}
        finally:
            driver.close()

        return {
            "status": "ok",
            "group_id": group_id,
            "edges": projected,
            "cards": len(card_by_id),
        }

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

        return self._run_sync(self.search(query, market=market, limit=limit))

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            await close()
        if self._sync_loop is not None and self._sync_loop.is_running():
            self._sync_loop.call_soon_threadsafe(self._sync_loop.stop)


def get_graphiti_service() -> GraphitiService:
    global _service_instance
    if _service_instance is not None:
        return _service_instance
    with _service_lock:
        if _service_instance is None:
            _service_instance = GraphitiService()
    return _service_instance
