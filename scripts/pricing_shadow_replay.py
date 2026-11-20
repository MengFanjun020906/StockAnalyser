#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回放历史选股候选, 汇总确定性点位引擎的 shadow 分布.

用途:
- 遍历 ``data/agent_traces/*/selection_context.json`` 中带有效 ``as_of`` 的
  选股 run, 按各 run 自己的 ``as_of`` 调用 ``run_pricing_shadow_probe``;
- 样本按 ``(as_of, code)`` 去重, 同一交易日重复运行产生的同码候选只跑一次;
- 输出整体 ``by_status`` / ``by_reason_code``, 并把
  ``MISSING_HISTORICAL_CHIP_SNAPSHOT`` 单列一层;
- 按交易日、板块、行业、市值分档给出状态差异;
- 原始结果落 ``<out>/work/``, 汇总报告落 ``<out>/outputs/``。

脚本只读业务表: 走 ``run_pricing_shadow_probe(persist=False)`` 同一条链路,
不写 ranking / action / report, 也不改点位算法。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The probe is default-off in production; the replay script is the only caller
# that turns it on, and only inside this process.
os.environ.setdefault("AGENT_PRICING_SHADOW_ENABLED", "1")

from src.agent.pricing.params import PRICE_PLAN_PARAMS_V1  # noqa: E402
from src.agent.pricing.shadow import (  # noqa: E402
    pricing_shadow_enabled,
    run_pricing_shadow_probe,
)
from src.storage import DatabaseManager  # noqa: E402

CHIP_MISSING = "MISSING_HISTORICAL_CHIP_SNAPSHOT"
REQUIRED_BARS = (
    PRICE_PLAN_PARAMS_V1.daily_evaluation_bars
    + PRICE_PLAN_PARAMS_V1.daily_warmup_bars
)
RW_BOARDS = (
    ("主板", ("600", "601", "603", "605", "000", "001", "002", "003")),
    ("创业板", ("300", "301", "302")),
    ("科创板", ("688", "689")),
    ("北交所", ("4", "8", "92")),
)
MV_BUCKETS = (
    ("<50亿", 0.0, 5.0e5),
    ("50-100亿", 5.0e5, 1.0e6),
    ("100-300亿", 1.0e6, 3.0e6),
    ("300-1000亿", 3.0e6, 1.0e7),
    (">=1000亿", 1.0e7, float("inf")),
)


# ---------------------------------------------------------------------------
# input collection
# ---------------------------------------------------------------------------

def _dedupe(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _candidate_codes(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    candidates = ((payload.get("full") or {}).get("candidates") or [])
    codes = []
    for item in candidates:
        if isinstance(item, dict):
            codes.append(item.get("code"))
    return _dedupe(codes)


def collect_runs(traces_dir: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for context_path in sorted(traces_dir.glob("*/selection_context.json")):
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_as_of = str((context.get("market_regime") or {}).get("as_of") or "")[:10]
        try:
            as_of = date.fromisoformat(raw_as_of)
        except ValueError:
            continue
        run_dir = context_path.parent
        runs.append(
            {
                "run_id": run_dir.name,
                "as_of": as_of,
                "codes": _candidate_codes(run_dir / "candidate_discovery.json"),
            }
        )
    return runs


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def replay(
    runs: list[dict[str, Any]],
    *,
    work_dir: Path,
    db: DatabaseManager,
    time_budget_seconds: float,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    raw_dir = work_dir / "runs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    probed: dict[tuple[str, str], dict[str, Any]] = {}
    meta: dict[str, Any] = {
        "runs": len(runs),
        "runs_with_candidates": 0,
        "probe_calls": 0,
        "probes": 0,
        "duplicate_candidates_skipped": 0,
        "failed_runs": [],
        "stopped_by_time_budget": False,
        "elapsed_seconds": 0.0,
    }
    started = time.perf_counter()
    for index, run in enumerate(runs, start=1):
        if time.perf_counter() - started > time_budget_seconds:
            meta["stopped_by_time_budget"] = True
            print(f"[stop] time budget exceeded before run {index}/{len(runs)}")
            break
        as_of_text = run["as_of"].isoformat()
        pending = [
            code
            for code in run["codes"]
            if (as_of_text, code) not in probed
        ]
        meta["duplicate_candidates_skipped"] += len(run["codes"]) - len(pending)
        if not pending:
            continue
        meta["runs_with_candidates"] += 1
        try:
            payload = run_pricing_shadow_probe(
                pending,
                as_of=run["as_of"],
                run_id=run["run_id"],
                output_path=raw_dir / f"{run['run_id']}.json",
                max_codes=0,
                db_manager=db,
            )
        except Exception as exc:  # noqa: BLE001 - one run must not stop the replay
            meta["failed_runs"].append(
                {"run_id": run["run_id"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if payload is None:
            continue
        meta["probe_calls"] += 1
        for item in payload.get("items") or []:
            code = str(item.get("code") or "")
            if not code:
                continue
            probed[(as_of_text, code)] = item
            meta["probes"] += 1
        print(
            f"[{index}/{len(runs)}] {run['run_id']} as_of={as_of_text} "
            f"probed={len(pending)} total={len(probed)} "
            f"elapsed={time.perf_counter() - started:.0f}s",
            flush=True,
        )
    meta["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    return probed, meta


# ---------------------------------------------------------------------------
# reference data (board / industry / market cap)
# ---------------------------------------------------------------------------

def _code6(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def board_of(code: str) -> str:
    digits = _code6(code)
    for label, prefixes in RW_BOARDS:
        if digits.startswith(prefixes):
            return label
    return "其他"


def load_industries(cache_dir: Path) -> dict[str, str]:
    path = cache_dir / "stock_basic_LD.csv"
    if not path.exists():
        return {}
    import pandas as pd

    frame = pd.read_csv(path, dtype=str).fillna("")
    mapping: dict[str, str] = {}
    for row in frame.to_dict("records"):
        code = _code6(row.get("ts_code"))
        industry = str(row.get("industry") or "").strip()
        if code and industry:
            mapping[code] = industry
    return mapping


def load_market_caps(cache_dir: Path, as_of: date) -> dict[str, float]:
    """Return ``{code6: total_mv(万元)}`` from the nearest basic_*.csv <= as_of."""
    import pandas as pd

    candidates = sorted(cache_dir.glob("basic_*.csv"))
    chosen = None
    for path in candidates:
        stamp = path.stem.split("_", 1)[-1]
        if len(stamp) == 8 and stamp <= as_of.strftime("%Y%m%d"):
            chosen = path
    if chosen is None:
        return {}
    try:
        frame = pd.read_csv(chosen, dtype=str).fillna("")
    except Exception:
        return {}
    mapping: dict[str, float] = {}
    for row in frame.to_dict("records"):
        code = _code6(row.get("ts_code"))
        try:
            mapping[code] = float(row.get("total_mv") or 0)
        except (TypeError, ValueError):
            continue
    return mapping


def mv_bucket(total_mv: float | None) -> str:
    if total_mv is None or total_mv <= 0:
        return "unknown"
    for label, low, high in MV_BUCKETS:
        if low <= total_mv < high:
            return label
    return "unknown"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _status(item: dict[str, Any]) -> str:
    return str(item.get("status") or "unknown")


def _reasons(item: dict[str, Any]) -> list[str]:
    return [str(reason) for reason in item.get("reason_codes") or []]


def _has_chip_missing(item: dict[str, Any]) -> bool:
    return CHIP_MISSING in _reasons(item)


def _evaluable(item: dict[str, Any]) -> bool:
    """True when the snapshot actually carried enough daily bars to evaluate."""
    return int(item.get("cash_bars") or 0) >= REQUIRED_BARS


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": None if denominator == 0 else round(numerator / denominator, 4),
    }


def load_work_items(
    work_dir: Path,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Reload a previous replay so aggregation/report can be rebuilt offline."""
    probed: dict[tuple[str, str], dict[str, Any]] = {}
    for line in (work_dir / "items.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        as_of = str(payload.pop("as_of"))
        code = str(payload.pop("code"))
        probed[(as_of, code)] = payload
    previous = json.loads((work_dir / "summary.json").read_text(encoding="utf-8"))["meta"]
    meta = {
        key: previous.get(key)
        for key in (
            "runs",
            "runs_with_candidates",
            "probe_calls",
            "probes",
            "duplicate_candidates_skipped",
            "failed_runs",
            "stopped_by_time_budget",
            "elapsed_seconds",
        )
    }
    meta["reused_from_items"] = True
    return probed, meta


def summarize(
    probed: dict[tuple[str, str], dict[str, Any]],
    runs: list[dict[str, Any]],
    meta: dict[str, Any],
    *,
    cache_dir: Path,
) -> dict[str, Any]:
    items = [{"as_of": pair[0], "code": pair[1], **item} for pair, item in probed.items()]
    status_counts = Counter(_status(item) for item in items)
    reason_counts = Counter(reason for item in items for reason in _reasons(item))

    chip_items = [item for item in items if _has_chip_missing(item)]
    clean_items = [item for item in items if not _has_chip_missing(item)]
    generated = sum(1 for item in items if _status(item) == "generated")

    runs_per_day = Counter(run["as_of"].isoformat() for run in runs)
    items_per_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        items_per_day[item["as_of"]].append(item)

    industries = load_industries(cache_dir)
    caps_by_day = {day: load_market_caps(cache_dir, date.fromisoformat(day)) for day in items_per_day}

    def group(items_subset: list[dict[str, Any]], key) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items_subset:
            buckets[key(item)].append(item)
        rows = []
        for label, bucket in buckets.items():
            generated_n = sum(1 for item in bucket if _status(item) == "generated")
            evaluable_n = sum(1 for item in bucket if _evaluable(item))
            rows.append(
                {
                    "group": label,
                    "samples": len(bucket),
                    "no_history": len(bucket) - evaluable_n,
                    "generated": generated_n,
                    "generated_rate": round(generated_n / len(bucket), 4),
                    "evaluable_rate": (
                        None if evaluable_n == 0 else round(generated_n / evaluable_n, 4)
                    ),
                    "chip_missing": sum(1 for item in bucket if _has_chip_missing(item)),
                }
            )
        rows.sort(key=lambda row: (-row["samples"], row["group"]))
        return rows

    by_day = []
    for day in sorted(items_per_day):
        bucket = items_per_day[day]
        evaluable_n = sum(1 for item in bucket if _evaluable(item))
        generated_n = sum(1 for item in bucket if _status(item) == "generated")
        by_day.append(
            {
                "as_of": day,
                "runs": runs_per_day.get(day, 0),
                "samples": len(bucket),
                "no_history": len(bucket) - evaluable_n,
                "generated": generated_n,
                "evaluable_rate": (
                    None if evaluable_n == 0 else round(generated_n / evaluable_n, 4)
                ),
                "chip_missing": sum(1 for item in bucket if _has_chip_missing(item)),
                "status": dict(Counter(_status(item) for item in bucket)),
            }
        )

    no_plan = [item for item in items if _status(item) == "no_plan"]
    no_plan_reason_counts = Counter(reason for item in no_plan for reason in _reasons(item))
    no_plan_examples: dict[str, list[str]] = defaultdict(list)
    for item in no_plan:
        for reason in _reasons(item):
            if len(no_plan_examples[reason]) < 3:
                no_plan_examples[reason].append(f"{item['as_of']}/{item['code']}")

    evaluable = [item for item in items if _evaluable(item)]
    chip_split = []
    for flag in (True, False):
        bucket = [item for item in items if _has_chip_missing(item) is flag]
        chip_split.append(
            {
                "chip_missing": flag,
                "samples": len(bucket),
                "by_status": dict(Counter(_status(item) for item in bucket)),
                "generated_rate": _ratio(
                    sum(1 for item in bucket if _status(item) == "generated"),
                    len(bucket),
                ),
            }
        )
    no_history = [item for item in items if not _evaluable(item)]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": {
            **meta,
            "unique_pairs": len(items),
            "days": len(items_per_day),
            "dedupe_rule": "unique (as_of, 6-digit code); duplicate candidates across "
            "repeated runs of the same as_of reuse the first probe result",
        },
        "overall": {
            "samples": len(items),
            "by_status": dict(status_counts),
            "by_reason_code": dict(reason_counts.most_common()),
            "generated_rate": _ratio(generated, len(items)),
            "chip_missing": len(chip_items),
            "chip_missing_generated": sum(
                1 for item in chip_items if _status(item) == "generated"
            ),
            "generated_ex_chip": _ratio(
                sum(1 for item in clean_items if _status(item) == "generated"),
                len(clean_items),
            ),
            "evaluable": {
                "samples": len(evaluable),
                "by_status": dict(Counter(_status(item) for item in evaluable)),
                "generated_rate": _ratio(
                    sum(1 for item in evaluable if _status(item) == "generated"),
                    len(evaluable),
                ),
            },
            "chip_split": chip_split,
            "no_history": {
                "samples": len(no_history),
                "codes": sorted({item["code"] for item in no_history}),
            },
        },
        "by_day": by_day,
        "by_board": group(items, lambda item: board_of(item["code"])),
        "by_industry": group(
            items, lambda item: industries.get(_code6(item["code"]), "unknown")
        ),
        "by_market_cap": group(
            items,
            lambda item: mv_bucket(
                caps_by_day.get(item["as_of"], {}).get(_code6(item["code"]))
            ),
        ),
        "no_plan": {
            "samples": len(no_plan),
            "by_reason_code": dict(no_plan_reason_counts.most_common()),
            "examples": {key: value for key, value in no_plan_examples.items()},
            "chip_missing": sum(1 for item in no_plan if _has_chip_missing(item)),
        },
        "bar_counts": {
            "min": min((item.get("cash_bars") or 0) for item in items) if items else 0,
            "median": median([(item.get("cash_bars") or 0) for item in items]) if items else 0,
            "max": max((item.get("cash_bars") or 0) for item in items) if items else 0,
        },
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _table(rows: list[dict[str, Any]], headers: tuple[str, ...]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(key, "")) for key in headers) + " |")
    return lines


def render_report(summary: dict[str, Any], probability_notes: list[str]) -> str:
    overall = summary["overall"]
    evaluable = overall["evaluable"]
    chip_split = {row["chip_missing"]: row for row in overall["chip_split"]}
    with_chip = chip_split.get(True, {})
    without_chip = chip_split.get(False, {})
    lines = [
        "# 确定性点位引擎 · 历史影子回放报告",
        "",
        f"- 生成时间: {summary['generated_at']}",
        f"- 选股 run: {summary['meta']['runs']} 个"
        f"（其中产生新样本的 run {summary['meta']['runs_with_candidates']} 个）",
        f"- 去重后样本: {summary['meta']['unique_pairs']} 个 `(as_of, code)`，"
        f"覆盖 {summary['meta']['days']} 个交易日",
        f"- 实际探针调用: {summary['meta']['probe_calls']} 次"
        f"（同一天重复 run 的同码候选复用首次结果，跳过 "
        f"{summary['meta']['duplicate_candidates_skipped']} 个）",
        f"- 回放耗时: {summary['meta']['elapsed_seconds']} 秒"
        + ("（**因 20 分钟预算中断，结果为部分样本**）"
           if summary["meta"]["stopped_by_time_budget"] else ""),
        f"- 去重规则: {summary['meta']['dedupe_rule']}",
        "",
        "## 1. 整体分布",
        "",
        "| status | 数量 | 占比 |",
        "| --- | --- | --- |",
    ]
    for status, count in sorted(
        overall["by_status"].items(), key=lambda pair: -pair[1]
    ):
        lines.append(f"| {status} | {count} | {_pct(count / overall['samples'])} |")
    lines += [
        "",
        "### chip 缺失分层",
        "",
        "| 分层 | 样本 | generated | observation | no_plan | generated 率 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for flag, row in ((True, with_chip), (False, without_chip)):
        by_status = row.get("by_status") or {}
        rate = (row.get("generated_rate") or {}).get("rate")
        lines.append(
            f"| {'含 chip 缺失' if flag else '无 chip 缺失'} | {row.get('samples', 0)} | "
            f"{by_status.get('generated', 0)} | {by_status.get('observation', 0)} | "
            f"{by_status.get('no_plan', 0)} | {_pct(rate)} |"
        )
    lines += [
        "",
        "口径说明（重要）:",
        "",
        f"- 含 chip 缺失口径: `generated` "
        f"{_pct(overall['generated_rate']['rate'])} "
        f"({overall['generated_rate']['numerator']}/"
        f"{overall['generated_rate']['denominator']})。",
        f"- 字面「剔除 chip 缺失样本」: {_pct(overall['generated_ex_chip']['rate'])} "
        f"({overall['generated_ex_chip']['numerator']}/"
        f"{overall['generated_ex_chip']['denominator']})。**该口径不可用于判断严苛度**: "
        f"{overall['chip_missing']} 个含 chip 缺失样本里有 "
        f"{overall['chip_missing_generated']} 个是 `generated`，说明 chip 缺失并不阻断生成；"
        f"而剩下的 {overall['generated_ex_chip']['denominator']} 个无 chip 缺失样本"
        f"恰好是拿不到 720 根日线的空快照失败样本。",
        f"- 真正可评估口径（`cash_bars >= {REQUIRED_BARS}`，忽略 chip 标记）: `generated` "
        f"{_pct((evaluable['generated_rate'] or {}).get('rate'))} "
        f"({(evaluable['generated_rate'] or {}).get('numerator')}/"
        f"{evaluable['samples']})，observation "
        f"{(evaluable['by_status'] or {}).get('observation', 0)}，no_plan "
        f"{(evaluable['by_status'] or {}).get('no_plan', 0)}。",
        f"- 无历史行情样本: {overall['no_history']['samples']} 个，"
        f"涉及代码 {', '.join(overall['no_history']['codes'])}。",
        "",
        "### reason_code 分布（Top 20）",
        "",
        "| reason_code | 次数 |",
        "| --- | --- |",
    ]
    for reason, count in list(overall["by_reason_code"].items())[:20]:
        lines.append(f"| {reason} | {count} |")

    lines += ["", "## 2. 按交易日", ""]
    day_rows = []
    for row in summary["by_day"]:
        day_rows.append(
            {
                "as_of": row["as_of"],
                "runs": row["runs"],
                "samples": row["samples"],
                "no_history": row["no_history"],
                "generated": row["generated"],
                "chip_missing": row["chip_missing"],
                "generated_rate": _pct(row["generated"] / row["samples"]),
                "evaluable_rate": _pct(row.get("evaluable_rate")),
            }
        )
    lines += _table(
        day_rows,
        (
            "as_of",
            "runs",
            "samples",
            "no_history",
            "generated",
            "chip_missing",
            "generated_rate",
            "evaluable_rate",
        ),
    )

    for index, (title, key) in enumerate(
        (
            ("板块", "by_board"),
            ("行业（当前 stock_basic 口径）", "by_industry"),
            ("市值分档（当日 daily_basic total_mv）", "by_market_cap"),
        ),
        start=1,
    ):
        lines += ["", f"## 3.{index} 按{title}", ""]
        rows = []
        for row in summary[key][:15]:
            rows.append(
                {
                    **row,
                    "generated_rate": _pct(row.get("generated_rate")),
                    "evaluable_rate": _pct(row.get("evaluable_rate")),
                }
            )
        lines += _table(
            rows,
            (
                "group",
                "samples",
                "no_history",
                "generated",
                "generated_rate",
                "evaluable_rate",
                "chip_missing",
            ),
        )

    lines += ["", "## 4. no_plan 原因排行", ""]
    no_plan = summary["no_plan"]
    rows = []
    for reason, count in list(no_plan["by_reason_code"].items())[:15]:
        share = None if no_plan["samples"] == 0 else count / no_plan["samples"]
        rows.append(
            {
                "reason_code": reason,
                "count": count,
                "share_of_no_plan": _pct(share),
                "examples": ", ".join(no_plan["examples"].get(reason, [])),
            }
        )
    lines += _table(rows, ("reason_code", "count", "share_of_no_plan", "examples"))
    lines += [
        "",
        f"no_plan 样本 {no_plan['samples']} 个，其中 {no_plan['chip_missing']} 个同时含 chip 缺失。",
        "结构性缺失（无 720 根日线）会一次性带出 INSUFFICIENT_DAILY_HISTORY / "
        "INSUFFICIENT_WEEKLY_HISTORY / MARKET_RULES_UNAVAILABLE / MISSING_CASH_PRICE / "
        "PRICE_SCALE_UNVERIFIED 一组 reason，统计时按同一样本只算一次。",
        "",
        "## 5. 已知偏差",
        "",
        "- **chip 缺失**: 影子链路固定 `chip_loader=None`，历史筹码快照本身没有历史数据，"
        f"`{CHIP_MISSING}` 属数据缺失。实测它只是附加 reason，不阻断 `generated`"
        "（chip 缺失样本中仍有大量 generated），因此不能把「剔除 chip 缺失」当成严苛度指标。",
        "- **候选来源是旧 prompt 的选股结果**: 候选来自历史 `candidate_discovery.json`，"
        "不是用当前 prompt 重跑，分布代表的是当时候选池，而非当前策略的候选池。",
        "- **历史 ST 状态存在推导误差**: 历史元数据由 `stock_basic` + `namechange` 快照反推，"
        "`namechange` 被上游单次返回上限截断在 2006-10-09 附近；有覆盖记录才 verified，"
        "无覆盖时降级不置 verified（readiness 会保持未就绪），不做精确性承诺。",
        "- **行业 / 市值是快照口径**: 行业取 `stock_basic_LD.csv` 当前值；市值取 `as_of` 当日"
        "或之前最近一份 `basic_YYYYMMDD.csv` 的 `total_mv`，不是严格复权市值。",
        "- **bar 数量**: "
        f"min={summary['bar_counts']['min']} / median={summary['bar_counts']['median']} / "
        f"max={summary['bar_counts']['max']}，不足 {REQUIRED_BARS} 根会直接落结构性 reason code，"
        "本次未继续扩窗，按现状如实计入。",
        "",
        "## 6. 判定提示",
        "",
    ]
    lines += [f"- {note}" for note in probability_notes]
    lines.append("")
    return "\n".join(lines)


def judgment_notes(summary: dict[str, Any]) -> list[str]:
    overall = summary["overall"]
    evaluable = overall["evaluable"]
    status = evaluable["by_status"]
    no_plan = summary["no_plan"]["by_reason_code"]
    notes: list[str] = []
    generated_rate = (evaluable["generated_rate"] or {}).get("rate")
    if generated_rate is not None:
        notes.append(
            f"可评估样本（{evaluable['samples']} 个有 {REQUIRED_BARS} 根日线）中 "
            f"`generated` 比例 {_pct(generated_rate)}，这是判断引擎是否过严的主口径。"
        )
    if evaluable["samples"]:
        notes.append(
            f"可评估样本里 observation {status.get('observation', 0)} 个"
            f"（{_pct(status.get('observation', 0) / evaluable['samples'])}）、"
            f"no_plan {status.get('no_plan', 0)} 个"
            f"（{_pct(status.get('no_plan', 0) / evaluable['samples'])}）。"
        )
    structural = {
        CHIP_MISSING,
        "INSUFFICIENT_DAILY_HISTORY",
        "INSUFFICIENT_WEEKLY_HISTORY",
        "UNSUPPORTED_INSTRUMENT",
        "SYSTEM_NOT_READY",
        "SHADOW_PROBE_ERROR",
        "MARKET_RULES_UNAVAILABLE",
        "MISSING_CASH_PRICE",
        "PRICE_SCALE_UNVERIFIED",
    }
    engine_reasons = [
        (reason, count) for reason, count in no_plan.items() if reason not in structural
    ]
    for reason, count in engine_reasons[:8]:
        notes.append(f"no_plan 中 `{reason}` 出现 {count} 次，属引擎判断结果，需结合样例复核。")
    if engine_reasons:
        notes.append(
            "no_plan 的结构性缺口（无 720 根日线）与引擎判断（NO_ELIGIBLE_ANCHORS）已分离，"
            "前者是数据问题，后者才反映引擎逻辑。"
        )
    else:
        notes.append("no_plan 全部落在结构性缺失上，暂未看到引擎逻辑异常信号。")
    return notes


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--traces-dir",
        default=str(ROOT / "data" / "agent_traces"),
        help="选股 run 目录，默认 data/agent_traces",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "2026-11-20" / "pricing-replay"),
        help="产物根目录，默认 2026-11-20/pricing-replay",
    )
    parser.add_argument(
        "--time-budget-minutes",
        type=float,
        default=20.0,
        help="单轮时间预算，超时后中断并输出部分结果",
    )
    parser.add_argument(
        "--db",
        default="",
        help="DatabaseManager 连接串，默认读取项目配置",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="复用 <out>/work/items.jsonl 重新聚合与出报告，不再次调用探针",
    )
    args = parser.parse_args()

    if not pricing_shadow_enabled():
        print("shadow probe is disabled; refusing to run", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    work_dir = out_root / "work"
    outputs_dir = out_root / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    runs = collect_runs(Path(args.traces_dir))
    print(f"runs={len(runs)}")
    if not runs:
        print("no runs with valid as_of", file=sys.stderr)
        return 1

    if args.reuse:
        probed, meta = load_work_items(work_dir)
        print(f"reused items={len(probed)}")
    else:
        db = DatabaseManager(args.db) if args.db else DatabaseManager.get_instance()
        probed, meta = replay(
            runs,
            work_dir=work_dir,
            db=db,
            time_budget_seconds=args.time_budget_minutes * 60,
        )

    summary = summarize(probed, runs, meta, cache_dir=ROOT / "data" / "factor_eval_cache")
    (work_dir / "items.jsonl").write_text(
        "\n".join(
            json.dumps({"as_of": pair[0], "code": pair[1], **item}, ensure_ascii=False)
            for pair, item in sorted(probed.items())
        )
        + "\n",
        encoding="utf-8",
    )
    (work_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = render_report(summary, judgment_notes(summary))
    (outputs_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"wrote {work_dir / 'summary.json'}")
    print(f"wrote {outputs_dir / 'report.md'}")
    print(
        f"generated_ex_chip="
        f"{summary['overall']['generated_ex_chip']['numerator']}/"
        f"{summary['overall']['generated_ex_chip']['denominator']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
