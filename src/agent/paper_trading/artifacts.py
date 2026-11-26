# -*- coding: utf-8 -*-
"""Artifact ingest for the paper-trading shadow account.

The shadow account never calls an LLM and never re-runs the selection chain.
It only consumes products that the user's manual runs already persisted:

* **selection run** — ``<trace_dir>/<run_id>/stock_selection.json``.  The AI
  final action and its sizing live in
  ``final_report_json.portfolio_allocation.full.positions_plan[]``.
* **single-stock analysis** — ``analysis_history`` rows, written by the normal
  ``main.py --stocks ...`` / API analysis path.  The AI verdict lives in
  ``raw_result.decision_type`` / ``operation_advice``.

Engine price levels are never taken from the AI text.  They come from
``trade_plan_artifacts`` (the persisted deterministic plan artifact).  When a
symbol has no stored artifact yet, the same deterministic engine is evaluated
locally with ``fallback_to_network=False`` and ``persist=False`` so that the
shadow still gets engine-authored levels without any network access.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

from src.agent.pricing import generate_trade_plan
from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.snapshot import PricePlanSnapshotBuilder
from src.storage import AnalysisHistory, DatabaseManager, TradePlanArtifactRecord

logger = logging.getLogger(__name__)

SOURCE_SELECTION = "selection_run"
SOURCE_SINGLE = "single_analysis"

_OPEN_ACTIONS = {"open", "buy", "add", "strong_buy", "immediate_open"}
_OPEN_ADVICE = {"买入", "加仓", "建仓", "增持"}
_RUN_ID_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


@dataclass
class ArtifactOrder:
    symbol: str
    ai_action: str
    ai_position_pct: Optional[float] = None
    engine_position_pct: Optional[float] = None
    execution_mode: Optional[str] = None
    name: Optional[str] = None
    portfolio_action: Optional[str] = None
    engine_plan: Optional[Dict[str, Any]] = None


@dataclass
class ArtifactRecord:
    source: str
    run_id: str
    decision_date: date
    artifact_path: str
    orders: List[ArtifactOrder] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def normalize_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _OPEN_ACTIONS:
        return "open"
    return text


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def decision_date_from_run_id(run_id: str) -> Optional[date]:
    match = _RUN_ID_DATE.match(str(run_id or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def next_trading_day(calendar: Sequence[date], day: date) -> date:
    for candidate in calendar:
        if candidate > day:
            return candidate
    return date.fromordinal(day.toordinal() + 1)


def trace_root(config: Any) -> Path:
    configured = str(getattr(config, "agent_paper_trading_trace_dir", "") or "").strip()
    if configured:
        return Path(configured)
    database_path = str(getattr(config, "database_path", "") or "").strip()
    base = Path(database_path).parent if database_path else Path("data")
    return base / "agent_traces"


# ---------------------------------------------------------------------------
# selection run artifacts
# ---------------------------------------------------------------------------
def discover_selection_artifacts(
    root: Path,
    *,
    known: Iterable[tuple[str, str]] = (),
    limit: int = 20,
    since: Optional[date] = None,
) -> List[ArtifactRecord]:
    """Parse not-yet-ingested ``stock_selection.json`` traces, newest first."""
    root = Path(root)
    if not root.exists():
        return []
    known_keys = set(known)
    records: List[ArtifactRecord] = []
    for path in sorted(root.glob("*/stock_selection.json"), reverse=True):
        run_id = path.parent.name
        if (SOURCE_SELECTION, run_id) in known_keys:
            continue
        record = parse_selection_artifact(path, run_id=run_id)
        if record is None:
            continue
        if since is not None and record.decision_date < since:
            continue
        records.append(record)
        if len(records) >= max(1, int(limit)):
            break
    records.sort(key=lambda item: item.decision_date)
    return records


def parse_selection_artifact(path: Path, *, run_id: Optional[str] = None) -> Optional[ArtifactRecord]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[PaperTrading] 选股产物解析失败 %s: %s", path, exc)
        return None

    context = payload.get("selection_context") or {}
    # The trace directory name is unique per run; ``selection_context.run_id``
    # is not (some traces store a placeholder such as ``selection-run``).
    run_id = str(run_id or Path(path).parent.name)
    decision_date = decision_date_from_run_id(run_id)
    if decision_date is None:
        decision_date = datetime.fromtimestamp(Path(path).stat().st_mtime).date()

    report = payload.get("final_report_json") or {}
    allocation = report.get("portfolio_allocation") or {}
    summary = allocation.get("summary") or {}
    plans = ((allocation.get("full") or {}).get("positions_plan")) or []
    portfolio_action = summary.get("portfolio_action")

    caps = _load_position_caps(Path(path).parent)

    orders: List[ArtifactOrder] = []
    for item in plans:
        symbol = str(item.get("code") or "").strip()
        if not symbol:
            continue
        orders.append(
            ArtifactOrder(
                symbol=symbol,
                ai_action=normalize_action(item.get("action")),
                ai_position_pct=_as_float(item.get("initial_position_pct")),
                engine_position_pct=caps.get(symbol),
                execution_mode=item.get("execution_mode"),
                name=item.get("name"),
                portfolio_action=portfolio_action,
            )
        )

    return ArtifactRecord(
        source=SOURCE_SELECTION,
        run_id=run_id,
        decision_date=decision_date,
        artifact_path=str(path),
        orders=orders,
        meta={
            "portfolio_action": portfolio_action,
            "orders_total": len(plans),
            "chain_run_id": context.get("run_id"),
        },
    )


def _load_position_caps(trace_dir: Path) -> Dict[str, float]:
    """Per-symbol deterministic position cap, when the trace carries one."""
    path = trace_dir / "deterministic_scoring.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    full = payload.get("full") or payload
    items = full.get("symbols") if isinstance(full, dict) else None
    if not isinstance(items, list):
        items = full.get("items") if isinstance(full, dict) else None
    if not isinstance(items, list):
        return {}
    caps: Dict[str, float] = {}
    for item in items:
        code = str(item.get("code") or item.get("symbol") or "").strip()
        cap = _as_float(item.get("target_position_cap_pct") or item.get("position_cap_pct"))
        if code and cap:
            caps[code] = cap
    return caps


# ---------------------------------------------------------------------------
# single-stock analysis artifacts
# ---------------------------------------------------------------------------
def discover_single_analysis(
    db_session: Any,
    *,
    known: Iterable[tuple[str, str]] = (),
    limit: int = 20,
    before: Optional[date] = None,
    since: Optional[date] = None,
) -> List[ArtifactRecord]:
    """Read recent ``analysis_history`` rows that were not booked yet."""
    known_keys = set(known)
    stmt = select(AnalysisHistory).order_by(AnalysisHistory.id.desc()).limit(200)
    rows = db_session.execute(stmt).scalars().all()
    records: List[ArtifactRecord] = []
    for row in rows:
        run_id = str(row.query_id or "").strip()
        if not run_id or (SOURCE_SINGLE, run_id) in known_keys:
            continue
        created = getattr(row, "created_at", None)
        if created is None:
            continue
        decision_date = created.date() if isinstance(created, datetime) else created
        if before is not None and decision_date > before:
            continue
        if since is not None and decision_date < since:
            continue
        record = parse_single_analysis_row(row, decision_date=decision_date)
        if record is None:
            continue
        records.append(record)
        if len(records) >= max(1, int(limit)):
            break
    records.sort(key=lambda item: item.decision_date)
    return records


def parse_single_analysis_row(row: Any, *, decision_date: date) -> Optional[ArtifactRecord]:
    raw: Dict[str, Any] = {}
    if getattr(row, "raw_result", None):
        try:
            raw = json.loads(row.raw_result) or {}
        except (TypeError, ValueError):
            raw = {}
    decision_type = str(raw.get("decision_type") or "").strip()
    advice = str(getattr(row, "operation_advice", "") or "").strip()
    if decision_type.lower() in _OPEN_ACTIONS:
        ai_action = "open"
    elif advice in _OPEN_ADVICE:
        ai_action = "open"
    else:
        ai_action = normalize_action(decision_type or advice)

    symbol = str(getattr(row, "code", "") or "").strip()
    if not symbol:
        return None
    return ArtifactRecord(
        source=SOURCE_SINGLE,
        run_id=str(row.query_id),
        decision_date=decision_date,
        artifact_path=f"analysis_history:{row.id}",
        orders=[
            ArtifactOrder(
                symbol=symbol,
                ai_action=ai_action,
                ai_position_pct=_as_float(raw.get("position_pct")),
                engine_position_pct=_as_float(raw.get("target_position_cap_pct")),
                name=getattr(row, "name", None),
            )
        ],
        meta={"decision_type": decision_type, "operation_advice": advice},
    )


# ---------------------------------------------------------------------------
# engine price levels
# ---------------------------------------------------------------------------
def resolve_engine_plan(
    db_session: Any,
    symbol: str,
    decision_date: date,
    *,
    builder: Any = None,
    priority: int = 0,
) -> Optional[Dict[str, Any]]:
    """Return numeric engine levels for ``symbol`` as of ``decision_date``."""
    persisted = _persisted_plan(db_session, symbol, decision_date)
    if persisted is not None:
        return persisted
    return _computed_plan(symbol, decision_date, builder=builder, priority=priority)


def _persisted_plan(db_session: Any, symbol: str, decision_date: date) -> Optional[Dict[str, Any]]:
    stmt = (
        select(TradePlanArtifactRecord)
        .where(
            TradePlanArtifactRecord.symbol == symbol,
            TradePlanArtifactRecord.generation_status == "generated",
            TradePlanArtifactRecord.requested_as_of <= decision_date,
        )
        .order_by(
            TradePlanArtifactRecord.requested_as_of.desc(),
            TradePlanArtifactRecord.created_at.desc(),
        )
        .limit(5)
    )
    for row in db_session.execute(stmt).scalars().all():
        if row.valid_until is not None and row.valid_until < decision_date:
            continue
        try:
            artifact = json.loads(row.artifact_json)
        except (TypeError, ValueError):
            continue
        plan = plan_row_from_artifact(artifact, priority=0)
        if plan is not None:
            return plan
    return None


def _computed_plan(
    symbol: str,
    decision_date: date,
    *,
    builder: Any = None,
    priority: int = 0,
) -> Optional[Dict[str, Any]]:
    try:
        snapshot = builder.build(symbol, as_of=decision_date) if builder is not None else None
        result = generate_trade_plan(
            symbol,
            as_of=decision_date,
            snapshot=snapshot,
            persist=False,
        )
    except Exception as exc:  # pragma: no cover - defensive around engine IO
        logger.warning("[PaperTrading] 引擎生成计划失败 symbol=%s err=%s", symbol, exc)
        return None
    return plan_row_from_engine(result.price_plan if result is not None else None, priority=priority)


def build_plan_builder(db_manager: Any = None) -> PricePlanSnapshotBuilder:
    repository = PricePlanRepository(db_manager or DatabaseManager.get_instance())
    return PricePlanSnapshotBuilder(
        chip_loader=lambda *args, **kwargs: None,
        market_rules_loader=repository.get_market_rules_for_symbol_date,
        fallback_to_network=False,
    )


def plan_row_from_artifact(artifact: Dict[str, Any], *, priority: int = 0) -> Optional[Dict[str, Any]]:
    if not isinstance(artifact, dict):
        return None
    if str(artifact.get("generation_status") or "generated") != "generated":
        return None
    scenarios = artifact.get("scenarios") or []
    scenario = _pick_scenario(
        scenarios,
        get=lambda item, key: item.get(key),
        status_of=lambda item: str(item.get("role") or ""),
    )
    if scenario is None:
        return None
    valid_until = scenario.get("confirmation_deadline") or artifact.get("valid_until")
    return {
        "symbol": str(artifact.get("symbol") or scenario.get("symbol") or ""),
        "plan_id": str(artifact.get("plan_id") or ""),
        "generation_status": "generated",
        "scenario_id": str(scenario.get("scenario_id") or ""),
        "entry_low": float(scenario["entry_low"]),
        "entry_high": float(scenario["entry_high"]),
        "stop_loss": float(scenario["stop_loss"]),
        "target_1": float(scenario["target_1"]),
        "target_2": _as_float(scenario.get("target_2")),
        "target_1_exit_ratio": float(scenario.get("target_1_exit_ratio") or 0.5),
        "valid_until": valid_until if isinstance(valid_until, str) else (
            valid_until.isoformat() if isinstance(valid_until, date) else None
        ),
        "engine_position_pct": _as_float(
            scenario.get("target_position_cap_pct") or artifact.get("target_position_cap_pct")
        ),
        "priority": priority,
    }


def plan_row_from_engine(artifact: Any, *, priority: int = 0) -> Optional[Dict[str, Any]]:
    if artifact is None or str(getattr(artifact, "generation_status", "")) != "generated":
        return None
    scenarios = list(getattr(artifact, "scenarios", []) or [])
    scenario = _pick_scenario(
        scenarios,
        get=getattr,
        status_of=lambda item: str(getattr(item, "role", "")),
    )
    if scenario is None:
        return None
    valid_until = getattr(scenario, "confirmation_deadline", None)
    return {
        "symbol": str(getattr(artifact, "symbol", "")),
        "plan_id": str(getattr(artifact, "plan_id", "")),
        "generation_status": "generated",
        "scenario_id": str(getattr(scenario, "scenario_id", "")),
        "entry_low": float(scenario.entry_low),
        "entry_high": float(scenario.entry_high),
        "stop_loss": float(scenario.stop_loss),
        "target_1": float(scenario.target_1),
        "target_2": _as_float(getattr(scenario, "target_2", None)),
        "target_1_exit_ratio": float(getattr(scenario, "target_1_exit_ratio", 0.5) or 0.5),
        "valid_until": valid_until.isoformat() if isinstance(valid_until, date) else None,
        "engine_position_pct": _as_float(getattr(scenario, "target_position_cap_pct", None)),
        "priority": priority,
    }


def _pick_scenario(items: Iterable[Any], *, get: Any, status_of: Any) -> Optional[Any]:
    formal = [
        item
        for item in items
        if status_of(item) == "primary"
        and get(item, "stop_loss") is not None
        and get(item, "target_1") is not None
    ]
    if not formal:
        formal = [
            item
            for item in items
            if status_of(item) != "observation"
            and get(item, "stop_loss") is not None
            and get(item, "target_1") is not None
        ]
    if not formal:
        return None
    return sorted(formal, key=lambda item: int(get(item, "base_rank") or 1))[0]


__all__ = [
    "ArtifactOrder",
    "ArtifactRecord",
    "SOURCE_SELECTION",
    "SOURCE_SINGLE",
    "build_plan_builder",
    "decision_date_from_run_id",
    "discover_selection_artifacts",
    "discover_single_analysis",
    "next_trading_day",
    "normalize_action",
    "parse_selection_artifact",
    "parse_single_analysis_row",
    "plan_row_from_artifact",
    "plan_row_from_engine",
    "resolve_engine_plan",
    "trace_root",
]
