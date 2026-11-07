# -*- coding: utf-8 -*-
"""Read-only signal preview endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.services.signal_evaluation_service import SignalEvaluationService
from src.services.signal_lifecycle_service import SignalLifecycleService


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", summary="List entry signals, position watches, and scheduler state")
def list_signal_preview(
    status: Optional[str] = Query(None, description="Lifecycle status or all"),
    symbol: Optional[str] = Query(None, description="Optional entry-signal stock code"),
) -> dict:
    try:
        return SignalLifecycleService().list_preview(status=status, symbol=symbol)
    except Exception as exc:
        logger.error("查询信号预览失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/evaluation", summary="Read-only shadow evaluation of persisted entry signals")
def get_signal_evaluation(
    limit: int = Query(300, ge=1, le=2000, description="Maximum number of persisted signals to evaluate"),
    setup_type: Optional[str] = Query(None, description="Optional setup type filter"),
) -> dict:
    try:
        return SignalEvaluationService().evaluate_all(limit=limit, setup_type=setup_type)
    except Exception as exc:
        logger.error("查询信号评估失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/entries/{signal_id}", summary="Get one entry signal with full local K-line chart data")
def get_entry_signal_preview(
    signal_id: str,
    refresh_tail: bool = Query(True, description="Fill only the missing recent K-line tail and persist it"),
) -> dict:
    try:
        payload = SignalLifecycleService().entry_chart(signal_id, refresh_tail=refresh_tail)
    except Exception as exc:
        logger.error("查询入场信号详情失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    if payload is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "entry signal not found"})
    return payload


@router.get("/plans/{plan_id}", summary="Get one deterministic price plan with full local K-line chart data")
def get_price_plan_preview(
    plan_id: str,
    refresh_tail: bool = Query(True, description="Fill only the missing recent K-line tail and persist it"),
) -> dict:
    try:
        payload = SignalLifecycleService().price_plan_chart(plan_id, refresh_tail=refresh_tail)
    except Exception as exc:
        logger.error("查询确定性价格计划详情失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    if payload is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "price plan not found"})
    return payload


@router.get("/positions/{watch_id}", summary="Get one holding watch with full local K-line chart data")
def get_position_watch_preview(
    watch_id: str,
    refresh_tail: bool = Query(True, description="Fill only the missing recent K-line tail and persist it"),
) -> dict:
    try:
        payload = SignalLifecycleService().position_chart(watch_id, refresh_tail=refresh_tail)
    except Exception as exc:
        logger.error("查询持仓信号详情失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
    if payload is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "position watch not found"})
    return payload


__all__ = ["router"]
