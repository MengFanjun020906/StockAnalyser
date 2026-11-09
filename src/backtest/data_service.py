# -*- coding: utf-8 -*-
"""Persisted precheck / minute-bar sync jobs for the backtest UI.

Data jobs never run inside the backtest kernel.  They are bounded background
tasks owned by the API process; the pure backtest worker has no provider access.
"""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from src.config import get_config
from src.repositories.backtest_run_repo import BacktestRunRepository
from src.repositories.stock_repo import StockRepository
from src.services.agent_entry_minute_data_service import _records_from_baostock_df
from src.storage import DatabaseManager

from .calendar import SHANGHAI
from .service import BacktestRunService, BacktestValidationError

logger = logging.getLogger(__name__)


class BacktestDataService:
    def __init__(
        self,
        *,
        db_manager: DatabaseManager | None = None,
        repository: BacktestRunRepository | None = None,
        run_service: BacktestRunService | None = None,
        inline: bool = False,
    ) -> None:
        self.config = get_config()
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = repository or BacktestRunRepository(self.db)
        self.run_service = run_service or BacktestRunService(
            db_manager=self.db,
            repository=self.repo,
        )
        self.inline = inline
        self._executor = None if inline else ThreadPoolExecutor(
            max_workers=max(1, int(getattr(self.config, "backtest_data_job_workers", 2) or 2)),
            thread_name_prefix="backtest-data",
        )

    # ------------------------------------------------------------------
    # Public job creation
    # ------------------------------------------------------------------
    def create_precheck_job(self, config) -> dict[str, Any]:
        job_id = f"dj-{datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {"config": config.model_dump(mode="json")}
        self.repo.create_data_job(
            job_id=job_id,
            job_type="precheck",
            request_json=json.dumps(payload, ensure_ascii=True),
        )
        self._submit(job_id, "precheck", payload)
        return self.get_job(job_id)

    def create_sync_job(
        self,
        *,
        symbols: list[str],
        start_date: date,
        end_date: date,
        frequency: str = "5",
        adjustflag: str = "3",
        config=None,
    ) -> dict[str, Any]:
        if start_date > end_date:
            raise BacktestValidationError("invalid_date_range", "start_date must not be after end_date")
        if frequency not in {"5", "15", "30", "60"}:
            raise BacktestValidationError("invalid_frequency", "frequency must be one of 5/15/30/60")
        if adjustflag not in {"1", "2", "3"}:
            raise BacktestValidationError("invalid_adjustflag", "adjustflag must be one of 1/2/3")
        job_id = f"dj-{datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "symbols": sorted({str(item).strip() for item in symbols if str(item).strip()}),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": frequency,
            "adjustflag": adjustflag,
            "config": config.model_dump(mode="json") if config is not None else None,
        }
        self.repo.create_data_job(
            job_id=job_id,
            job_type="sync_minute_bars",
            request_json=json.dumps(payload, ensure_ascii=True),
        )
        self._submit(job_id, "sync_minute_bars", payload)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_data_job(job_id)
        if job is None:
            raise BacktestValidationError("not_found", "data job not found")
        return job

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _submit(self, job_id: str, job_type: str, payload: dict[str, Any]) -> None:
        if self._executor is None:
            self._run_job(job_id, job_type, payload)
            return
        self._executor.submit(self._run_job, job_id, job_type, payload)

    def _run_job(self, job_id: str, job_type: str, payload: dict[str, Any]) -> None:
        worker_id = f"api:{job_id}"
        if not self.repo.start_data_job(job_id, worker_id=worker_id):
            return
        try:
            if job_type == "precheck":
                result = self._run_precheck(payload)
                status = "succeeded" if result.get("status") != "blocked" else "partial"
            elif job_type == "sync_minute_bars":
                result = self._run_sync(payload)
                status = "partial" if result.get("failed_symbols") else "succeeded"
            else:
                raise BacktestValidationError("unsupported_job", f"unsupported job type: {job_type}")
            self.repo.finish_data_job(
                job_id,
                status=status,
                result_json=json.dumps(result, ensure_ascii=True, default=str),
            )
        except Exception as exc:
            logger.exception("backtest data job failed: %s", exc)
            self.repo.finish_data_job(
                job_id,
                status="failed",
                error_code="data_job_failed",
                error_message=str(exc),
            )

    def _run_precheck(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .schemas import ExperimentConfig

        config = ExperimentConfig.model_validate(payload["config"])
        result = self.run_service.precheck(config)
        return result.model_dump(mode="json")

    def _run_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .schemas import ExperimentConfig

        symbols = list(payload.get("symbols") or [])
        config_payload = payload.get("config")
        if not symbols and config_payload:
            config = ExperimentConfig.model_validate(config_payload)
            if config.mode.value == "historical_agent_research" and config.research is not None:
                symbols = sorted(config.research.universe)
            else:
                from .plan_adapter import PlanAdapter

                adaptation = PlanAdapter(
                    config=config,
                    db_manager=self.db,
                    calendar=self.run_service.calendar,
                ).adapt()
                symbols = sorted({plan.symbol for plan in adaptation.plans})
        if not symbols:
            raise BacktestValidationError("no_symbols", "no symbols to sync")
        start_date = date.fromisoformat(payload["start_date"])
        end_date = date.fromisoformat(payload["end_date"])
        frequency = str(payload.get("frequency") or "5")
        adjustflag = str(payload.get("adjustflag") or "3")
        from data_provider.baostock_fetcher import BaostockFetcher

        fetcher = BaostockFetcher()
        stock_repo = StockRepository(self.db)
        items = []
        fetched_rows = 0
        written_rows = 0
        failed = 0
        for symbol in symbols:
            try:
                frame = fetcher.get_minute_k_data(
                    symbol,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    frequency=frequency,
                    adjustflag=adjustflag,
                )
                records = _records_from_baostock_df(
                    frame,
                    fallback_symbol=symbol,
                    frequency=frequency,
                    adjustflag=adjustflag,
                )
                written = stock_repo.save_minute_bars(records, data_source="BaostockMinute")
                fetched_rows += len(records)
                written_rows += written
                items.append(
                    {
                        "symbol": symbol,
                        "status": "ok" if records else "empty",
                        "fetched_rows": len(records),
                        "written_rows": written,
                    }
                )
            except Exception as exc:
                failed += 1
                items.append({"symbol": symbol, "status": "failed", "error": str(exc)})
        return {
            "symbols": len(symbols),
            "failed_symbols": failed,
            "fetched_rows": fetched_rows,
            "written_rows": written_rows,
            "frequency": frequency,
            "adjustflag": adjustflag,
            "items": items,
        }


__all__ = ["BacktestDataService"]
