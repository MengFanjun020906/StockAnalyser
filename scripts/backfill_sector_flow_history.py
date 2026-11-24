#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线回补板块资金流历史快照（FAC-01b 评估用）。

只读生产代码：仅导入 ``sector_flow`` 的纯计算函数，不修改任何生产逻辑。
所有产物写入 ``data/factor_eval_cache/sector_flow_backfill/``，与真实采集目录隔离。

口径（已实测验证）：
- 资金流：``akshare.stock_sector_fund_flow_hist``，120 个交易日，单位 yuan；
- 5 日涨幅：``akshare.stock_board_industry_hist_em`` 不复权收盘，
  收盘(窗口最后日) / 收盘(窗口首日前一交易日) - 1。
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import akshare as ak  # noqa: E402

from src.agent.candidate_experts_v2.sector_flow import (  # noqa: E402
    AkshareSectorSource,
    _apply_cross_section,
    _build_board_record,
    _now_iso,
    _snapshot_meta,
    load_settings,
)

DEFAULT_CACHE = ROOT / "data" / "factor_eval_cache"
DEFAULT_OUT = DEFAULT_CACHE / "sector_flow_backfill"
RECONSTRUCTION = {
    "mode": "offline_backfill",
    "flow_source": "akshare.stock_sector_fund_flow_hist",
    "sector_return_source": "akshare.stock_board_industry_hist_em",
    "sector_return_basis": "close(last window day) / close(previous trading day before window start) - 1",
    "adjust": "none",
    "verified_error_pp": "<=0.004 vs online snapshot (8 boards, 2 days)",
}


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _retry(fn, *, attempts: int, backoff: float) -> Tuple[Optional[Any], int, str]:
    last = ""
    for i in range(max(1, attempts)):
        try:
            return fn(), i, ""
        except Exception as exc:  # noqa: BLE001 - upstream errors are broad
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(backoff * (i + 1) + random.uniform(0, 1))
    return None, max(1, attempts), last


def load_catalog(cache_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Path]:
    files = sorted(cache_dir.glob("sector_flow_snapshot_*.json"))
    if not files:
        raise SystemExit("no real sector_flow_snapshot_*.json found")
    source = files[-1]
    payload = json.loads(source.read_text(encoding="utf-8"))
    catalog = payload.get("board_catalog") or {}
    if not catalog:
        raise SystemExit(f"catalog missing in {source}")
    return catalog, source


def resolve_calendar(
    catalog: Dict[str, Dict[str, Any]], attempts: int, backoff: float
) -> List[str]:
    source = AkshareSectorSource()
    names = [catalog[c]["bk_name"] for c in sorted(catalog)]
    for name in names[:5]:
        rows, _, err = _retry(
            lambda n=name: source.board_history(n, 200),
            attempts=attempts,
            backoff=backoff,
        )
        dates = sorted({str(r["date"]) for r in (rows or []) if r.get("date")})
        if len(dates) >= 70:
            log(f"calendar from {name}: {len(dates)} trading days ({dates[0]} -> {dates[-1]})")
            return dates
        log(f"calendar probe {name} failed: {err or 'insufficient rows'}")
    raise SystemExit("unable to resolve trading calendar from upstream")


def _flow_rows_via_code(bk_code: str) -> List[Dict[str, Any]]:
    """Fallback for boards missing from akshare's industry name map."""

    import requests

    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "lmt": "0",
        "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "secid": f"90.{bk_code}",
    }
    response = requests.get(url, params=params, timeout=20)
    klines = (response.json() or {}).get("data", {}).get("klines") or []
    rows: List[Dict[str, Any]] = []
    for item in klines:
        parts = item.split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "date": parts[0],
                "main_amount": float(parts[1]),
                "main_amount_pct": float(parts[6]),
                "super_big_amount": float(parts[5]),
                "big_amount": float(parts[4]),
            }
        )
    return rows


def resolve_calendar_from_cache(cache_dir: Path, needed: int) -> List[str]:
    """Reuse trading dates already cached so resume runs never depend on upstream."""

    dates: set[str] = set()
    for path in cache_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for row in data.get("flow_rows") or []:
            if row.get("date"):
                dates.add(str(row["date"]))
        if len(dates) >= needed:
            break
    ordered = sorted(dates)
    return ordered if len(ordered) >= needed else []


