#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a small/base Kronos pretrained model A/B evaluation.

The script evaluates model-backed forecasts at historical cutoffs.  It avoids
mutating process-wide environment variables and does not persist Kronos signals
to the application database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import get_config
from src.services.history_loader import _history_code_candidates, _load_sequoia_history_df, load_history_df
from src.services.kronos_data_adapter import normalize_a_share_code
from src.services.kronos_signal_service import KronosSignalService


MODEL_ALIASES = {
    "mini": "NeoQuasar/Kronos-mini",
    "small": "NeoQuasar/Kronos-small",
    "base": "NeoQuasar/Kronos-base",
}

DEFAULT_SYMBOLS = [
    "600519",
    "300750",
    "601318",
    "000858",
    "002594",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Kronos pretrained models on historical A-share daily cutoffs.")
    parser.add_argument("--models", default="small,base", help="Comma-separated model aliases or HF ids. Defaults to small,base.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated A-share symbols.")
    parser.add_argument(
        "--target-date",
        action="append",
        dest="target_dates",
        default=[],
        help="Historical cutoff date in YYYY-MM-DD. Can repeat. Default: 2026-06-30.",
    )
    parser.add_argument("--lookback", type=int, default=160, help="Daily bars used as model context.")
    parser.add_argument("--pred-len", type=int, default=5, help="Forecast horizon in trading bars.")
    parser.add_argument("--sample-count", type=int, default=1, help="Kronos sampling count per prediction.")
    parser.add_argument("--top-p", type=float, default=None, help="Override Kronos top_p.")
    parser.add_argument("--temperature", type=float, default=None, help="Override Kronos sampling temperature.")
    parser.add_argument("--device", default=None, help="Override KRONOS_DEVICE, e.g. cpu, mps, cuda, auto.")
    parser.add_argument("--seed", type=int, default=100, help="Deterministic base seed. Use -1 to disable per-case seeding.")
    parser.add_argument("--allow-network", action="store_true", help="Allow history_loader network fallback for missing daily bars.")
    parser.add_argument("--output-dir", default="data/run/kronos_ab", help="Directory for JSON/CSV experiment artifacts.")
    parser.add_argument("--label", default="", help="Optional run label included in artifact names.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first model/symbol failure.")
    return parser.parse_args()


def parse_models(value: str) -> List[str]:
    models: List[str] = []
    for item in str(value or "").split(","):
        text = item.strip()
        if not text:
            continue
        models.append(MODEL_ALIASES.get(text, text))
    return models or [MODEL_ALIASES["small"], MODEL_ALIASES["base"]]


def parse_symbols(value: str) -> List[str]:
    symbols: List[str] = []
    for item in str(value or "").split(","):
        code = normalize_a_share_code(item)
        if code and code not in symbols:
            symbols.append(code)
    return symbols


def parse_target_dates(values: Iterable[str]) -> List[date]:
    parsed: List[date] = []
    source = list(values) or ["2026-06-30"]
    for item in source:
        parsed.append(datetime.strptime(str(item).strip(), "%Y-%m-%d").date())
    return parsed


def direction(value: Optional[float], *, neutral_band_pct: float = 0.0) -> str:
    if value is None or not math.isfinite(float(value)):
        return "unknown"
    number = float(value)
    if number > neutral_band_pct:
        return "up"
    if number < -neutral_band_pct:
        return "down"
    return "flat"


def round_or_none(value: Any, digits: int = 4) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def seed_case(base_seed: int, *parts: str) -> Optional[int]:
    if int(base_seed) < 0:
        return None
    digest = hashlib.sha256("|".join([str(base_seed), *parts]).encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big") % (2**31 - 1)
    random.seed(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except Exception:
        pass
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except Exception:
        pass
    return seed


def load_direct_main_history_df(code: str, start: date, end: date) -> Tuple[Optional[pd.DataFrame], str]:
    db_path = Path(os.getenv("DATABASE_PATH") or "data/stock_analysis.db").expanduser()
    if not db_path.exists():
        return None, "main_stock_daily_missing"
    candidates, _normalized = _history_code_candidates(code)
    if not candidates:
        return None, "main_stock_daily_missing_symbol"
    placeholders = ",".join("?" * len(candidates))
    params = [*candidates, start.isoformat(), end.isoformat()]
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT code, date, open, high, low, close, volume, amount
                    FROM stock_daily
                    WHERE code IN ({placeholders})
                      AND date >= ?
                      AND date <= ?
                    ORDER BY date""",
                params,
            ).fetchall()
    except Exception:
        return None, "main_stock_daily_read_failed"
    if not rows:
        return None, "main_stock_daily_empty"
    return pd.DataFrame([dict(row) for row in rows]), "main_stock_daily_direct"


def load_eval_history_df(code: str, *, target_date: date, horizon: int, lookback: int, fallback_to_network: bool) -> Tuple[Optional[pd.DataFrame], str]:
    future_end = target_date + timedelta(days=max(20, int(horizon) * 4 + 10))
    days = max(int(lookback) + int(horizon) + 20, 80)
    df, source = load_history_df(
        code,
        days=days,
        target_date=future_end,
        fallback_to_network=fallback_to_network,
    )
    if df is not None and not df.empty:
        return df, source

    start = target_date - timedelta(days=max(10, int(horizon) * 2))
    direct_end = target_date + timedelta(days=max(60, int(horizon) * 5 + 20))
    direct_df, direct_source = load_direct_main_history_df(code, start, direct_end)
    if direct_df is not None and not direct_df.empty:
        return direct_df, direct_source

    sequoia_df = _load_sequoia_history_df(code, start, direct_end)
    if sequoia_df is not None and not sequoia_df.empty:
        return sequoia_df, "sequoia_stock_daily_direct"

    return None, source or direct_source


def load_actual_future(
    code: str,
    *,
    target_date: date,
    horizon: int,
    lookback: int,
    last_close: Optional[float],
    fallback_to_network: bool,
) -> Dict[str, Any]:
    if last_close is None or not math.isfinite(last_close) or last_close <= 0:
        return {"status": "missing_last_close"}

    df, source = load_eval_history_df(
        code,
        target_date=target_date,
        horizon=horizon,
        lookback=lookback,
        fallback_to_network=fallback_to_network,
    )
    if df is None or df.empty:
        return {"status": "missing_future_history", "source": source}

    frame = df.copy()
    timestamp_col = next((col for col in ["timestamp", "date", "datetime", "trade_date", "time"] if col in frame.columns), None)
    if timestamp_col is None:
        return {"status": "missing_timestamp", "source": source}
    frame["timestamp"] = pd.to_datetime(frame[timestamp_col], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "close"]).sort_values("timestamp")
    future = frame[frame["timestamp"].dt.date > target_date].reset_index(drop=True)
    if len(future) < horizon:
        return {
            "status": "insufficient_future_bars",
            "source": source,
            "available_future_bars": int(len(future)),
        }

    end_row = future.iloc[int(horizon) - 1]
    actual_close = float(end_row["close"])
    actual_return_pct = (actual_close / float(last_close) - 1.0) * 100.0
    future_close = pd.to_numeric(future.head(int(horizon))["close"], errors="coerce").dropna()
    return {
        "status": "ok",
        "source": source,
        "future_date": pd.Timestamp(end_row["timestamp"]).date().isoformat(),
        "actual_close": round_or_none(actual_close),
        "actual_return_pct": round_or_none(actual_return_pct),
        "max_actual_return_pct": round_or_none((float(future_close.max()) / float(last_close) - 1.0) * 100.0) if not future_close.empty else None,
        "min_actual_return_pct": round_or_none((float(future_close.min()) / float(last_close) - 1.0) * 100.0) if not future_close.empty else None,
        "available_future_bars": int(len(future)),
    }


def make_service(model_name: str, args: argparse.Namespace) -> KronosSignalService:
    base = get_config()
    config = replace(
        base,
        kronos_enabled=True,
        kronos_model_path=model_name,
        kronos_cache_signals=False,
        kronos_default_lookback_daily=int(args.lookback),
        kronos_default_pred_len_daily=int(args.pred_len),
        kronos_sample_count=int(args.sample_count),
        kronos_top_p=float(args.top_p if args.top_p is not None else base.kronos_top_p),
        kronos_temperature=float(args.temperature if args.temperature is not None else base.kronos_temperature),
        kronos_device=str(args.device if args.device is not None else base.kronos_device),
    )
    return KronosSignalService(config=config)


def run_case(
    service: KronosSignalService,
    *,
    model_name: str,
    code: str,
    target_date: date,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    seed = seed_case(int(args.seed), model_name, code, target_date.isoformat(), str(args.pred_len))
    started = time.perf_counter()
    result = service.analyze(
        code,
        lookback=int(args.lookback),
        pred_len=int(args.pred_len),
        target_date=target_date,
        fallback_to_network=bool(args.allow_network),
        persist=False,
    )
    elapsed_sec = time.perf_counter() - started

    summary = result.get("forecast_summary") or {}
    signal = result.get("signal") or {}
    patterns = result.get("pattern_instances") or []
    pattern_types = sorted({str(item.get("pattern_type")) for item in patterns if item.get("pattern_type")})
    pattern_labels = sorted({str(item.get("label")) for item in patterns if item.get("label")})
    pattern_confirmations = sorted(
        {
            str((item.get("evidence") or {}).get("kronos_confirmation"))
            for item in patterns
            if (item.get("evidence") or {}).get("kronos_confirmation")
        }
    )
    last_close = round_or_none(summary.get("last_close"))
    predicted_return_pct = round_or_none(summary.get("expected_return_pct"))
    actual = load_actual_future(
        code,
        target_date=target_date,
        horizon=int(args.pred_len),
        lookback=int(args.lookback),
        last_close=last_close,
        fallback_to_network=bool(args.allow_network),
    )
    actual_return_pct = round_or_none(actual.get("actual_return_pct"))
    predicted_direction = direction(predicted_return_pct)
    actual_direction = direction(actual_return_pct)
    evaluated = result.get("status") == "ok" and actual.get("status") == "ok"

    return {
        "model": model_name,
        "symbol": code,
        "target_date": target_date.isoformat(),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_sec": round_or_none(elapsed_sec, 3),
        "as_of": result.get("as_of"),
        "last_close": last_close,
        "predicted_final_close": round_or_none(summary.get("final_close")),
        "predicted_return_pct": predicted_return_pct,
        "predicted_direction": predicted_direction,
        "signal_bias": signal.get("bias"),
        "signal_confidence": round_or_none(signal.get("confidence")),
        "pattern_count": len(patterns),
        "pattern_types": pattern_types,
        "pattern_labels": pattern_labels,
        "pattern_confirmations": pattern_confirmations,
        "data_source": (result.get("data_quality") or {}).get("source"),
        "actual_status": actual.get("status"),
        "actual_source": actual.get("source"),
        "actual_future_date": actual.get("future_date"),
        "actual_close": round_or_none(actual.get("actual_close")),
        "actual_return_pct": actual_return_pct,
        "actual_direction": actual_direction,
        "direction_correct": bool(predicted_direction == actual_direction) if evaluated and predicted_direction != "flat" else None,
        "absolute_return_error_pct": round_or_none(abs(float(predicted_return_pct) - float(actual_return_pct))) if evaluated and predicted_return_pct is not None and actual_return_pct is not None else None,
        "max_actual_return_pct": round_or_none(actual.get("max_actual_return_pct")),
        "min_actual_return_pct": round_or_none(actual.get("min_actual_return_pct")),
        "source_chain": result.get("source_chain") or [],
        "model_profile": result.get("model_profile") or {},
        "diagnostics": result.get("diagnostics") or {},
        "seed": seed,
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    summary: Dict[str, Dict[str, Any]] = {}
    for model in sorted({str(row["model"]) for row in rows}):
        model_rows = [row for row in rows if row["model"] == model]
        evaluated = [row for row in model_rows if row.get("status") == "ok" and row.get("actual_status") == "ok"]
        direction_values = [row.get("direction_correct") for row in evaluated if row.get("direction_correct") is not None]
        errors = [float(row["absolute_return_error_pct"]) for row in evaluated if row.get("absolute_return_error_pct") is not None]
        elapsed = [float(row["elapsed_sec"]) for row in model_rows if row.get("elapsed_sec") is not None]
        predicted = [float(row["predicted_return_pct"]) for row in evaluated if row.get("predicted_return_pct") is not None]
        actual = [float(row["actual_return_pct"]) for row in evaluated if row.get("actual_return_pct") is not None]
        pattern_type_counts: Dict[str, int] = {}
        pattern_returns: Dict[str, List[float]] = {}
        for row in evaluated:
            for pattern_type in row.get("pattern_types") or []:
                pattern_type_counts[pattern_type] = pattern_type_counts.get(pattern_type, 0) + 1
                if row.get("actual_return_pct") is not None:
                    pattern_returns.setdefault(pattern_type, []).append(float(row["actual_return_pct"]))
        summary[model] = {
            "cases": len(model_rows),
            "ok_cases": sum(1 for row in model_rows if row.get("status") == "ok"),
            "evaluated_cases": len(evaluated),
            "direction_accuracy": round_or_none(sum(1 for item in direction_values if item) / len(direction_values), 4) if direction_values else None,
            "mean_absolute_return_error_pct": round_or_none(statistics.fmean(errors)) if errors else None,
            "mean_elapsed_sec": round_or_none(statistics.fmean(elapsed), 3) if elapsed else None,
            "mean_predicted_return_pct": round_or_none(statistics.fmean(predicted)) if predicted else None,
            "mean_actual_return_pct": round_or_none(statistics.fmean(actual)) if actual else None,
            "pattern_type_counts": dict(sorted(pattern_type_counts.items())),
            "pattern_mean_actual_return_pct": {
                pattern_type: round_or_none(statistics.fmean(values))
                for pattern_type, values in sorted(pattern_returns.items())
                if values
            },
            "failures": [
                {
                    "symbol": row.get("symbol"),
                    "target_date": row.get("target_date"),
                    "status": row.get("status"),
                    "reason": row.get("reason") or row.get("actual_status"),
                }
                for row in model_rows
                if row.get("status") != "ok" or row.get("actual_status") != "ok"
            ],
        }
    return summary


def write_outputs(output_dir: Path, label: str, payload: Dict[str, Any], rows: List[Dict[str, Any]]) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label.strip()}" if label.strip() else ""
    json_path = output_dir / f"kronos-model-ab{suffix}-{stamp}.json"
    csv_path = output_dir / f"kronos-model-ab{suffix}-{stamp}.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "model",
        "symbol",
        "target_date",
        "status",
        "reason",
        "elapsed_sec",
        "as_of",
        "last_close",
        "predicted_final_close",
        "predicted_return_pct",
        "predicted_direction",
        "signal_bias",
        "signal_confidence",
        "pattern_count",
        "pattern_types",
        "pattern_labels",
        "pattern_confirmations",
        "data_source",
        "actual_status",
        "actual_source",
        "actual_future_date",
        "actual_close",
        "actual_return_pct",
        "actual_direction",
        "direction_correct",
        "absolute_return_error_pct",
        "max_actual_return_pct",
        "min_actual_return_pct",
        "seed",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            output_row = {key: row.get(key) for key in fieldnames}
            for key in ("pattern_types", "pattern_labels", "pattern_confirmations"):
                if isinstance(output_row.get(key), list):
                    output_row[key] = "|".join(str(item) for item in output_row[key])
            writer.writerow(output_row)
    return json_path, csv_path


def main() -> int:
    args = parse_args()
    models = parse_models(args.models)
    symbols = parse_symbols(args.symbols)
    target_dates = parse_target_dates(args.target_dates)
    if not symbols:
        raise SystemExit("No supported A-share symbols were provided.")

    started_at = datetime.now()
    rows: List[Dict[str, Any]] = []
    for model_name in models:
        service = make_service(model_name, args)
        for code in symbols:
            for cutoff in target_dates:
                try:
                    row = run_case(service, model_name=model_name, code=code, target_date=cutoff, args=args)
                except Exception as exc:
                    row = {
                        "model": model_name,
                        "symbol": code,
                        "target_date": cutoff.isoformat(),
                        "status": "exception",
                        "reason": str(exc),
                        "elapsed_sec": None,
                        "actual_status": "not_evaluated",
                    }
                    if args.fail_fast:
                        rows.append(row)
                        raise
                rows.append(row)
                print(
                    f"{model_name} {code} {cutoff.isoformat()} "
                    f"status={row.get('status')} actual={row.get('actual_status')} "
                    f"pred={row.get('predicted_return_pct')} actual_ret={row.get('actual_return_pct')} "
                    f"elapsed={row.get('elapsed_sec')}s"
                )

    payload = {
        "created_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "models": models,
            "symbols": symbols,
            "target_dates": [item.isoformat() for item in target_dates],
            "lookback": int(args.lookback),
            "pred_len": int(args.pred_len),
            "sample_count": int(args.sample_count),
            "top_p": args.top_p,
            "temperature": args.temperature,
            "device": args.device,
            "seed": int(args.seed),
            "allow_network": bool(args.allow_network),
        },
        "summary": summarize(rows),
        "rows": rows,
    }
    json_path, csv_path = write_outputs(Path(args.output_dir), args.label, payload, rows)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"Wrote JSON: {json_path}")
    print(f"Wrote CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
