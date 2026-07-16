# -*- coding: utf-8 -*-
"""Bounded, auditable Graphiti evidence for stock-selection stages."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from src.services.graphiti import get_graphiti_service


class StockSelectionGraphEvidence:
    """Collect compact graph evidence through one bounded search per stage."""

    def __init__(self, *, graphiti: Any = None, timeout_seconds: float = 12.0) -> None:
        self.graphiti = graphiti or get_graphiti_service()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or 12.0), 60.0))

    def collect_discovery(
        self,
        *,
        task: str,
        market: str,
        target_symbols: Iterable[str],
        limit: int = 12,
    ) -> Dict[str, Any]:
        symbols = [_normalize_code(value) for value in target_symbols]
        symbols = [value for value in symbols if value]
        query_parts = [str(task or "").strip(), "候选发现 活跃事件 历史分析 产业链"]
        if symbols:
            query_parts.append(" ".join(symbols[:8]))
        return self._collect(" ".join(part for part in query_parts if part), market=market, limit=limit)

    def collect_adversarial(
        self,
        *,
        candidate_codes: Iterable[str],
        market: str,
        limit: int = 15,
    ) -> Dict[str, Any]:
        codes = [_normalize_code(value) for value in candidate_codes]
        codes = [value for value in codes if value]
        query = " ".join([*codes[:8], "历史相似情形 失败案例 风险事件 信号失效 回撤"])
        return self._collect(query, market=market, limit=limit)

    def _collect(self, query: str, *, market: str, limit: int) -> Dict[str, Any]:
        try:
            result = self.graphiti.search_sync(
                query,
                market=market,
                limit=max(1, min(int(limit or 12), 20)),
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            result = {
                "success": False,
                "source": "graphiti",
                "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "episodes": [],
                "edges": [],
            }
        success = bool(result.get("success"))
        episodes = result.get("episodes") if isinstance(result.get("episodes"), list) else []
        edges = result.get("edges") if isinstance(result.get("edges"), list) else []
        compact_items = [_compact_item(item) for item in episodes[:20] if isinstance(item, dict)]
        compact_edges = [
            _compact_item(item)
            for item in edges[:20]
            if isinstance(item, dict) and str(item.get("quality_grade") or "medium") != "low"
        ]
        strong_edges = [item for item in compact_edges if not _is_weak_semantic_edge(item)]
        weak_edges = [item for item in compact_edges if _is_weak_semantic_edge(item)]
        by_code: Dict[str, List[Dict[str, Any]]] = {}
        for item in compact_items:
            for code in _codes_from_item(item):
                by_code.setdefault(code, []).append(item)
        return {
            "required": True,
            "status": "ok" if success and compact_items else ("partial" if success else "failed"),
            "source": str(result.get("source") or ("graphiti" if success else "unknown")),
            "degraded": bool(result.get("degraded", False)),
            "fallback_reason": result.get("fallback_reason"),
            "query": query,
            "items": compact_items,
            "strong_edges": strong_edges,
            "weak_edges": weak_edges,
            "edge_quality_summary": _edge_quality_summary(edges),
            "by_code": by_code,
            "error": result.get("error") or result.get("graphiti_error"),
            "guardrails": [
                "图谱证据只作为候选与风险上下文，不等于买入推荐。",
                "semantic_similarity 或低质量边不得被表述为因果关系。",
                "degraded=true 时必须披露关系库降级来源。",
            ],
        }


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    digits = "".join(char for char in text if char.isdigit())
    if 5 <= len(digits) <= 6:
        return digits.zfill(6)
    return text if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", text) else ""


def _codes_from_item(item: Dict[str, Any]) -> List[str]:
    codes: List[str] = []
    for value in (item.get("code"), item.get("stock_code"), item.get("symbol")):
        code = _normalize_code(value)
        if code and code not in codes:
            codes.append(code)
    for company in item.get("company_impacts") or []:
        if not isinstance(company, dict):
            continue
        code = _normalize_code(company.get("symbol") or company.get("code"))
        if code and code not in codes:
            codes.append(code)
    return codes


def _compact_item(item: Dict[str, Any]) -> Dict[str, Any]:
    allowed = (
        "type",
        "id",
        "card_id",
        "code",
        "stock_code",
        "name",
        "summary_short",
        "analysis_summary",
        "operation_advice",
        "trend_prediction",
        "edge_class",
        "edge_type",
        "signal_date",
        "signal_layer",
        "evidence_grade",
        "signal_score",
        "company_impacts",
        "primary_industries",
        "quality_grade",
        "edge_quality",
        "quality_flags",
        "rationale",
        "relevance_score",
        "target_id",
        "target_type",
        "target_card_id",
    )
    compact = {key: item.get(key) for key in allowed if item.get(key) not in (None, "", [], {})}
    if isinstance(compact.get("company_impacts"), list):
        compact["company_impacts"] = compact["company_impacts"][:5]
    return compact


def _is_weak_semantic_edge(item: Dict[str, Any]) -> bool:
    edge_class = str(item.get("edge_class") or "")
    edge_type = str(item.get("edge_type") or "")
    flags = item.get("quality_flags") if isinstance(item.get("quality_flags"), list) else []
    return edge_class == "semantic_similarity" or edge_type == "semantic_similarity" or "semantic_not_causal" in flags


def _edge_quality_summary(edges: List[Any]) -> Dict[str, Any]:
    total = 0
    skipped_low_quality = 0
    semantic_edges = 0
    grade_counts: Dict[str, int] = {}
    class_counts: Dict[str, int] = {}
    for item in edges:
        if not isinstance(item, dict):
            continue
        total += 1
        grade = str(item.get("quality_grade") or "unknown")
        edge_class = str(item.get("edge_class") or "unknown")
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        class_counts[edge_class] = class_counts.get(edge_class, 0) + 1
        if grade == "low":
            skipped_low_quality += 1
        if _is_weak_semantic_edge(item):
            semantic_edges += 1
    return {
        "edge_count": total,
        "skipped_low_quality_edges": skipped_low_quality,
        "semantic_similarity_edges": semantic_edges,
        "quality_grade_counts": grade_counts,
        "edge_class_counts": class_counts,
    }
