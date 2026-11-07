# -*- coding: utf-8 -*-
"""Kronos K-line pattern endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.kronos import KronosAnalyzeRequest, KronosAnalyzeResponse, KronosStatusResponse
from src.services.kronos_signal_service import KronosSignalService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/status",
    response_model=KronosStatusResponse,
    responses={500: {"description": "服务器错误", "model": ErrorResponse}},
    summary="获取 Kronos 集成状态",
    description="返回 Kronos 是否启用、模型路径、支持市场与频率。",
)
def get_kronos_status() -> KronosStatusResponse:
    try:
        return KronosStatusResponse(**KronosSignalService().status())
    except Exception as exc:
        logger.error("查询 Kronos 状态失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询 Kronos 状态失败: {exc}"},
        )


@router.post(
    "/signals/analyze",
    response_model=KronosAnalyzeResponse,
    responses={500: {"description": "服务器错误", "model": ErrorResponse}},
    summary="运行 Kronos K线形态分析",
    description="对单只 A 股日线执行 Kronos 预测，并返回形态实例、红框坐标、预测摘要和数据质量诊断。",
)
def analyze_kronos_signal(request: KronosAnalyzeRequest) -> KronosAnalyzeResponse:
    try:
        result = KronosSignalService().analyze(
            stock_code=request.stock_code,
            frequency="1d" if request.frequency == "daily" else request.frequency,
            lookback=request.lookback,
            pred_len=request.pred_len,
            target_date=request.target_date,
            fallback_to_network=request.fallback_to_network,
            persist=request.persist,
        )
        return KronosAnalyzeResponse(**result)
    except Exception as exc:
        logger.error("Kronos K线形态分析失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"Kronos K线形态分析失败: {exc}"},
        )
