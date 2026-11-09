# -*- coding: utf-8 -*-
"""Portfolio backtest run API.

Resource layout follows the design doc: `/api/v1/backtest/runs`, with separate
precheck / minute-bar sync jobs and per-run paginated detail tables.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.deps import get_database_manager
from api.v1.schemas.backtest_runs import (
    CreateBacktestRunRequest,
    DataJobResponse,
    FeePolicyResponse,
    PrecheckJobRequest,
    RiskPolicyPresetResponse,
    RunDetailResponse,
    RunListResponse,
    RunSummaryResponse,
    SyncMinuteBarsRequest,
)
from src.agent.pricing.repository import PricePlanRepository
from src.backtest.data_service import BacktestDataService
from src.backtest.fees import FEE_POLICIES
from src.backtest.schemas import RISK_POLICY_PRESETS
from src.backtest.service import (
    BacktestNotFoundError,
    BacktestRunService,
    BacktestValidationError,
)
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)
router = APIRouter()


def get_backtest_service(request: Request) -> BacktestRunService:
    service = getattr(request.app.state, "backtest_run_service", None)
    if service is None:
        service = BacktestRunService()
        request.app.state.backtest_run_service = service
    return service


def get_backtest_data_service(request: Request) -> BacktestDataService:
    service = getattr(request.app.state, "backtest_data_service", None)
    if service is None:
        service = BacktestDataService()
        request.app.state.backtest_data_service = service
    return service


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": code, "message": message, "detail": detail},
    )


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        **job,
        "request": job.get("request_json"),
        "result": job.get("result_json"),
    }


def _service_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BacktestNotFoundError:
        raise _error(404, "not_found", "backtest run not found")
    except BacktestValidationError as exc:
        status = 422 if exc.code in {
            "no_included_plans",
            "precheck_blocked",
            "invalid_date_range",
            "date_range_too_large",
            "too_many_plans",
            "no_symbols",
        } else 400
        raise _error(status, exc.code, exc.message, exc.details)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("backtest API failure: %s", exc)
        raise _error(500, "internal_error", str(exc))


@router.post(
    "/runs",
    response_model=RunSummaryResponse,
    status_code=202,
    summary="Create a portfolio backtest experiment",
)
def create_run(
    payload: CreateBacktestRunRequest,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(
        service.create_run,
        payload.config,
        allow_incomplete=payload.allow_incomplete,
    )


@router.get("/runs", response_model=RunListResponse, summary="List backtest experiments")
def list_runs(
    status: str | None = Query(None, description="queued|validating|running|succeeded|partial|failed|cancelled|interrupted"),
    mode: str | None = Query(None, description="recorded_plan_replay"),
    source_pipeline: str | None = Query(None, description="selection_confirmed|single_stock|any"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(
        service.list_runs,
        status=status,
        mode=mode,
        source_pipeline=source_pipeline,
        page=page,
        page_size=page_size,
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse, summary="Get backtest experiment detail")
def get_run(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_run_detail, run_id)


@router.get("/runs/{run_id}/metrics", summary="Get run metrics")
def get_run_metrics(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    run = _service_call(service.get_run, run_id)
    return {"run_id": run["run_id"], "items": run.get("metrics")}


@router.get("/runs/{run_id}/equity", summary="Get paginated equity curve")
def get_run_equity(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=500),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_equity, run_id, page=page, page_size=page_size)


@router.get("/runs/{run_id}/benchmarks", summary="Get display-only index benchmark curves")
def get_run_benchmarks(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_benchmarks, run_id)


@router.get("/runs/{run_id}/orders", summary="Get paginated orders")
def get_run_orders(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_orders, run_id, page=page, page_size=page_size)


@router.get("/runs/{run_id}/fills", summary="Get paginated fills")
def get_run_fills(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_fills, run_id, page=page, page_size=page_size)


@router.get("/runs/{run_id}/trades", summary="Get paginated closed trades")
def get_run_trades(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_trades, run_id, page=page, page_size=page_size)


@router.get("/runs/{run_id}/positions", summary="Get end-of-run positions")
def get_run_positions(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_positions, run_id)


@router.get("/runs/{run_id}/inputs", summary="Get included/excluded plan inputs")
def get_run_inputs(
    run_id: str,
    included: bool | None = Query(None),
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_inputs, run_id, included=included)


@router.get("/runs/{run_id}/diagnostics", summary="Get data/execution diagnostics")
def get_run_diagnostics(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.get_diagnostics, run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunSummaryResponse, summary="Cancel a run")
def cancel_run(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.cancel_run, run_id)


@router.post("/runs/{run_id}/rerun", response_model=RunSummaryResponse, status_code=202, summary="Create a new attempt")
def rerun(
    run_id: str,
    service: BacktestRunService = Depends(get_backtest_service),
) -> dict[str, Any]:
    return _service_call(service.rerun, run_id)


@router.get("/runs/{run_id}/export", summary="Export config/manifest/metrics/ledger/fills CSV")
def export_run(
    run_id: str,
    kind: str = Query(..., pattern="^(config|manifest|metrics|inputs|diagnostics|ledger|orders|fills|equity|trades)$"),
    service: BacktestRunService = Depends(get_backtest_service),
) -> StreamingResponse:
    filename, media_type, iterator = _service_call(service.export, run_id, kind)
    return StreamingResponse(
        iterator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/candidates", summary="List structured price-plan candidates for the create form")
def list_candidates(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    symbol: str | None = Query(None),
    limit: int = Query(300, ge=1, le=2000),
    db: DatabaseManager = Depends(get_database_manager),
) -> dict[str, Any]:
    try:
        repo = PricePlanRepository(db)
        artifacts = repo.list_artifacts(symbol=symbol, limit=limit)
        items = []
        for artifact in artifacts:
            if start_date and artifact.actual_as_of_session < start_date:
                continue
            if end_date and artifact.actual_as_of_session > end_date:
                continue
            decision = repo.get_latest_account_decision(artifact.plan_id) or {}
            trace_refs = []
            try:
                with db.get_session() as session:
                    from src.storage import TradePlanTraceLink
                    from sqlalchemy import select

                    trace_refs = [
                        row.trace_ref
                        for row in session.execute(
                            select(TradePlanTraceLink).where(TradePlanTraceLink.plan_id == artifact.plan_id)
                        ).scalars().all()
                    ]
            except Exception:
                trace_refs = []
            items.append(
                {
                    "plan_id": artifact.plan_id,
                    "revision": artifact.revision,
                    "symbol": artifact.symbol,
                    "actual_as_of_session": artifact.actual_as_of_session.isoformat(),
                    "generation_status": artifact.generation_status.value,
                    "data_quality": artifact.data_quality,
                    "scenario_count": len(artifact.scenarios),
                    "formal_scenarios": [
                        item.scenario_id
                        for item in artifact.scenarios
                        if item.role.value != "observation"
                    ],
                    "selected_scenario_id": decision.get("selected_scenario_id"),
                    "decision_status": decision.get("execution_status"),
                    "target_position_pct": decision.get("target_position_pct"),
                    "trace_refs": sorted(set(trace_refs)),
                    "plan_fingerprint": artifact.plan_fingerprint,
                }
            )
        return {"total": len(items), "items": items}
    except Exception as exc:
        logger.exception("list backtest candidates failed: %s", exc)
        raise _error(500, "internal_error", str(exc))


@router.get("/risk-policies", response_model=list[RiskPolicyPresetResponse], summary="List risk policy presets")
def list_risk_policies() -> list[dict[str, Any]]:
    result = []
    for profile, policy in RISK_POLICY_PRESETS.items():
        result.append(
            {
                "profile": profile.value,
                "policy_version": policy.policy_version,
                "max_single_position_pct": float(policy.max_single_position_pct),
                "max_total_exposure_pct": float(policy.max_total_exposure_pct),
                "max_concurrent_positions": policy.max_concurrent_positions,
                "min_cash_pct": float(policy.min_cash_pct),
                "risk_per_trade_pct": float(policy.risk_per_trade_pct),
                "require_stop_loss": policy.require_stop_loss,
                "selection_top_n": policy.selection_top_n,
            }
        )
    return result


@router.get("/fee-policies", response_model=list[FeePolicyResponse], summary="List fee policy versions")
def list_fee_policies() -> list[dict[str, Any]]:
    return [
        {
            "version": policy.version,
            "description": policy.description,
            "rules": [rule.model_dump(mode="json") for rule in policy.rules],
        }
        for policy in FEE_POLICIES.values()
    ]


@router.post("/prechecks", response_model=DataJobResponse, status_code=202, summary="Create data precheck job")
def create_precheck_job(
    payload: PrecheckJobRequest,
    service: BacktestDataService = Depends(get_backtest_data_service),
) -> dict[str, Any]:
    job = _service_call(service.create_precheck_job, payload.config)
    return _job_payload(job)


@router.get("/prechecks/{job_id}", response_model=DataJobResponse, summary="Get precheck job")
def get_precheck_job(
    job_id: str,
    service: BacktestDataService = Depends(get_backtest_data_service),
) -> dict[str, Any]:
    job = _service_call(service.get_job, job_id)
    return _job_payload(job)


@router.post("/minute-bars/sync", response_model=DataJobResponse, status_code=202, summary="Create minute-bar sync job")
def create_minute_sync_job(
    payload: SyncMinuteBarsRequest,
    service: BacktestDataService = Depends(get_backtest_data_service),
) -> dict[str, Any]:
    job = _service_call(
        service.create_sync_job,
        symbols=payload.symbols,
        start_date=payload.start_date,
        end_date=payload.end_date,
        frequency=payload.frequency,
        adjustflag=payload.adjustflag,
        config=payload.config,
    )
    return _job_payload(job)


@router.get("/data-jobs/{job_id}", response_model=DataJobResponse, summary="Get data job")
def get_data_job(
    job_id: str,
    service: BacktestDataService = Depends(get_backtest_data_service),
) -> dict[str, Any]:
    job = _service_call(service.get_job, job_id)
    return _job_payload(job)