def fetch_board(
    code: str,
    meta: Dict[str, Any],
    *,
    cache_dir: Path,
    index_start: str,
    index_end: str,
    keep_dates: Sequence[str],
    attempts: int,
    backoff: float,
) -> Dict[str, Any]:
    path = cache_dir / f"{code}.json"
    cached: Dict[str, Any] = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupted cache should be refetched
            cached = {}
    covered = cached.get("keep_from", "9999") <= keep_dates[0] and cached.get(
        "keep_to", ""
    ) >= keep_dates[-1]
    flow_rows = list(cached.get("flow_rows") or []) if covered else []
    closes: Dict[str, float] = dict(cached.get("closes") or {}) if covered else {}
    if flow_rows and closes:
        return cached

    name = meta["bk_name"]
    keep = set(keep_dates)
    flow_errs = 0
    flow_err = ""
    if not flow_rows:
        flow_rows, flow_errs, flow_err = _retry(
            lambda: AkshareSectorSource().board_history(name, 200),
            attempts=attempts,
            backoff=backoff,
        )
        flow_rows = flow_rows or []
        if not flow_rows and "KeyError" in flow_err:
            # 行业历史资金流的名称映射表缺失时，直接用快照目录里的 BK 代码访问同一端点。
            flow_rows, flow_errs2, flow_err2 = _retry(
                lambda: _flow_rows_via_code(code),
                attempts=attempts,
                backoff=backoff,
            )
            flow_rows = flow_rows or []
            flow_errs += flow_errs2
            flow_err = flow_err or flow_err2
            if flow_rows:
                flow_err = "resolved_by_bk_code"
    flow_rows = [r for r in flow_rows if str(r.get("date")) in keep]

    idx_errs = 0
    idx_err = ""
    if flow_rows and not closes:
        df, idx_errs, idx_err = _retry(
            lambda: ak.stock_board_industry_hist_em(
                symbol=name,
                start_date=index_start,
                end_date=index_end,
                period="日k",
                adjust="",
            ),
            attempts=attempts,
            backoff=backoff,
        )
        if df is not None:
            for _, row in df.iterrows():
                day = str(row.get("日期") or "").strip()
                if day and day in keep:
                    try:
                        closes[day] = float(row.get("收盘"))
                    except (TypeError, ValueError):
                        continue
        if not closes:
            idx_err = idx_err or "empty_index_history"

    ok = bool(flow_rows)
    payload = {
        "bk_code": code,
        "bk_name": name,
        "ok": ok,
        "index_ok": bool(closes),
        "fetched_at": _now_iso(),
        "keep_from": keep_dates[0],
        "keep_to": keep_dates[-1],
        "flow_rows": [r for r in (flow_rows or []) if str(r.get("date")) in keep],
        "closes": closes,
        "errors": {"flow": flow_err, "index": idx_err},
        "retries": {"flow": flow_errs, "index": idx_errs},
        "catalog_member_count": meta.get("member_count"),
    }
    _write_json(path, payload)
    return payload


