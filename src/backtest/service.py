# -*- coding: utf-8 -*-
"""Application service for portfolio backtest runs and read models."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from src.config import get_config
from src.repositories.backtest_run_repo import BacktestRunRepository
from src.storage import DatabaseManager

from .calendar import CnTradingCalendar, SHANGHAI
from .plan_adapter import PlanAdapter, PlanAdaptationResult
from .runner import EngineIdentity, build_request_fingerprint, get_engine_identity
from .schemas import ExperimentConfig, PrecheckResult, RunStatus
from .snapshot import SnapshotBuilder

logger = logging.getLogger(__name__)

RUN_ID_PATTERN = re.compile(r"^bt-[A-Za-z0-9-]{8,80}$")
MAX_PAGE_SIZE = 500
EXPORT_KINDS = {
    "config": "json",
    "manifest": "json",
    "metrics": "json",
    "inputs": "json",
    "diagnostics": "json",
    "ledger": "csv",
    "orders": "csv",
    "fills": "csv",
    "equity": "csv",
    "trades": "csv",
}


class BacktestNotFoundError(LookupError):
    pass


class BacktestValidationError(ValueError):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class BacktestRunService:
    def __init__(
        self,
        *,
        db_manager: DatabaseManager | None = None,
        repository: BacktestRunRepository | None = None,
        calendar: CnTradingCalendar | None = None,
        engine_identity: EngineIdentity | None = None,
        runs_root_override: Path | str | None = None,
        research_llm_adapter: Any | None = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.repo = repository or BacktestRunRepository(self.db)
        self.calendar = calendar or CnTradingCalendar()
        self.engine_identity = engine_identity or get_engine_identity()
        self.config = get_config()
        self._runs_root_override = Path(runs_root_override).expanduser().resolve() if runs_root_override else None
        self.research_llm_adapter = research_llm_adapter

    # ------------------------------------------------------------------
    # Paths / IDs
    # ------------------------------------------------------------------
    def runs_root(self) -> Path:
        if self._runs_root_override is not None:
            return self._runs_root_override
        configured = str(getattr(self.config, "backtest_runs_dir", "") or "").strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = Path(self.config.database_path).expanduser().resolve().parent / path
            return path.resolve()
        return Path(self.config.database_path).expanduser().resolve().parent / "backtest_runs"

    def generate_run_id(self) -> str:
        now = datetime.now(SHANGHAI).strftime("%Y%m%d%H%M%S")
        return f"bt-{now}-{uuid.uuid4().hex[:8]}"

    def _validate_run_id(self, run_id: str) -> str:
        if not RUN_ID_PATTERN.match(str(run_id or "")):
            raise BacktestValidationError("invalid_run_id", "invalid run id")
        return str(run_id)

    def _run_dir(self, run_id: str) -> Path:
        root = self.runs_root()
        candidate = (root / self._validate_run_id(run_id)).resolve()
        if root != candidate and root not in candidate.parents:
            raise BacktestValidationError("invalid_run_id", "run path escapes configured root")
        return candidate

    def _artifact_dir(self, row: dict[str, Any]) -> Path:
        result_dir = row.get("result_dir")
        if not result_dir:
            return self._run_dir(row["run_id"])
        root = self.runs_root()
        candidate = Path(result_dir).expanduser().resolve()
        if root != candidate and root not in candidate.parents:
            raise BacktestValidationError("invalid_result_dir", "result path escapes configured root")
        return candidate

    # ------------------------------------------------------------------
    # Validation / precheck
    # ------------------------------------------------------------------
    def _validate_config(self, config: ExperimentConfig) -> None:
        if not bool(getattr(self.config, "backtest_enabled", True)):
            raise BacktestValidationError("backtest_disabled", "backtest is disabled by configuration")
        if config.start_date > config.end_date:
            raise BacktestValidationError("invalid_date_range", "start_date must not be after end_date")
        max_days = int(getattr(self.config, "backtest_max_date_range_days", 3650) or 3650)
        if (config.end_date - config.start_date).days > max_days:
            raise BacktestValidationError(
                "date_range_too_large",
                f"date range exceeds {max_days} days",
            )
        if len(config.plan_ids) > int(getattr(self.config, "backtest_max_plans", 2000) or 2000):
            raise BacktestValidationError("too_many_plans", "plan_ids exceeds configured maximum")

    def _build_precheck(
        self,
        config: ExperimentConfig,
        adaptation: PlanAdaptationResult | None = None,
    ) -> PrecheckResult:
        if config.mode.value == "historical_agent_research":
            return self._build_research_precheck(config)
        adaptation = adaptation or PlanAdapter(
            config=config,
            db_manager=self.db,
            calendar=self.calendar,
        ).adapt()
        if not adaptation.plans:
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                included_plans=0,
                excluded_plans=adaptation.excluded_count,
                inputs=adaptation.inputs,
                blocking_reasons=["no_included_plans"],
                warnings=[],
                diagnostics=adaptation.diagnostics,
            )
        builder = SnapshotBuilder(
            run_id="precheck",
            config=config,
            plans=adaptation.plans,
            calendar=self.calendar,
            db_manager=self.db,
        )
        try:
            snapshot = builder.build()
        except Exception as exc:
            logger.warning("backtest precheck snapshot failed: %s", exc)
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                included_plans=adaptation.included_count,
                excluded_plans=adaptation.excluded_count,
                inputs=adaptation.inputs,
                blocking_reasons=[f"snapshot_failed:{exc}"],
            )
        return snapshot.to_precheck(
            inputs=adaptation.inputs,
            config_hash=_config_hash(config),
            generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
            diagnostics=adaptation.diagnostics,
        )

    def _build_research_precheck(self, config: ExperimentConfig) -> PrecheckResult:
        from .research.orchestrator import ResearchOrchestrator

        research = config.research
        if research is None:
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                blocking_reasons=["research_config_missing"],
            )
        from .research.dataset import (
            build_research_dataset,
            resolve_research_universe,
        )
        from .research.universe import ResearchUniverseError

        try:
            universe, universe_source = resolve_research_universe(
                config,
                db_manager=self.db,
            )
        except ResearchUniverseError as exc:
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                blocking_reasons=[f"research_universe_failed:{exc}"],
                diagnostics={"research": {"universe_mode": research.universe_mode.value}},
            )
        if not universe:
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                blocking_reasons=["research_universe_empty"],
                diagnostics={"research": {"universe_mode": research.universe_mode.value}},
            )
        try:
            dataset = build_research_dataset(
                config,
                calendar=self.calendar,
                universe=universe,
                db_manager=self.db,
                created_at=datetime.now(SHANGHAI).replace(tzinfo=None),
            )
            snapshot = dataset.snapshot
        except Exception as exc:
            logger.warning("research precheck snapshot failed: %s", exc)
            return PrecheckResult(
                config_hash=_config_hash(config),
                generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                status="blocked",
                symbols=universe,
                blocking_reasons=[f"research_snapshot_failed:{exc}"],
            )
        orchestrator = ResearchOrchestrator(
            config=config,
            data=snapshot.data,
            calendar=self.calendar,
            cache_path=None,
            llm_adapter=self.research_llm_adapter,
            universe=universe,
            universe_source=dataset.universe_source,
        )
        try:
            blockers, warnings, diagnostics = orchestrator.research_precheck(snapshot)
            warnings = list(warnings) + list(dataset.warnings)
            research_diag = diagnostics.setdefault("research", {})
            research_diag["data_source"] = dataset.data_source
            research_diag["universe_source"] = dataset.universe_source
            if dataset.coverage is not None:
                research_diag["data_coverage"] = {
                    "symbol_count": dataset.coverage.symbol_count,
                    "min_date": dataset.coverage.min_date.isoformat() if dataset.coverage.min_date else None,
                    "max_date": dataset.coverage.max_date.isoformat() if dataset.coverage.max_date else None,
                    "last_completed_date": (
                        dataset.coverage.last_completed_date.isoformat()
                        if dataset.coverage.last_completed_date
                        else None
                    ),
                    "price_basis": dataset.coverage.price_basis,
                }
        finally:
            orchestrator.close()
        base = snapshot.to_precheck(
            inputs=[],
            config_hash=_config_hash(config),
            generated_at=datetime.now(SHANGHAI).replace(tzinfo=None),
            diagnostics=diagnostics,
            require_included_plans=False,
        )
        merged_blockers = sorted(set(base.blocking_reasons) | set(blockers))
        merged_warnings = sorted(set(base.warnings) | set(warnings))
        status = "blocked" if merged_blockers else ("warning" if merged_warnings else "ok")
        return base.model_copy(
            update={
                "status": status,
                "blocking_reasons": merged_blockers,
                "warnings": merged_warnings,
            }
        )

    def precheck(self, config: ExperimentConfig) -> PrecheckResult:
        self._validate_config(config)
        return self._build_precheck(config)

    # ------------------------------------------------------------------
    # Create / list / detail
    # ------------------------------------------------------------------
    def create_run(
        self,
        config: ExperimentConfig,
        *,
        allow_incomplete: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        self._validate_config(config)
        is_research = config.mode.value == "historical_agent_research"
        if is_research:
            adaptation = PlanAdaptationResult(plans=[], inputs=[], diagnostics={})
        else:
            adaptation = PlanAdapter(
                config=config,
                db_manager=self.db,
                calendar=self.calendar,
            ).adapt()
            if not adaptation.plans:
                raise BacktestValidationError(
                    "no_included_plans",
                    "no machine-executable plans after input validation",
                    details={
                        "inputs": [item.model_dump(mode="json") for item in adaptation.inputs],
                        "diagnostics": adaptation.diagnostics,
                    },
                )
        request_fingerprint = build_request_fingerprint(
            config,
            adaptation.plans,
            identity=self.engine_identity,
            calendar_version=self.calendar.version,
        )
        if not force:
            active = self.repo.find_active_by_request_fingerprint(request_fingerprint)
            if active is not None:
                return {
                    **self._summary_from_row(active, config=None),
                    "deduplicated": True,
                }
        precheck = None
        if not allow_incomplete:
            precheck = self._build_precheck(config, adaptation)
            if precheck.status == "blocked":
                raise BacktestValidationError(
                    "precheck_blocked",
                    "backtest data precheck blocked the run",
                    details=precheck.model_dump(mode="json"),
                )
        run_id = self.generate_run_id()
        row = self.repo.create_run(
            run_id=run_id,
            mode=config.mode.value,
            status=RunStatus.QUEUED.value,
            request_fingerprint=request_fingerprint,
            config_json=config.model_dump_json(),
            engine_version=self.engine_identity.engine_version,
            git_revision=self.engine_identity.git_revision,
            git_dirty=self.engine_identity.git_dirty,
            plan_hashes=sorted(plan.fingerprint() for plan in adaptation.plans),
            source_pipeline=config.source_pipeline.value,
            risk_profile=config.risk_profile.value,
            worker_kind="research" if is_research else "standard",
        )
        self.repo.save_inputs(run_id, adaptation.inputs)
        result = self._summary_from_row(row, config=config)
        result["deduplicated"] = False
        if precheck is not None:
            result["precheck_status"] = precheck.status
            result["precheck_warnings"] = precheck.warnings
        return result

    def list_runs(
        self,
        *,
        status: str | None = None,
        mode: str | None = None,
        source_pipeline: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
        payload = self.repo.list_runs(
            status=status,
            mode=mode,
            source_pipeline=source_pipeline,
            page=page,
            page_size=page_size,
        )
        payload["items"] = [self._summary_from_row(row, config=None) for row in payload["items"]]
        return payload

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_run(self._validate_run_id(run_id))
        if row is None:
            raise BacktestNotFoundError("run not found")
        return self._summary_from_row(row, config=None)

    def get_run_detail(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_run(self._validate_run_id(run_id))
        if row is None:
            raise BacktestNotFoundError("run not found")
        summary = self._summary_from_row(row, config=None)
        run_dir = self._artifact_dir(row)
        diagnostics = {}
        if (run_dir / "diagnostics.json").exists():
            try:
                diagnostics = json.loads((run_dir / "diagnostics.json").read_text(encoding="utf-8"))
            except Exception:
                diagnostics = {}
        summary.update(
            {
                "manifest": row.get("manifest_json"),
                "coverage": (row.get("manifest_json") or {}).get("coverage", []),
                "inputs": self.repo.get_inputs(run_id),
                "diagnostics": diagnostics,
                "result_available": bool(
                    (row.get("result_dir") and Path(row["result_dir"]).exists())
                    or row.get("reused_from_run_id")
                ),
            }
        )
        return summary

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run_id = self._validate_run_id(run_id)
        row = self.repo.get_run(run_id)
        if row is None:
            raise BacktestNotFoundError("run not found")
        changed = self.repo.request_cancel(run_id)
        if not changed:
            raise BacktestValidationError("not_cancellable", "run is already terminal")
        return self.get_run(run_id)

    def rerun(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_run(self._validate_run_id(run_id))
        if row is None:
            raise BacktestNotFoundError("run not found")
        config = ExperimentConfig.model_validate_json(row["config_json"])
        return self.create_run(config, allow_incomplete=True, force=True)

    # ------------------------------------------------------------------
    # Detail tables
    # ------------------------------------------------------------------
    def get_equity(self, run_id: str, *, page: int = 1, page_size: int = 500) -> dict[str, Any]:
        return self._paginate_ledger(
            run_id,
            table="equity_points",
            order_by="session",
            page=page,
            page_size=page_size,
            transform=_equity_row,
        )

    def get_benchmarks(self, run_id: str) -> dict[str, Any]:
        row = self.repo.get_run(self._validate_run_id(run_id))
        if row is None:
            raise BacktestNotFoundError("run not found")
        run_dir = self._artifact_dir(row)
        ledger_path = run_dir / "ledger.sqlite"
        sessions: list[Any] = []
        initial_equity = None
        if ledger_path.exists():
            conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
            try:
                sessions = [
                    datetime.fromisoformat(str(item[0])).date()
                    for item in conn.execute(
                        "SELECT session FROM equity_points ORDER BY session"
                    ).fetchall()
                ]
                first = conn.execute(
                    "SELECT equity FROM equity_points ORDER BY session LIMIT 1"
                ).fetchone()
                if first is not None:
                    initial_equity = first[0]
            finally:
                conn.close()
        config_path = run_dir / "config.json"
        if config_path.exists():
            try:
                initial_equity = json.loads(config_path.read_text(encoding="utf-8")).get("initial_cash") or initial_equity
            except Exception:
                pass
        if not sessions:
            return {
                "run_id": run_id,
                "initial_equity": str(initial_equity or 0),
                "series": [],
                "warnings": ["no_equity_points"],
            }
        strategy_return_pct = None
        metrics_path = run_dir / "metrics.json"
        if metrics_path.exists():
            try:
                strategy_return_pct = json.loads(metrics_path.read_text(encoding="utf-8")).get(
                    "total_return_pct"
                )
            except Exception:
                pass
        if strategy_return_pct is None and initial_equity:
            try:
                conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
                try:
                    last = conn.execute(
                        "SELECT equity FROM equity_points ORDER BY session DESC LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
                if last is not None:
                    strategy_return_pct = (
                        float(last[0]) / float(initial_equity) - 1.0
                    ) * 100.0
            except Exception:
                pass
        from .benchmarks import build_benchmark_payload
        from decimal import Decimal

        return build_benchmark_payload(
            run_id=run_id,
            sessions=sessions,
            initial_equity=Decimal(str(initial_equity or 0)),
            cache_path=run_dir / "benchmarks.json",
            strategy_return_pct=strategy_return_pct,
        )

    def get_orders(self, run_id: str, *, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self._paginate_ledger(
            run_id,
            table="orders",
            order_by="submitted_at, rowid",
            page=page,
            page_size=page_size,
            transform=_order_row,
        )

    def get_fills(self, run_id: str, *, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self._paginate_ledger(
            run_id,
            table="fills",
            order_by="event_seq",
            page=page,
            page_size=page_size,
            transform=_fill_row,
        )

    def get_trades(self, run_id: str, *, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self._paginate_ledger(
            run_id,
            table="closed_trades",
            order_by="exit_session, trade_id",
            page=page,
            page_size=page_size,
            transform=_trade_row,
        )

    def get_positions(self, run_id: str) -> dict[str, Any]:
        run_id = self._validate_run_id(run_id)
        with self._ledger_connection(run_id) as conn:
            rows = conn.execute(
                "SELECT symbol, SUM(remaining) AS quantity, "
                "SUM(CASE WHEN remaining > 0 THEN cost_total ELSE 0 END) AS cost_total, "
                "MIN(acquired_session) AS first_acquired "
                "FROM position_lots WHERE remaining > 0 GROUP BY symbol ORDER BY symbol"
            ).fetchall()
            items = []
            total_cost = 0.0
            for row in rows:
                quantity = int(row["quantity"] or 0)
                cost_total = float(row["cost_total"] or 0)
                total_cost += cost_total
                items.append(
                    {
                        "symbol": row["symbol"],
                        "quantity": quantity,
                        "cost_total": cost_total,
                        "average_cost": (cost_total / quantity) if quantity else None,
                        "first_acquired": row["first_acquired"],
                    }
                )
            last_equity = conn.execute(
                "SELECT market_value FROM equity_points ORDER BY session DESC LIMIT 1"
            ).fetchone()
            market_value = float(last_equity["market_value"]) if last_equity else 0.0
            return {
                "items": items,
                "total_cost": total_cost,
                "market_value": market_value,
                "unrealized_pnl": market_value - total_cost if items else 0.0,
            }

    def get_inputs(self, run_id: str, *, included: bool | None = None) -> dict[str, Any]:
        run_id = self._validate_run_id(run_id)
        return {"items": self.repo.get_inputs(run_id, included=included)}

    def get_diagnostics(self, run_id: str) -> dict[str, Any]:
        run_id = self._validate_run_id(run_id)
        row = self.repo.get_run(run_id)
        if row is None:
            raise BacktestNotFoundError("run not found")
        run_dir = self._artifact_dir(row)
        path = run_dir / "diagnostics.json"
        if not path.exists():
            return {"items": {}}
        try:
            return {"items": json.loads(path.read_text(encoding="utf-8"))}
        except Exception:
            return {"items": {}}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export(self, run_id: str, kind: str) -> tuple[str, str, Iterator[bytes]]:
        run_id = self._validate_run_id(run_id)
        kind = str(kind or "").strip().lower()
        if kind not in EXPORT_KINDS:
            raise BacktestValidationError("invalid_export_kind", f"unsupported export kind: {kind}")
        row = self.repo.get_run(run_id)
        if row is None:
            raise BacktestNotFoundError("run not found")
        run_dir = self._artifact_dir(row)
        if not run_dir.exists():
            raise BacktestNotFoundError("run artifacts not found")
        if EXPORT_KINDS[kind] == "json":
            path = {
                "config": run_dir / "config.json",
                "manifest": run_dir / "manifest.json",
                "metrics": run_dir / "metrics.json",
                "inputs": run_dir / "inputs.json",
                "diagnostics": run_dir / "diagnostics.json",
            }[kind]
            if not path.exists():
                raise BacktestNotFoundError(f"{kind} artifact not found")
            content = path.read_bytes()
            return f"{run_id}-{kind}.json", "application/json", _iter_bytes(content)
        query = {
            "ledger": "SELECT * FROM ledger_events ORDER BY sequence",
            "orders": "SELECT * FROM orders ORDER BY submitted_at, rowid",
            "fills": "SELECT * FROM fills ORDER BY event_seq",
            "equity": "SELECT * FROM equity_points ORDER BY session",
            "trades": "SELECT * FROM closed_trades ORDER BY exit_session, trade_id",
        }[kind]
        return f"{run_id}-{kind}.csv", "text/csv", self._stream_csv(run_id, query, kind)

    def _stream_csv(self, run_id: str, query: str, kind: str) -> Iterator[bytes]:
        with self._ledger_connection(run_id) as conn:
            cursor = conn.execute(query)
            header = [description[0] for description in cursor.description]
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)
            for row in cursor:
                writer.writerow([row[key] for key in header])
                yield buffer.getvalue().encode("utf-8")
                buffer.seek(0)
                buffer.truncate(0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _summary_from_row(self, row: dict[str, Any], *, config: ExperimentConfig | None) -> dict[str, Any]:
        if config is None:
            try:
                config = ExperimentConfig.model_validate_json(row["config_json"])
            except Exception:
                config = None
        metrics = row.get("metrics_json")
        summary = row.get("summary_json") or {}
        research = summary.get("research") or {}
        return {
            "realized_pnl": summary.get("realized_pnl"),
            "unrealized_pnl": None,
            "final_equity": summary.get("final_equity"),
            "cash": summary.get("cash"),
            "research": research,
            "labels": research.get("labels", []),
            "llm_calls": research.get("llm_calls", 0),
            "cache_hits": research.get("cache_hits", 0),
            "run_id": row["run_id"],
            "mode": row["mode"],
            "source_pipeline": (config.source_pipeline.value if config else row.get("source_pipeline")),
            "risk_profile": (config.risk_profile.value if config else row.get("risk_profile")),
            "status": row["status"],
            "attempt": row.get("attempt", 1),
            "request_fingerprint": row.get("request_fingerprint"),
            "experiment_fingerprint": row.get("experiment_fingerprint"),
            "reused_from_run_id": row.get("reused_from_run_id"),
            "data_quality": row.get("data_quality") or "partial",
            "quality_flags": row.get("quality_flags_json") or [],
            "progress": float(row.get("progress") or 0.0),
            "phase": row.get("phase") or "",
            "metrics": metrics,
            "counts": row.get("counts_json") or summary.get("counts") or {},
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "cancel_requested": bool(row.get("cancel_requested")),
            "result_available": bool(row.get("result_dir") or row.get("reused_from_run_id")),
            "config": config.model_dump(mode="json") if config else None,
        }

    def _paginate_ledger(
        self,
        run_id: str,
        *,
        table: str,
        order_by: str,
        page: int,
        page_size: int,
        transform,
    ) -> dict[str, Any]:
        run_id = self._validate_run_id(run_id)
        page = max(1, int(page))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
        with self._ledger_connection(run_id) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": [transform(row) for row in rows],
            }

    @contextmanager
    def _ledger_connection(self, run_id: str):
        import sqlite3

        row = self.repo.get_run(run_id)
        if row is None:
            raise BacktestNotFoundError("run not found")
        run_dir = self._artifact_dir(row)
        ledger_path = run_dir / "ledger.sqlite"
        if not ledger_path.exists():
            raise BacktestNotFoundError("run ledger not found")
        conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _config_hash(config: ExperimentConfig) -> str:
    from .schemas import fingerprint_value

    return fingerprint_value(config.normalized())


def _iter_bytes(content: bytes) -> Iterator[bytes]:
    yield content


def _equity_row(row) -> dict[str, Any]:
    return {
        "session": row["session"],
        "timestamp": row["timestamp"],
        "cash": float(row["cash"]),
        "reserved_cash": float(row["reserved_cash"]),
        "available_cash": float(row["available_cash"]),
        "market_value": float(row["market_value"]),
        "equity": float(row["equity"]),
        "peak_equity": float(row["peak_equity"]),
        "drawdown_pct": float(row["drawdown"]) * 100.0,
        "stale": bool(row["stale"]),
        "benchmark_value": float(row["benchmark_value"]) if row["benchmark_value"] is not None else None,
        "granularity": row["granularity"],
        "quality": row["quality"],
    }


def _order_row(row) -> dict[str, Any]:
    return {
        "order_id": row["order_id"],
        "plan_key": row["plan_key"],
        "symbol": row["symbol"],
        "side": row["side"],
        "order_type": row["order_type"],
        "quantity": int(row["quantity"]),
        "remaining": int(row["remaining"]),
        "filled_quantity": int(row["filled_quantity"]),
        "avg_fill_price": float(row["avg_fill_price"]) if row["avg_fill_price"] else None,
        "limit_price": float(row["limit_price"]) if row["limit_price"] else None,
        "stop_price": float(row["stop_price"]) if row["stop_price"] else None,
        "status": row["status"],
        "reason": row["reason"],
        "pre_registered_exit": bool(row["pre_registered_exit"]),
        "exit_reason": row["exit_reason"],
        "submitted_at": row["submitted_at"],
        "active_from": row["active_from"],
        "expires_on_session": row["expires_on_session"],
    }


def _fill_row(row) -> dict[str, Any]:
    return {
        "fill_id": row["fill_id"],
        "order_id": row["order_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": int(row["quantity"]),
        "price": float(row["price"]),
        "gross_amount": float(row["gross_amount"]),
        "commission": float(row["commission"]),
        "transfer_fee": float(row["transfer_fee"]),
        "stamp_duty": float(row["stamp_duty"]),
        "slippage_cost": float(row["slippage_cost"]),
        "session": row["session"],
        "bar_end_at": row["bar_end_at"],
        "quality": row["quality"],
        "reason": row["reason"],
    }


def _trade_row(row) -> dict[str, Any]:
    return {
        "trade_id": row["trade_id"],
        "symbol": row["symbol"],
        "entry_order_id": row["entry_order_id"],
        "exit_order_id": row["exit_order_id"],
        "quantity": int(row["quantity"]),
        "entry_price": float(row["entry_price"]),
        "exit_price": float(row["exit_price"]),
        "cost_basis": float(row["cost_basis"]),
        "net_proceeds": float(row["net_proceeds"]),
        "realized_pnl": float(row["realized_pnl"]),
        "return_pct": float(row["return_pct"]),
        "entry_session": row["entry_session"],
        "exit_session": row["exit_session"],
        "holding_days": int(row["holding_days"]),
        "exit_reason": row["exit_reason"],
        "mfe_pct": row["mfe_pct"],
        "mae_pct": row["mae_pct"],
    }


__all__ = [
    "BacktestNotFoundError",
    "BacktestRunService",
    "BacktestValidationError",
]
