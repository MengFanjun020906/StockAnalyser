"""FAC-03 估值分位观察值。

只读本地 ``data/factor_eval_cache`` 的 ``basic_*.csv`` 与申万一级快照，
在候选确定后把 ``ValuationFact`` 挂到 ``FactSheet.valuation``。所有字段仅展示，
不进入 desk LLM 事实包、不参与排序/阈值/否决；缺数据一律写 ``None``，不联网补数。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schemas import ValuationFact

logger = logging.getLogger(__name__)

HISTORY_WINDOW_DAYS = 120
MIN_INDUSTRY_PEERS = 8
MIN_HISTORY_OBSERVATIONS = 20
PE_CHANGE_LOOKBACK_DAYS = 20
INDUSTRY_SOURCE = "sw2021_l1"
_DEFAULT_CACHE_DIR = os.path.join("data", "factor_eval_cache")
_INDUSTRY_SNAPSHOT = "industry_snapshot_sw2021.csv"


def attach_valuation_facts(
    targets: Sequence[Any],
    *,
    as_of_date: Optional[str] = None,
    enabled: bool = False,
    cache_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """候选确定后统一挂载估值分位观察值；关闭时立即返回。

    ``targets`` 只需有 ``.code`` 与 ``.fact_sheet`` 属性（FeatureRow /
    AggregatedCandidate 均满足）。任何异常只记日志，不抛到主流程。
    """
    if not enabled:
        return {"enabled": False}

    rows = [
        item
        for item in targets
        if getattr(item, "code", "") and getattr(item, "fact_sheet", None) is not None
    ]
    if not rows:
        return {
            "enabled": True,
            "as_of_date": _normalize_date(as_of_date),
            "data_as_of_date": None,
            "coverage": 0.0,
            "status_counts": {},
            "missing_reason_top3": [],
            "total": 0,
        }

    try:
        cache = _resolve_cache_dir(cache_dir)
        dates = _available_basic_dates(cache, as_of_date)
        industry_map = _load_industry_map(cache)
        targets_by_code = {_code6(getattr(item, "code")): item for item in rows}
        panel, latest_pe = _load_basic_panel(cache, dates, set(targets_by_code))
        facts: List[ValuationFact] = []
        for code6, item in targets_by_code.items():
            fact = _build_fact(
                code6,
                panel.get(code6) or [],
                latest_pe,
                industry_map,
                dates[-1] if dates else None,
                as_of_date,
            )
            item.fact_sheet.valuation = fact
            facts.append(fact)
        return _summarize(facts, as_of_date, dates[-1] if dates else None)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("valuation_fact: attach failed: %s", exc, exc_info=True)
        return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}


def serialize_for_display(fact: Any) -> Optional[Dict[str, Any]]:
    if fact is None:
        return None
    fields = (
        "industry_source",
        "industry_code",
        "industry_name",
        "as_of_date",
        "data_as_of_date",
        "pe_ttm",
        "pe_negative",
        "industry_pe_median",
        "industry_peer_count",
        "industry_pe_percentile",
        "history_pe_percentile",
        "history_observations",
        "pe_change_20d",
        "status",
        "missing_reason",
        "quality_notes",
        "usage",
        "display_only",
    )
    return {key: getattr(fact, key, None) for key in fields}


def _build_fact(
    code6: str,
    series: Sequence[Tuple[str, Optional[float]]],
    latest_pe: Dict[str, float],
    industry_map: Dict[str, Tuple[str, str]],
    data_date: Optional[str],
    as_of_date: Optional[str],
) -> ValuationFact:
    fact = ValuationFact(
        as_of_date=_normalize_date_iso(as_of_date),
        data_as_of_date=_normalize_date_iso(data_date),
    )
    industry = industry_map.get(code6)
    if industry:
        fact.industry_code, fact.industry_name = industry
    else:
        fact.missing_reason = "industry_unavailable"

    current = latest_pe.get(code6)
    if current is None:
        current = next((pe for _, pe in reversed(series) if pe is not None), None)
        if current is not None:
            fact.quality_notes.append("pe_ttm_from_last_observation")

    if current is None:
        if industry is None:
            fact.quality_notes.append("industry_unavailable")
        fact.missing_reason = "pe_ttm_missing"
        fact.status = "missing"
        return fact

    fact.pe_ttm = round(float(current), 4)
    fact.pe_negative = bool(current <= 0)

    if current > 0 and industry and data_date:
        l1_code = industry[0]
        peers = [
            pe
            for other, pe in latest_pe.items()
            if pe > 0 and (industry_map.get(other) or ("", ""))[0] == l1_code
        ]
        fact.industry_peer_count = len(peers)
        if len(peers) >= MIN_INDUSTRY_PEERS:
            fact.industry_pe_median = round(float(median(peers)), 4)
            fact.industry_pe_percentile = _percentile_rank(current, peers)
        elif fact.missing_reason is None:
            fact.missing_reason = "industry_peers_below_minimum"

    positives = [pe for _, pe in series if pe is not None and pe > 0]
    fact.history_observations = len(positives)
    if current > 0 and len(positives) >= MIN_HISTORY_OBSERVATIONS:
        fact.history_pe_percentile = _percentile_rank(current, positives)
    elif fact.missing_reason is None:
        fact.missing_reason = "history_observations_below_minimum"

    values = [pe for _, pe in series]
    if len(values) > PE_CHANGE_LOOKBACK_DAYS:
        base = values[-1 - PE_CHANGE_LOOKBACK_DAYS]
        if base is not None and base > 0 and values[-1] is not None and values[-1] > 0:
            fact.pe_change_20d = round((values[-1] / base - 1.0) * 100.0, 4)
    if fact.pe_negative:
        fact.quality_notes.append("pe_negative_excluded_from_percentiles")

    if (
        fact.industry_pe_percentile is not None
        and fact.history_pe_percentile is not None
    ):
        fact.status = "ok"
    else:
        fact.status = "partial"
    return fact


def _percentile_rank(value: float, values: Sequence[float]) -> float:
    """0-1 分位：样本中 <= value 的占比。"""
    if not values:
        return 0.0
    return round(sum(1 for item in values if item <= value) / len(values), 4)


def _load_basic_panel(
    cache: Path,
    dates: Sequence[str],
    target_codes: Iterable[str],
) -> Tuple[Dict[str, List[Tuple[str, Optional[float]]]], Dict[str, float]]:
    """读取窗口内 basic CSV；仅保留目标个股序列与最后一个交易日的全市场 PE。"""
    wanted = set(target_codes)
    panel: Dict[str, List[Tuple[str, Optional[float]]]] = {code: [] for code in wanted}
    latest_pe: Dict[str, float] = {}
    for index, day in enumerate(dates):
        path = cache / f"basic_{day}.csv"
        is_latest = index == len(dates) - 1
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                code6 = _code6(row.get("ts_code") or "")
                if not code6:
                    continue
                pe = _to_float(row.get("pe_ttm"))
                if is_latest and pe is not None:
                    latest_pe[code6] = pe
                if code6 in panel:
                    panel[code6].append((day, pe))
    return panel, latest_pe


def _load_industry_map(cache: Path) -> Dict[str, Tuple[str, str]]:
    """申万一级当前归属；以 out_date 为空且 in_date 最新的记录为准。"""
    path = cache / _INDUSTRY_SNAPSHOT
    if not path.exists():
        return {}
    output: Dict[str, Tuple[str, str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("out_date") or "").strip():
                continue
            code6 = _code6(row.get("ts_code") or "")
            l1_code = str(row.get("l1_code") or "").strip()
            if not code6 or not l1_code:
                continue
            in_date = str(row.get("in_date") or "").strip()
            previous = output.get(code6)
            if previous is None or in_date >= previous[2]:
                output[code6] = (l1_code, str(row.get("l1_name") or "").strip(), in_date)
    return {code: (value[0], value[1]) for code, value in output.items()}


def _available_basic_dates(cache: Path, as_of_date: Optional[str]) -> List[str]:
    cutoff = _normalize_date(as_of_date)
    days = []
    for path in cache.glob("basic_*.csv"):
        digits = path.stem.split("_")[-1]
        if len(digits) != 8 or not digits.isdigit():
            continue
        if cutoff and digits > cutoff:
            continue
        days.append(digits)
    days.sort()
    return days[-HISTORY_WINDOW_DAYS:]


def _resolve_cache_dir(cache_dir: Optional[str]) -> Path:
    raw = str(cache_dir or "").strip() or _DEFAULT_CACHE_DIR
    return Path(raw)


def _code6(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split(".")[0]
    return head.zfill(6) if head.isdigit() else ""


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _normalize_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y%m%d")
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def _normalize_date_iso(value: Any) -> Optional[str]:
    digits = _normalize_date(value)
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if digits else None


def _summarize(
    facts: Sequence[ValuationFact], as_of_date: Optional[str], data_date: Optional[str]
) -> Dict[str, Any]:
    status_counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    for fact in facts:
        status_counts[fact.status] = status_counts.get(fact.status, 0) + 1
        if fact.missing_reason:
            reason_counts[fact.missing_reason] = reason_counts.get(fact.missing_reason, 0) + 1
    total = len(facts)
    ok = status_counts.get("ok", 0)
    return {
        "enabled": True,
        "as_of_date": _normalize_date_iso(as_of_date),
        "data_as_of_date": _normalize_date_iso(data_date),
        "industry_source": INDUSTRY_SOURCE,
        "coverage": round(ok / total, 4) if total else 0.0,
        "status_counts": status_counts,
        "missing_reason_top3": sorted(
            reason_counts.items(), key=lambda item: (-item[1], item[0])
        )[:3],
        "total": total,
    }


__all__ = [
    "attach_valuation_facts",
    "serialize_for_display",
    "HISTORY_WINDOW_DAYS",
    "MIN_INDUSTRY_PEERS",
    "MIN_HISTORY_OBSERVATIONS",
    "INDUSTRY_SOURCE",
]
