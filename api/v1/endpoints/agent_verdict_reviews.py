# -*- coding: utf-8 -*-
"""Agent verdict review endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from src.services.agent_verdict_review_service import DEFAULT_EVAL_WINDOWS, AgentVerdictReviewService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", summary="List Agent verdict review rows")
def list_agent_verdict_reviews(
    chain_type: str | None = Query(None, description="stock_selection | single_stock_analysis"),
    review_label: str | None = Query(None, description="hit | missed_up | avoided_down | wrong_direction | insufficient_data ..."),
    symbol: str | None = Query(None, description="Stock code"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    try:
        return AgentVerdictReviewService().query_reviews(
            chain_type=chain_type,
            review_label=review_label,
            symbol=symbol,
            limit=limit,
        )
    except Exception as exc:
        logger.error("查询 Agent verdict reviews 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/rebuild", summary="Rebuild Agent verdict review rows from local traces")
def rebuild_agent_verdict_reviews(
    windows: str = Query(
        ",".join(str(item) for item in DEFAULT_EVAL_WINDOWS),
        description="Comma-separated forward evaluation windows, e.g. 7,30",
    ),
    limit: int | None = Query(300, ge=1, le=2000, description="Newest trace count to scan; omit in CLI for full rebuild"),
) -> dict:
    eval_windows = _parse_windows(windows)
    try:
        result = AgentVerdictReviewService().build_and_write(
            eval_windows=eval_windows,
            limit=limit,
        )
        payload = result.to_dict()
        payload["eval_windows"] = list(eval_windows)
        return payload
    except Exception as exc:
        logger.error("重建 Agent verdict reviews 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


def _parse_windows(value: str) -> tuple[int, ...]:
    windows: list[int] = []
    for raw in str(value or "").split(","):
        text = raw.strip()
        if not text:
            continue
        try:
            window = int(text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_windows", "message": "windows must be comma-separated integers"}) from exc
        if window <= 0 or window > 365:
            raise HTTPException(status_code=400, detail={"error": "invalid_windows", "message": "windows must be between 1 and 365 days"})
        windows.append(window)
    if not windows:
        raise HTTPException(status_code=400, detail={"error": "invalid_windows", "message": "windows must not be empty"})
    return tuple(dict.fromkeys(windows))
