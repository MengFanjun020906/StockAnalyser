# -*- coding: utf-8 -*-
"""
===================================
API v1 路由聚合
===================================

职责：
1. 聚合 v1 版本的所有 endpoint 路由
2. 统一添加 /api/v1 前缀
"""

from fastapi import APIRouter

from api.v1.endpoints import (
    agent,
    agent_entry_execution_backtests,
    agent_verdict_reviews,
    auth,
    backtest,
    backtest_runs,
    candidate_pool,
    kronos,
    news_signals,
    portfolio,
    seed_pool_quality,
    signal_preview,
    stocks,
    system_config,
    usage,
)

# 创建 v1 版本主路由
router = APIRouter(prefix="/api/v1")

router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Auth"]
)

router.include_router(
    agent.router,
    prefix="/agent",
    tags=["Agent"]
)

router.include_router(
    candidate_pool.router,
    prefix="/candidate-pool",
    tags=["CandidatePool"]
)

router.include_router(
    seed_pool_quality.router,
    prefix="/seed-pool-quality",
    tags=["SeedPoolQuality"]
)

router.include_router(
    agent_verdict_reviews.router,
    prefix="/agent-verdict-reviews",
    tags=["AgentVerdictReviews"]
)

router.include_router(
    signal_preview.router,
    prefix="/signal-preview",
    tags=["SignalPreview"]
)

router.include_router(
    agent_entry_execution_backtests.router,
    prefix="/agent-entry-execution-backtests",
    tags=["AgentEntryExecutionBacktests"]
)

router.include_router(
    news_signals.router,
    prefix="/news-signals",
    tags=["NewsSignals"]
)

router.include_router(
    kronos.router,
    prefix="/kronos",
    tags=["Kronos"]
)

router.include_router(
    stocks.router,
    prefix="/stocks",
    tags=["Stocks"]
)

router.include_router(
    backtest.router,
    prefix="/backtest",
    tags=["Backtest"]
)

router.include_router(
    backtest_runs.router,
    prefix="/backtest",
    tags=["BacktestRuns"]
)

router.include_router(
    system_config.router,
    prefix="/system",
    tags=["SystemConfig"]
)

router.include_router(
    usage.router,
    prefix="/usage",
    tags=["Usage"]
)

router.include_router(
    portfolio.router,
    prefix="/portfolio",
    tags=["Portfolio"]
)
