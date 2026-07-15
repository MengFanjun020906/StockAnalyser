# -*- coding: utf-8 -*-
"""News signal card endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.services.news_signal_service import NewsSignalService
from src.repositories.graphiti_outbox_repo import GraphitiOutboxRepository
from src.services.graphiti.outbox_worker import GraphitiOutboxWorker

logger = logging.getLogger(__name__)
router = APIRouter()


class NewsSignalFeedbackRequest(BaseModel):
    feedback_type: str = Field(..., description="useful | wrong | noisy | duplicate | adjust_industries | remove_company | note")
    note: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    user_id: str = ""


@router.get("", summary="List news signal cards")
def list_news_signals(
    signal_date: str = Query("", description="YYYY-MM-DD"),
    signal_layer: str = Query("", description="industry | company | macro"),
    industry: str = Query("", description="Industry/theme filter"),
    horizon: str = Query("", description="short | medium | long"),
    status: str = Query("", description="active | suppressed | pending"),
    limit: int = Query(120, ge=1, le=500),
) -> dict:
    try:
        return NewsSignalService().list_cards(
            signal_date=signal_date,
            signal_layer=signal_layer,
            industry=industry,
            horizon=horizon,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        logger.error("查询新闻信号卡片失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/metrics", summary="Get news signal card metrics")
def get_news_signal_metrics(
    signal_date: str = Query("", description="YYYY-MM-DD"),
) -> dict:
    try:
        return NewsSignalService().metrics(signal_date=signal_date)
    except Exception as exc:
        logger.error("查询新闻信号指标失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/rebuild", summary="Rebuild news signal cards from configured news sources")
def rebuild_news_signals(
    target_date: str = Query("", description="YYYY-MM-DD; defaults to effective CN trading date"),
    include_cjzc: bool = Query(True, description="Include Eastmoney 财经早餐 source"),
    include_cls: bool = Query(True, description="Include CLS telegraph source"),
    include_xueqiu: bool = Query(True, description="Include Xueqiu hot-list source"),
    include_macro_finance: bool = Query(True, description="Include macro-finance source"),
    cls_limit: int = Query(50, ge=1, le=50),
    xueqiu_limit: int = Query(30, ge=1, le=50),
    macro_finance_limit: int = Query(30, ge=1, le=50),
    sync_graphiti: bool = Query(False, description="Best-effort sync rebuilt cards into Graphiti"),
    include_semantic_edges: bool = Query(False, description="Build embedding semantic edges during rebuild when embedding is configured"),
) -> dict:
    try:
        return NewsSignalService().rebuild(
            target_date=target_date,
            include_cjzc=include_cjzc,
            include_cls=include_cls,
            include_xueqiu=include_xueqiu,
            include_macro_finance=include_macro_finance,
            cls_limit=cls_limit,
            xueqiu_limit=xueqiu_limit,
            macro_finance_limit=macro_finance_limit,
            sync_graphiti=sync_graphiti,
            include_semantic_edges=include_semantic_edges,
        )
    except Exception as exc:
        logger.error("重建新闻信号卡片失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/graph-sync", summary="Synchronize pending news signal cards into Graphiti")
def sync_news_signals_to_graphiti(
    signal_date: str = Query("", description="YYYY-MM-DD; empty means pending cards across dates"),
    limit: int = Query(100, ge=1, le=500),
    include_semantic_edges: bool = Query(False, description="Rebuild embedding semantic edges before graph projection"),
    include_episodes: bool = Query(False, description="Also run slow Graphiti episode extraction; explicit edge projection is always attempted"),
) -> dict:
    try:
        return NewsSignalService().sync_graphiti(
            signal_date=signal_date,
            limit=limit,
            include_semantic_edges=include_semantic_edges,
            include_episodes=include_episodes,
        )
    except Exception as exc:
        logger.error("同步新闻信号卡片到 Graphiti 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/edges", summary="List deterministic news signal edges")
def list_news_signal_edges(
    card_id: str = Query("", description="Optional source/target card id"),
    signal_date: str = Query("", description="YYYY-MM-DD"),
    edge_class: str = Query("", description="semantic_similarity | event_clue | typed_relation"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    try:
        return NewsSignalService().list_edges(
            card_id=card_id,
            signal_date=signal_date,
            edge_class=edge_class,
            limit=limit,
        )
    except Exception as exc:
        logger.error("查询新闻信号边失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/edges/rebuild", summary="Rebuild deterministic news signal edges")
def rebuild_news_signal_edges(
    signal_date: str = Query("", description="YYYY-MM-DD; empty means latest active cards by score"),
    limit: int = Query(160, ge=2, le=500),
    include_semantic: bool = Query(False, description="Use configured embedding model to build semantic_similarity edges"),
) -> dict:
    try:
        return NewsSignalService().rebuild_edges(
            signal_date=signal_date,
            limit=limit,
            include_semantic=include_semantic,
        )
    except Exception as exc:
        logger.error("重建新闻信号边失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/outcomes/refresh", summary="Refresh news signal outcomes from seed-pool evaluations")
def refresh_news_signal_outcomes() -> dict:
    try:
        return NewsSignalService().refresh_outcomes()
    except Exception as exc:
        logger.error("刷新新闻信号 outcome 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/events/backfill", summary="Backfill structured events from persisted news cards and raw episodes")
def backfill_news_signal_events(
    signal_date: str = Query("", description="YYYY-MM-DD; empty scans recent cards across dates"),
    limit: int = Query(500, ge=1, le=500),
    only_missing: bool = Query(True, description="Skip cards that already have extracted events"),
) -> dict:
    try:
        return NewsSignalService().backfill_extracted_events(
            signal_date=signal_date,
            limit=limit,
            only_missing=only_missing,
        )
    except Exception as exc:
        logger.error("回填新闻结构化事件失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/mapping-repair", summary="Remove company mappings not explicitly named in source news")
def repair_news_signal_company_mappings(
    signal_date: str = Query("", description="YYYY-MM-DD; empty scans recent cards across dates"),
    limit: int = Query(500, ge=1, le=500),
) -> dict:
    try:
        return NewsSignalService().repair_company_mapping_gates(
            signal_date=signal_date,
            limit=limit,
        )
    except Exception as exc:
        logger.error("修复新闻公司映射失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/clusters/reconcile", summary="Merge strong same-event card clusters")
def reconcile_news_signal_clusters(
    signal_date: str = Query("", description="YYYY-MM-DD; empty scans recent active cards"),
    limit: int = Query(500, ge=1, le=500),
) -> dict:
    try:
        return NewsSignalService().reconcile_same_event_clusters(
            signal_date=signal_date,
            limit=limit,
        )
    except Exception as exc:
        logger.error("归并新闻同事件卡片失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/graph-outbox/metrics", summary="Get durable Graphiti outbox metrics")
def get_graphiti_outbox_metrics() -> dict:
    return GraphitiOutboxRepository().metrics()


@router.post("/graph-outbox/run", summary="Process one bounded Graphiti outbox batch")
def run_graphiti_outbox(
    limit: int = Query(10, ge=1, le=100),
) -> dict:
    try:
        return GraphitiOutboxWorker().run_once(limit=limit)
    except Exception as exc:
        logger.error("执行 Graphiti outbox 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/{card_id}/graph", summary="Get one card's deterministic relation graph")
def get_news_signal_graph(
    card_id: str,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    try:
        return NewsSignalService().card_graph(card_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)})
    except Exception as exc:
        logger.error("查询新闻信号关系图失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/{card_id}", summary="Get one news signal card")
def get_news_signal(card_id: str) -> dict:
    try:
        card = NewsSignalService().get_card(card_id)
        if not card:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"news signal card not found: {card_id}"})
        return card
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询新闻信号卡片详情失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/{card_id}/evidence", summary="Adapt one news signal card to EvidenceCard")
def get_news_signal_evidence(
    card_id: str,
    symbol: str = Query("", description="Optional stock code"),
    name: str = Query("", description="Optional stock name"),
) -> dict:
    try:
        return NewsSignalService().evidence_card_for(card_id, symbol=symbol, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(exc)})
    except Exception as exc:
        logger.error("转换新闻信号 EvidenceCard 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/{card_id}/feedback", summary="Add feedback overlay for one news signal card")
def add_news_signal_feedback(card_id: str, request: NewsSignalFeedbackRequest) -> dict:
    try:
        return NewsSignalService().add_feedback(
            card_id=card_id,
            feedback_type=request.feedback_type,
            note=request.note,
            payload=request.payload,
            user_id=request.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_feedback", "message": str(exc)})
    except Exception as exc:
        logger.error("写入新闻信号反馈失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
