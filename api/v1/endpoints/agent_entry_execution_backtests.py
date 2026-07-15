# -*- coding: utf-8 -*-
"""Agent entry-execution backtest endpoints."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from src.services.agent_entry_minute_data_service import AgentEntryMinuteDataService
from src.services.agent_entry_execution_backtest_service import AgentEntryExecutionBacktestService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", summary="List Agent entry-execution backtest rows")
def list_agent_entry_execution_backtests(
    strategy: str | None = Query(None, description="strict_ai_entry | next_open_baseline | atr_elastic_entry | breakout_fallback_entry"),
    symbol: str | None = Query(None, description="Stock code"),
    decision_date: date | None = Query(None, description="Decision date, YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    try:
        return AgentEntryExecutionBacktestService().query_backtests(
            strategy=strategy,
            symbol=symbol,
            decision_date=decision_date.isoformat() if decision_date else None,
            page=page,
            page_size=page_size,
            limit=limit,
        )
    except Exception as exc:
        logger.error("查询 Agent entry execution backtests 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/rebuild", summary="Rebuild Agent entry-execution backtests from local traces")
def rebuild_agent_entry_execution_backtests(
    limit: int | None = Query(300, ge=1, le=2000, description="Newest trace count to scan"),
) -> dict:
    try:
        result = AgentEntryExecutionBacktestService().build_and_write(limit=limit)
        return result.to_dict()
    except Exception as exc:
        logger.error("重建 Agent entry execution backtests 失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})


@router.post("/minute-bars/sync", summary="Sync historical baostock minute bars through the latest date")
def sync_agent_entry_execution_minute_bars(
    limit: int | None = Query(
        None,
        ge=1,
        le=2000,
        description="Newest trace count to scan; omitted scans all traces",
    ),
    decision_date: date | None = Query(None, description="Optional targeted sync for one decision date"),
    symbol: str | None = Query(None, description="Only sync this stock code"),
    frequency: str = Query("5", pattern="^(5|15|30|60)$", description="Baostock minute frequency"),
    adjustflag: str = Query("3", pattern="^[123]$", description="1 后复权，2 前复权，3 不复权"),
    rebuild: bool = Query(True, description="Rebuild JSONL after minute-bar sync"),
) -> dict:
    try:
        sync_result = AgentEntryMinuteDataService().sync_for_latest_reports(
            limit=limit,
            decision_date=decision_date,
            symbol=symbol,
            frequency=frequency,
            adjustflag=adjustflag,
        )
        payload = {"sync": sync_result.to_dict()}
        if rebuild:
            payload["rebuild"] = AgentEntryExecutionBacktestService().build_and_write(limit=limit).to_dict()
        return payload
    except Exception as exc:
        logger.error("同步 Agent entry execution 分钟线失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": str(exc)})