def build(
    *,
    catalog: Dict[str, Dict[str, Any]],
    cache_dir: Path,
    out_dir: Path,
    days: int,
) -> Dict[str, Any]:
    settings = load_settings()
    window_days = max(1, int(settings.get("window_days") or 5))
    files = sorted(cache_dir.glob("*.json"))
    boards: Dict[str, Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []
    partial_return: List[Dict[str, Any]] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("ok"):
            failures.append(
                {
                    "bk_code": data.get("bk_code"),
                    "bk_name": data.get("bk_name"),
                    "errors": data.get("errors"),
                    "retries": data.get("retries"),
                }
            )
            continue
        boards[data["bk_code"]] = data
        if not data.get("closes"):
            partial_return.append(
                {
                    "bk_code": data.get("bk_code"),
                    "bk_name": data.get("bk_name"),
                    "reason": (data.get("errors") or {}).get("index") or "empty_index_history",
                }
            )

    sample_dates = {
        str(r["date"]) for data in boards.values() for r in data["flow_rows"] if r.get("date")
    }
    calendar = sorted(sample_dates)
    if len(calendar) < days + window_days:
        raise SystemExit(f"insufficient calendar: {len(calendar)} < {days + window_days}")
    targets = calendar[-days:]
    log(f"building {len(targets)} snapshots from {len(boards)}/{len(catalog)} boards")

    overwritten = 0
    trigger_stats = {"board_days": 0, "over_8pct": 0, "max_return": None, "max_board": None}
    for target in targets:
        ti = calendar.index(target)
        win = calendar[ti - window_days + 1 : ti + 1]
        base_date = calendar[ti - window_days]
        raw: Dict[str, Dict[str, Any]] = {}
        for code, data in boards.items():
            rows = data["flow_rows"]
            closes = data["closes"]
            last_close = closes.get(win[-1])
            base_close = closes.get(base_date)
            ret = (
                round((last_close / base_close - 1) * 100, 4)
                if last_close and base_close
                else None
            )
            record = _build_board_record(
                catalog.get(code) or {"bk_code": code, "bk_name": data["bk_name"]},
                rows,
                window_dates=win,
                window_days=window_days,
                sector_return=ret,
                error="",
            )
            record["return_source"] = (
                "reconstructed:stock_board_industry_hist_em"
                if ret is not None
                else "missing:index_unavailable"
            )
            record["return_status"] = "ok" if ret is not None else "partial"
            raw[code] = record
            if ret is not None:
                trigger_stats["board_days"] += 1
                if trigger_stats["max_return"] is None or ret > trigger_stats["max_return"]:
                    trigger_stats["max_return"] = ret
                    trigger_stats["max_board"] = data["bk_name"]
                if ret > 8:
                    trigger_stats["over_8pct"] += 1

        _apply_cross_section(raw, settings, window_days)
        payload: Dict[str, Any] = {
            "version": 1,
            "as_of_date": target,
            "window_dates": list(win),
            "generated_at": _now_iso(),
            "window_days": window_days,
            "unit": "yuan",
            "source": "backfill",
            "reconstruction": dict(RECONSTRUCTION, base_date=base_date),
            "source_chain": [
                RECONSTRUCTION["flow_source"],
                RECONSTRUCTION["sector_return_source"],
            ],
            "board_catalog": catalog,
            "boards": raw,
            "partial_return_boards": partial_return,
            "errors": {},
        }
        payload.update(_snapshot_meta(raw, {}, catalog))
        out_path = out_dir / f"sector_flow_snapshot_backfill_{target}.json"
        if out_path.exists():
            overwritten += 1
        _write_json(out_path, payload)

    distinct_over = sorted(
        [
            (p.name, json.loads(p.read_text(encoding="utf-8")))
            for p in out_dir.glob("sector_flow_snapshot_backfill_*.json")
        ]
    )
    over_boards: Dict[str, int] = {}
    for _, snap in distinct_over:
        for record in snap["boards"].values():
            if (record.get("overheat_penalty") or 0) > 0:
                over_boards[record["bk_name"]] = over_boards.get(record["bk_name"], 0) + 1

    return {
        "days": days,
        "dates": targets,
        "coverage": round(len(boards) / len(catalog), 4) if catalog else 0.0,
        "boards_ok": len(boards),
        "boards_with_return": len(boards) - len(partial_return),
        "partial_return": partial_return,
        "catalog_size": len(catalog),
        "failures": failures,
        "overwritten_snapshots": overwritten,
        "trigger_stats": trigger_stats,
        "boards_with_penalty": dict(sorted(over_boards.items(), key=lambda kv: -kv[1])),
        "output_dir": str(out_dir),
    }


def crosscheck(
    *,
    out_dir: Path,
    real_dir: Path,
    tolerance: Dict[str, float],
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"dates": {}, "max_abs_diff": {}, "mismatch_counts": {}}
    fields = ("latest_net_inflow", "sector_return_window", "overheat_penalty", "score")
    for real_path in sorted(real_dir.glob("sector_flow_snapshot_*.json")):
        day = real_path.stem.replace("sector_flow_snapshot_", "")
        rebuilt_path = out_dir / f"sector_flow_snapshot_backfill_{day}.json"
        if not rebuilt_path.exists():
            continue
        real = json.loads(real_path.read_text(encoding="utf-8"))
        new = json.loads(rebuilt_path.read_text(encoding="utf-8"))
        per_day = {"boards_compared": 0, "state_mismatch": 0, "missing": 0, "field_diffs": {}}
        for code, rb in real["boards"].items():
            nb = new["boards"].get(code)
            if not nb:
                per_day["missing"] += 1
                continue
            per_day["boards_compared"] += 1
            if rb.get("state") != nb.get("state"):
                per_day["state_mismatch"] += 1
            for field in fields:
                rv = (rb.get("metrics") or {}).get(field) if field == "latest_net_inflow" else rb.get(field)
                nv = (nb.get("metrics") or {}).get(field) if field == "latest_net_inflow" else nb.get(field)
                if rv is None or nv is None:
                    continue
                diff = abs(float(rv) - float(nv))
                bucket = per_day["field_diffs"].setdefault(
                    field, {"max": 0.0, "gt_tol": 0, "tol": tolerance.get(field, 0.01)}
                )
                bucket["max"] = round(max(bucket["max"], diff), 6)
                if diff > bucket["tol"]:
                    bucket["gt_tol"] += 1
        report["dates"][day] = per_day
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--backoff", type=float, default=3.0)
    parser.add_argument("--max-minutes", type=float, default=40.0)
    parser.add_argument("--cache-dir", default=str(DEFAULT_OUT / "cache"))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT / "snapshots"))
    parser.add_argument("--manifest", default=str(DEFAULT_OUT / "manifest.json"))
    parser.add_argument("--codes", default="", help="comma separated bk codes to refetch only")
    args = parser.parse_args()

    socket.setdefaulttimeout(25)
    cache_dir = Path(args.cache_dir)
    out_dir = Path(args.out_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    catalog, catalog_source = load_catalog(DEFAULT_CACHE)
    log(f"catalog {len(catalog)} boards from {catalog_source.name}")

    calendar = resolve_calendar_from_cache(cache_dir, args.days + 5)
    if calendar:
        log(f"calendar from cache: {len(calendar)} trading days ({calendar[0]} -> {calendar[-1]})")
    else:
        calendar = resolve_calendar(catalog, args.attempts, args.backoff)
    days = min(args.days, len(calendar) - 5)
    targets = calendar[-days:]
    keep = calendar[len(calendar) - days - 6 :]
    index_start = (
        datetime.strptime(calendar[len(calendar) - days - 6], "%Y-%m-%d") - timedelta(days=20)
    ).strftime("%Y%m%d")
    index_end = targets[-1].replace("-", "")

    started = time.time()
    def _complete(code: str) -> bool:
        path = cache_dir / f"{code}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        return bool(data.get("ok")) and bool(data.get("closes"))

    only = {c.strip() for c in args.codes.split(",") if c.strip()}
    pending = [
        (code, meta)
        for code, meta in sorted(catalog.items())
        if not _complete(code) and (not only or code in only)
    ]
    log(f"fetching {len(pending)} boards (workers={args.workers}, attempts={args.attempts})")

    done = 0
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {
        pool.submit(
            fetch_board,
            code,
            meta,
            cache_dir=cache_dir,
            index_start=index_start,
            index_end=index_end,
            keep_dates=keep,
            attempts=args.attempts,
            backoff=args.backoff,
        ): code
        for code, meta in pending
    }
    try:
        for future in as_completed(futures):
            code = futures[future]
            try:
                result = future.result()
                state = "ok" if result.get("ok") else "fail"
            except Exception as exc:  # noqa: BLE001 - keep going per requirement
                state = f"error:{type(exc).__name__}"
            done += 1
            if done % 20 == 0 or done == len(pending):
                ok_count = len(list(cache_dir.glob("*.json")))
                log(f"progress {done}/{len(pending)} processed, cache files={ok_count}, last={state}")
            if time.time() - started > args.max_minutes * 60:
                log("max minutes reached, stopping further fetches")
                break
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    elapsed = round(time.time() - started, 1)
    cached = list(cache_dir.glob("*.json"))
    ok_before = 0
    for path in cached:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("ok") and data.get("closes"):
            ok_before += 1
    ok_boards = 0
    complete_after = 0
    for path in cached:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("ok"):
            ok_boards += 1
        if data.get("ok") and data.get("closes"):
            complete_after += 1
    coverage = round(ok_boards / len(catalog), 4) if catalog else 0.0
    log(f"fetch phase done in {elapsed}s: ok={ok_boards}/{len(catalog)} coverage={coverage}")

    if complete_after <= ok_before:
        log(
            "no new data; manifest and snapshots left untouched "
            f"(complete={complete_after}, before={ok_before}, coverage={coverage})"
        )
        return 3

    if coverage < 0.8:
        _write_json(
            Path(args.manifest),
            {
                "status": "aborted_low_coverage",
                "coverage": coverage,
                "boards_ok": ok_boards,
                "catalog_size": len(catalog),
                "elapsed_s": elapsed,
                "cache_dir": str(cache_dir),
            },
        )
        log("coverage below 80%, aborting before snapshot build")
        return 2

    result = build(catalog=catalog, cache_dir=cache_dir, out_dir=out_dir, days=days)
    result.update(
        {
            "status": "ok",
            "catalog_source": str(catalog_source),
            "reconstruction": RECONSTRUCTION,
            "fetch_elapsed_s": elapsed,
            "crosscheck": crosscheck(
                out_dir=out_dir,
                real_dir=DEFAULT_CACHE,
                tolerance={"latest_net_inflow": 0.05, "sector_return_window": 0.01, "overheat_penalty": 0.0005, "score": 0.0005},
            ),
        }
    )
    _write_json(Path(args.manifest), result)
    log(f"manifest written: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
