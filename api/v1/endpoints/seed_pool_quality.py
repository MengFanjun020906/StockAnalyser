# -*- coding: utf-8 -*-
"""Seed Pool quality monitoring endpoints."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from src.repositories.seed_pool_quality_repo import SeedPoolQualityRepository
from src.services.seed_pool_quality_service import SeedPoolEvaluationPreconditionError, SeedPoolQualityService

logger = logging.getLogger(__name__)
router = APIRouter()


def _repo() -> SeedPoolQualityRepository:
    return SeedPoolQualityRepository()


def _service() -> SeedPoolQualityService:
    return SeedPoolQualityService(repo=_repo())


def _parse_seed_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_seed_date", "message": "seed_date 必须是 YYYY-MM-DD"},
        )


@router.get("/dates", summary="获取 Seed Pool 质量日期列表")
def list_seed_pool_quality_dates(limit: int = Query(60, ge=1, le=200)) -> dict:
    try:
        return {"dates": _repo().list_dates(limit=limit)}
    except Exception as exc:
        logger.error("查询 Seed Pool 质量日期失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("", summary="按 seed_date 获取 Seed Pool 质量总览")
def get_seed_pool_quality(seed_date: str = Query(..., description="YYYY-MM-DD")) -> dict:
    try:
        result = _repo().get_quality_by_date(_parse_seed_date(seed_date))
        if not result.get("snapshot"):
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"未找到 {seed_date} 的 Seed Pool 快照"})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询 Seed Pool 质量失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/snapshots/{snapshot_id}", summary="获取 Seed Pool 快照详情")
def get_seed_pool_snapshot(snapshot_id: int) -> dict:
    try:
        result = _repo().get_snapshot(snapshot_id)
        if not result.get("snapshot"):
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"未找到快照 {snapshot_id}"})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询 Seed Pool 快照失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/items/{item_id}", summary="获取 Seed Pool 单票详情")
def get_seed_pool_item(item_id: int) -> dict:
    try:
        result = _repo().get_item_detail(item_id)
        if not result:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"未找到 Seed item {item_id}"})
        return {"item": result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询 Seed item 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.get("/items/{item_id}/chart-data", summary="获取 Seed Pool 单票 K 线与席位价格线")
def get_seed_pool_item_chart_data(item_id: int) -> dict:
    try:
        result = _service().chart_data(item_id)
        if not result:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"未找到 Seed item {item_id}"})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询 Seed item chart-data 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/evaluate", summary="按需评估 Seed Pool 后验表现")
def evaluate_seed_pool(seed_date: str = Query(..., description="YYYY-MM-DD"), limit: int = Query(500, ge=1, le=1000)) -> dict:
    try:
        return _service().evaluate_seed_date(_parse_seed_date(seed_date), limit=limit)
    except SeedPoolEvaluationPreconditionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error": exc.error,
                "message": exc.message,
                "details": exc.details,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("评估 Seed Pool 质量失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
