"""Read-only shadow probe for the deterministic price plan engine.

The probe runs the engine for a bounded list of selection candidates and writes
the outcome to a single JSON file. It never writes business tables, never feeds
back into actions/ranking/report, and is disabled unless
``AGENT_PRICING_SHADOW_ENABLED`` (or ``agent_pricing_shadow_enabled``) is on.

When disabled the probe returns immediately, without building snapshots,
touching the database or writing files.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from src.agent.structure import analyze_price_structure, coerce_price_bars
from src.config import get_config
from src.storage import DatabaseManager

from .anchors import calculate_atr
from .market_rules import is_supported_cn_common_stock, normalize_cn_symbol
from .params import PRICE_PLAN_PARAMS_V1
from .plan import evaluate_price_plan
from .repository import PricePlanRepository
from .runtime import generate_trade_plan
from .schemas import PriceBarSnapshot, PricePlanSnapshot
from .snapshot import PricePlanSnapshotBuilder, resolve_completed_cn_as_of

SCHEMA_VERSION = "pricing-shadow.v1"
_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_OUTPUT = _ROOT / "data" / "pricing_shadow.json"
_ADJ_DB = _ROOT / "data" / "adj_factors.db"
_ADJ_DB_ENV = "AGENT_PRICING_SHADOW_ADJ_DB"
_PRICE_QUANT = Decimal("0.000001")
_CN_SUFFIXES = (".SH", ".SZ", ".BJ")


def pricing_shadow_enabled() -> bool:
    try:
        return bool(getattr(get_config(), "agent_pricing_shadow_enabled", False))
    except Exception:  # pragma: no cover - config must never break the probe
        raw = os.getenv("AGENT_PRICING_SHADOW_ENABLED", "")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _max_codes() -> int:
    try:
        value = int(getattr(get_config(), "agent_pricing_shadow_max_codes", 60))
    except Exception:
        value = 60
    return value if value > 0 else 0


def _output_path() -> Path:
    raw = ""
    try:
        raw = str(getattr(get_config(), "agent_pricing_shadow_path", "") or "")
    except Exception:
        raw = ""
    if raw.strip():
        candidate = Path(raw.strip()).expanduser()
        return candidate if candidate.is_absolute() else _ROOT / candidate
    return _DEFAULT_OUTPUT


def _adj_db_path() -> Path:
    raw = os.getenv(_ADJ_DB_ENV, "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else _ROOT / candidate
    return _ADJ_DB


def classify_market(code: Any, default_market: str = "cn") -> str:
    """Best-effort market label; never raises."""
    raw = str(code or "").strip()
    upper = raw.upper()
    if not upper:
        return default_market
    if upper.endswith(".HK") or upper.startswith("HK"):
        return "hk"
    if upper.endswith(_CN_SUFFIXES) or upper.startswith(("SH", "SZ", "BJ")):
        return "cn"
    digits = re.sub(r"\D", "", raw)
    if digits and len(digits) >= 6:
        return "cn"
    if digits and len(digits) == 5:
        return "hk"
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]*", upper):
        return "us"
    return default_market


def _normalize_code(code: Any) -> str:
    return normalize_cn_symbol(code) or str(code or "").strip()


def _dedupe(codes: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in codes:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _pct_delta(cash_value: Any, qfq_value: Any) -> float | None:
    try:
        cash = Decimal(str(cash_value))
        qfq = Decimal(str(qfq_value))
    except Exception:
        return None
    if cash == 0:
        return None
    return float((qfq / cash - Decimal("1")) * Decimal("100"))


def _factor_lookup(factors: dict[date, Decimal], target: date) -> Decimal | None:
    if target in factors:
        return factors[target]
    candidates = [day for day in factors if day <= target]
    if not candidates:
        return None
    return factors[max(candidates)]


def _load_adj_factors(code: str) -> dict[date, Decimal]:
    path = _adj_db_path()
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT date, adj_factor FROM stock_adj_factors WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    factors: dict[date, Decimal] = {}
    for raw_date, raw_factor in rows:
        try:
            factors[date.fromisoformat(str(raw_date)[:10])] = Decimal(str(raw_factor))
        except Exception:
            continue
    return factors


def _qfq_bar(bar: PriceBarSnapshot, ratio: Decimal, factor: Decimal) -> PriceBarSnapshot:
    open_ = (bar.open * ratio).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
    close = (bar.close * ratio).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
    high = (bar.high * ratio).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
    low = (bar.low * ratio).quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
    high = max(high, open_, close, low)
    low = min(low, open_, close, high)
    return bar.model_copy(
        update={
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": (bar.volume / ratio),
            "adjustment_factor": factor,
        }
    )


def _weekly_bars(bars: Sequence[PriceBarSnapshot]) -> list[PriceBarSnapshot]:
    buckets: dict[tuple[int, int], list[PriceBarSnapshot]] = {}
    for bar in bars:
        iso = bar.trade_date.isocalendar()
        buckets.setdefault((iso[0], iso[1]), []).append(bar)
    weekly: list[PriceBarSnapshot] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        weekly.append(
            PriceBarSnapshot(
                trade_date=bucket[-1].trade_date,
                open=bucket[0].open,
                high=max(item.high for item in bucket),
                low=min(item.low for item in bucket),
                close=bucket[-1].close,
                volume=sum((item.volume for item in bucket), Decimal("0")),
                amount=sum((item.amount or Decimal("0") for item in bucket), Decimal("0")),
            )
        )
    return weekly


def build_qfq_snapshot(
    snapshot: PricePlanSnapshot,
    factors: dict[date, Decimal],
) -> tuple[PricePlanSnapshot | None, dict[str, Any]]:
    """Derive a qian-fu-quan snapshot from cash bars via adj_factor ratios.

    ``qfq(t) = cash(t) * adj_factor(t) / adj_factor(as_of)`` and
    ``volume_qfq(t) = volume_cash(t) * adj_factor(as_of) / adj_factor(t)``.
    Market rules stay cash-based on purpose: this series is analysis-only.
    """
    as_of = snapshot.actual_as_of_session
    as_of_factor = _factor_lookup(factors, as_of)
    if as_of_factor is None:
        return None, {"status": "unavailable", "reason": "MISSING_ADJ_FACTOR_AS_OF"}

    qfq_bars: list[PriceBarSnapshot] = []
    gaps = 0
    max_deviation = Decimal("0")
    for bar in snapshot.daily_bars:
        factor = _factor_lookup(factors, bar.trade_date)
        if factor is None or factor <= 0:
            gaps += 1
            continue
        ratio = factor / as_of_factor
        max_deviation = max(max_deviation, abs(ratio - Decimal("1")) * Decimal("100"))
        qfq_bars.append(_qfq_bar(bar, ratio, factor))
    if not qfq_bars:
        return None, {"status": "unavailable", "reason": "MISSING_ADJ_FACTORS"}

    weekly = _weekly_bars(qfq_bars)
    max_weekly = (
        PRICE_PLAN_PARAMS_V1.weekly_evaluation_bars
        + PRICE_PLAN_PARAMS_V1.weekly_warmup_bars
    )
    structure = analyze_price_structure(
        coerce_price_bars(
            [
                {
                    "date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in qfq_bars
            ]
        ),
        include_raw=False,
    )
    derived = snapshot.model_copy(
        update={
            "daily_bars": qfq_bars,
            "weekly_bars": weekly[-max_weekly:],
            "last_cash_price": qfq_bars[-1].close,
            "price_basis": "qfq",
            "price_scale_verified": True,
            "adjustment_factor_semantics": (
                "qfq = cash * adj_factor(t) / adj_factor(as_of); "
                "volume = cash_volume * adj_factor(as_of) / adj_factor(t)"
            ),
            "adjustment_factor_version": f"tushare-adj-factor:{as_of.isoformat()}",
            "structure_evidence": structure,
        }
    )
    return derived, {
        "status": "ok",
        "bars": len(qfq_bars),
        "dropped_bars": gaps,
        "adj_factor_as_of": str(as_of_factor),
        "max_abs_price_deviation_pct": float(max_deviation),
    }


def _summarize(artifact: Any) -> dict[str, Any]:
    payload = artifact.model_dump(mode="json")
    keep = (
        "generation_status",
        "data_quality",
        "reason_codes",
        "actual_as_of_session",
        "scenarios",
        "observation_anchors",
        "diagnostics",
    )
    return {key: payload.get(key) for key in keep}


def _atr(bars: Sequence[PriceBarSnapshot]) -> str | None:
    try:
        return str(calculate_atr(list(bars), PRICE_PLAN_PARAMS_V1.atr_period))
    except Exception:
        return None


def _scenario_pairs(cash: dict[str, Any] | None, qfq: dict[str, Any] | None) -> list[dict[str, Any]]:
    cash_scenarios = list((cash or {}).get("scenarios") or [])
    qfq_scenarios = list((qfq or {}).get("scenarios") or [])
    pairs: list[dict[str, Any]] = []
    for index in range(max(len(cash_scenarios), len(qfq_scenarios))):
        left = cash_scenarios[index] if index < len(cash_scenarios) else {}
        right = qfq_scenarios[index] if index < len(qfq_scenarios) else {}
        pairs.append(
            {
                "index": index,
                "role": {"cash": left.get("role"), "qfq": right.get("role")},
                "entry_low": {"cash": left.get("entry_low"), "qfq": right.get("entry_low")},
                "entry_high": {"cash": left.get("entry_high"), "qfq": right.get("entry_high")},
                "stop_loss": {"cash": left.get("stop_loss"), "qfq": right.get("stop_loss")},
                "target_1": {"cash": left.get("target_1"), "qfq": right.get("target_1")},
                "target_2": {"cash": left.get("target_2"), "qfq": right.get("target_2")},
                "reference_net_rr_1": {
                    "cash": left.get("reference_net_rr_1"),
                    "qfq": right.get("reference_net_rr_1"),
                },
            }
        )
    return pairs


def _probe_one(
    code: str,
    *,
    market: str,
    as_of: date,
    db: DatabaseManager,
    builder_factory: Callable[[], PricePlanSnapshotBuilder],
) -> dict[str, Any]:
    started = time.perf_counter()
    record: dict[str, Any] = {
        "code": code,
        "market": market,
        "status": "error",
        "reason_codes": [],
        "cash": None,
        "qfq": None,
        "qfq_status": {"status": "not_attempted"},
        "comparison": None,
    }

    if market != "cn":
        record.update(
            status="unsupported",
            reason_codes=["UNSUPPORTED_INSTRUMENT"],
            note="market_not_supported_by_engine",
        )
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return record
    if not is_supported_cn_common_stock(code):
        record.update(
            status="unsupported",
            reason_codes=["UNSUPPORTED_INSTRUMENT"],
            note="symbol_not_supported_by_engine",
        )
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return record

    try:
        snapshot = builder_factory().build(code, as_of=as_of)
        result = generate_trade_plan(
            code,
            as_of=as_of,
            snapshot=snapshot,
            db_manager=db,
            persist=False,
        )
        artifact = result.price_plan
        record["cash"] = _summarize(artifact)
        record["cash_bars"] = len(snapshot.daily_bars)
        record["cash_atr_14"] = _atr(snapshot.daily_bars)
        generation_status = getattr(artifact.generation_status, "value", artifact.generation_status)
        record["execution_status"] = result.execution_status
        record["status"] = str(generation_status)
        if result.execution_status in {"system_unready", "system_disabled"}:
            record["status"] = result.execution_status
            record["note"] = "readiness_gate_failed: market rules / instrument metadata unverified"
        record["reason_codes"] = list(artifact.reason_codes)
    except Exception as exc:  # noqa: BLE001 - one symbol must not break the run
        record.update(
            status="error",
            reason_codes=["SHADOW_PROBE_ERROR"],
            error=f"{type(exc).__name__}: {exc}",
        )
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return record

    if str(getattr(artifact.generation_status, "value", artifact.generation_status)) == "no_plan":
        record["qfq_status"] = {"status": "skipped", "reason": "cash_evaluation_has_no_plan"}
    else:
        try:
            factors = _load_adj_factors(code)
            qfq_snapshot, qfq_status = build_qfq_snapshot(snapshot, factors)
            record["qfq_status"] = qfq_status
            if qfq_snapshot is not None:
                qfq_artifact = evaluate_price_plan(qfq_snapshot, PRICE_PLAN_PARAMS_V1)
                record["qfq"] = _summarize(qfq_artifact)
                record["qfq_atr_14"] = _atr(qfq_snapshot.daily_bars)
        except Exception as exc:  # noqa: BLE001
            record["qfq_status"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    record["comparison"] = {
        "qfq_price_deviation_pct": (record.get("qfq_status") or {}).get(
            "max_abs_price_deviation_pct"
        ),
        "atr_14": {
            "cash": record.get("cash_atr_14"),
            "qfq": record.get("qfq_atr_14"),
            "delta_pct": _pct_delta(record.get("cash_atr_14"), record.get("qfq_atr_14")),
        },
        "scenarios": _scenario_pairs(record.get("cash"), record.get("qfq")),
    }
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return record


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run_pricing_shadow_probe(
    codes: Iterable[Any],
    *,
    as_of: date | None = None,
    run_id: str | None = None,
    market: str = "cn",
    output_path: str | Path | None = None,
    max_codes: int | None = None,
    db_manager: DatabaseManager | None = None,
) -> dict[str, Any] | None:
    """Run the engine in shadow mode. Returns the payload, or None when disabled."""
    if not pricing_shadow_enabled():
        return None

    ordered = _dedupe(codes)
    limit = _max_codes() if max_codes is None else int(max_codes)
    truncated = 0
    if limit and len(ordered) > limit:
        truncated = len(ordered) - limit
        ordered = ordered[:limit]
    if not ordered:
        return None

    started = time.perf_counter()
    probe_as_of = as_of or resolve_completed_cn_as_of(date.today())
    db = db_manager or DatabaseManager.get_instance()
    repository = PricePlanRepository(db)

    def _builder_factory() -> PricePlanSnapshotBuilder:
        return PricePlanSnapshotBuilder(
            chip_loader=lambda *args, **kwargs: None,
            market_rules_loader=repository.get_market_rules_for_symbol_date,
            fallback_to_network=False,
        )

    items: list[dict[str, Any]] = []
    for raw_code in ordered:
        resolved_market = classify_market(raw_code, default_market=market)
        code = _normalize_code(raw_code) if resolved_market == "cn" else str(raw_code).strip()
        items.append(
            _probe_one(
                code,
                market=resolved_market,
                as_of=probe_as_of,
                db=db,
                builder_factory=_builder_factory,
            )
        )

    status_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        for reason in item.get("reason_codes") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "as_of": probe_as_of.isoformat(),
        "market": market,
        "engine": {
            "params_version": PRICE_PLAN_PARAMS_V1.version,
            "daily_bars_required": (
                PRICE_PLAN_PARAMS_V1.daily_evaluation_bars
                + PRICE_PLAN_PARAMS_V1.daily_warmup_bars
            ),
        },
        "scope": {
            "requested_codes": len(ordered) + truncated,
            "probed_codes": len(items),
            "truncated_codes": truncated,
            "max_codes": limit,
        },
        "summary": {
            "by_status": status_counts,
            "by_reason_code": reason_counts,
            "cash_generated": status_counts.get("generated", 0),
            "cash_observation": status_counts.get("observation", 0),
            "cash_no_plan": status_counts.get("no_plan", 0),
            "system_unready": status_counts.get("system_unready", 0),
            "unsupported": status_counts.get("unsupported", 0),
            "errors": status_counts.get("error", 0),
            "qfq_ok": sum(
                1 for item in items if (item.get("qfq_status") or {}).get("status") == "ok"
            ),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "items": items,
    }
    target = Path(output_path) if output_path else _output_path()
    _write_payload(target, payload)
    return payload
