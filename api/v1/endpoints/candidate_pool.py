# -*- coding: utf-8 -*-
"""Agent candidate-pool endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.agent.candidate_pool_store import CandidatePoolStore

logger = logging.getLogger(__name__)

router = APIRouter()


def _store() -> CandidatePoolStore:
    return CandidatePoolStore()


@router.get(
    "/runs",
    summary="获取候选池运行列表",
    description="返回最近的 Agent L1 候选池运行记录，用于独立候选池页面选择历史批次。",
)
def list_candidate_pool_runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    try:
        return {"runs": _store().list_runs(limit=limit)}
    except Exception as exc:
        logger.error("查询候选池运行列表失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询候选池运行列表失败: {exc}"},
        )


@router.get(
    "/latest",
    summary="获取最新候选池",
    description="返回最新一轮 Agent L1 候选池、质量摘要和硬排除诊断。",
)
def get_latest_candidate_pool() -> dict:
    try:
        latest = _store().get_latest()
        if latest is None:
            return {"run": None, "items": [], "quality": {}, "hard_exclusion": {}, "summary": {}}
        return latest
    except Exception as exc:
        logger.error("查询最新候选池失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询最新候选池失败: {exc}"},
        )


@router.get(
    "/runs/{run_id}",
    summary="获取指定候选池",
    description="按 run_id 返回指定 Agent L1 候选池明细。",
)
def get_candidate_pool_run(run_id: str) -> dict:
    try:
        result = _store().get_run(run_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "not_found", "message": f"未找到候选池运行 {run_id}"},
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("查询候选池运行失败: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"查询候选池运行失败: {exc}"},
        )
