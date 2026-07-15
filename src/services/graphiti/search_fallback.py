# -*- coding: utf-8 -*-
"""Relational fallback for knowledge-graph searches."""

from __future__ import annotations

import re
from typing import Any, Optional

from src.repositories.news_signal_repo import NewsSignalRepository
from src.storage import DatabaseManager


class RelationalGraphSearch:
    """Search persisted analysis history and news cards when Graphiti is unavailable."""

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        news_repo: Optional[NewsSignalRepository] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.news_repo = news_repo or NewsSignalRepository(self.db)

    def search(
        self,
        query: str,
        *,
        market: str | None = None,
        limit: int = 10,
        reason: str = "graphiti_unavailable",
    ) -> dict[str, Any]:
        capped_limit = max(1, min(int(limit or 10), 50))
        terms = _query_terms(query)
        code = _stock_code(terms)
        candidates: list[dict[str, Any]] = []

        analyses = self.db.get_analysis_history(
            code=code or None,
            days=365,
            limit=max(50, capped_limit * 5),
        )
        for row in analyses:
            payload = row.to_dict()
            score = _relevance_score(
                query,
                terms,
                " ".join(
                    str(payload.get(key) or "")
                    for key in (
                        "code",
                        "name",
                        "report_type",
                        "analysis_summary",
                        "operation_advice",
                        "trend_prediction",
                        "news_content",
                    )
                ),
            )
            if score <= 0:
                continue
            candidates.append(
                {
                    "type": "analysis_history",
                    "id": payload.get("id"),
                    "code": payload.get("code"),
                    "name": payload.get("name"),
                    "report_type": payload.get("report_type"),
                    "analysis_summary": payload.get("analysis_summary"),
                    "operation_advice": payload.get("operation_advice"),
                    "trend_prediction": payload.get("trend_prediction"),
                    "sentiment_score": payload.get("sentiment_score"),
                    "created_at": payload.get("created_at"),
                    "relevance_score": score,
                }
            )

        cards = self.news_repo.list_cards(status="active", limit=max(100, capped_limit * 10))
        for card in cards:
            searchable = " ".join(
                [
                    str(card.get("summary_short") or ""),
                    " ".join(str(item) for item in card.get("primary_industries") or []),
                    " ".join(str(item) for item in card.get("secondary_industries") or []),
                    " ".join(str(item) for item in card.get("explicit_entities") or []),
                    " ".join(
                        f"{item.get('symbol', '')} {item.get('name', '')} {item.get('rationale', '')}"
                        for item in card.get("company_impacts") or []
                        if isinstance(item, dict)
                    ),
                ]
            )
            score = _relevance_score(query, terms, searchable)
            if score <= 0:
                continue
            candidates.append(
                {
                    "type": "news_signal_card",
                    "card_id": card.get("card_id"),
                    "signal_date": card.get("signal_date"),
                    "signal_layer": card.get("signal_layer"),
                    "summary_short": card.get("summary_short"),
                    "primary_industries": card.get("primary_industries") or [],
                    "company_impacts": (card.get("company_impacts") or [])[:5],
                    "evidence_grade": card.get("evidence_grade"),
                    "signal_score": card.get("signal_score"),
                    "relevance_score": score,
                }
            )

        episodes = sorted(
            candidates,
            key=lambda item: (
                float(item.get("relevance_score") or 0.0),
                str(item.get("created_at") or item.get("signal_date") or ""),
            ),
            reverse=True,
        )[:capped_limit]
        return {
            "success": True,
            "source": "relational_fallback",
            "degraded": True,
            "fallback_reason": reason,
            "query": query,
            "market": market or "cn",
            "edges": [],
            "nodes": _nodes_from_episodes(episodes),
            "episodes": episodes,
        }


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+", str(query or ""))
    return [term.casefold() for term in terms if len(term.strip()) >= 2]


def _stock_code(terms: list[str]) -> str:
    for term in terms:
        if re.fullmatch(r"\d{5,6}", term):
            return term
    return ""


def _relevance_score(query: str, terms: list[str], text: str) -> float:
    haystack = " ".join(str(text or "").casefold().split())
    compact_query = " ".join(str(query or "").casefold().split())
    score = 0.0
    if compact_query and compact_query in haystack:
        score += 8.0
    for term in terms:
        if term in haystack:
            score += 3.0 if re.fullmatch(r"\d{5,6}", term) else 2.0
    return score


def _nodes_from_episodes(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for item in episodes:
        if item.get("type") == "analysis_history":
            code = str(item.get("code") or "").strip()
            if code:
                nodes[f"stock:{code}"] = {
                    "id": f"stock:{code}",
                    "type": "stock",
                    "code": code,
                    "name": item.get("name"),
                }
            continue
        card_id = str(item.get("card_id") or "").strip()
        if card_id:
            nodes[f"card:{card_id}"] = {
                "id": card_id,
                "type": "news_signal_card",
                "label": item.get("summary_short"),
            }
        for company in item.get("company_impacts") or []:
            if not isinstance(company, dict):
                continue
            symbol = str(company.get("symbol") or "").strip()
            if symbol:
                nodes[f"stock:{symbol}"] = {
                    "id": f"stock:{symbol}",
                    "type": "stock",
                    "code": symbol,
                    "name": company.get("name"),
                }
    return list(nodes.values())
