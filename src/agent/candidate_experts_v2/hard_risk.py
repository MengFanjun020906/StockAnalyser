# -*- coding: utf-8 -*-
"""硬风险识别 — ST / 退市 / 停牌 (选股链路 P2 上游).

为 :func:`build_fact_sheet` 提供 ``hard_risk_flags`` 的数据来源, 让
``veto_gate`` 的硬红线不是"永远为空"。判定只陈述事实, 不打分/不排序:

- **ST**: ``namechange`` 中 ``end_date`` 为空的最新在册记录优先; 在册名称非 ST
  时即使 ``stock_basic.name`` 仍带 ST 也判 ``clear`` (名称可能滞后), 历史 ST
  行 (``end_date`` 非空) 永不 ``hit``; 无在册记录时回退 ``stock_basic.name``。
- **退市**: ``stock_basic.list_status == "D"`` 且 ``delist_date`` 非空, 或名称
  处于"退市"整理期。
- **停牌**: ``suspend_d`` 在参考交易日是否出现该代码; 另用本地日线是否落后
  参考交易日作为辅助证据。
- **非 A 股标的**: 可转债 / ETF / 基金 / 债券 / 回购 / B 股按代码段识别后标记
  ``not_applicable``, 既不 veto 也不计入 ``unknown``, 由 seed pool 层前置过滤。

数据来源与缓存全部复用现有实现:

- ``stock_basic`` → ``data/factor_eval_cache/stock_basic_LD.csv`` (与
  ``src/backtest/factor_eval/data.py`` 同一个缓存文件)。
- ``namechange`` → ``data/factor_eval_cache/namechange_snapshot.csv``。
- ``suspend_d`` → ``data/factor_eval_cache/suspend_d_YYYYMMDD.csv``。
- 在线补数统一走 ``data_provider.tushare_client.query_tushare_api``, 不新增
  第二套 HTTP 客户端。

**未知即未知**: 任一来源缺失/失败时, 对应 flag 记为 ``unknown`` 并写明原因,
绝不默认当成"安全"; 只有 ``status == "hit"`` 才进入 ``hard_risk_flags``。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from src.agent.candidate_experts_v2.instruments import (
    A_SHARE,
    UNKNOWN as UNKNOWN_INSTRUMENT,
    classify_instrument,
)
from src.agent.candidate_experts.filters import _is_st_name

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/factor_eval_cache")
STOCK_BASIC_CACHE = "stock_basic_LD.csv"
NAMECHANGE_CACHE = "namechange_snapshot.csv"
SUSPEND_CACHE_TEMPLATE = "suspend_d_{date}.csv"

NAMECHANGE_TTL_DAYS = 7
NAMECHANGE_LOOKBACK_YEARS = 3

FLAG_ST = "st"
FLAG_DELIST = "delist"
FLAG_SUSPENDED = "suspended"
ALL_FLAGS: Tuple[str, ...] = (FLAG_ST, FLAG_DELIST, FLAG_SUSPENDED)

_SIX_DIGIT = re.compile(r"^\d{6}$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cache_dir(cache_dir: Optional[Path]) -> Path:
    return Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR


def _code6(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split(".")[0].strip()


def _normalize_date(value: Any) -> str:
    """Normalize YYYYMMDD / YYYY-MM-DD / date / NaT into ``YYYY-MM-DD``."""
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        return "" if pd.isna(value) else value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        value = int(value)
    text = str(value).strip()
    if text in {"", "nan", "None", "NaT", "<NA>"}:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return text[:10]


def _compact_date(value: Any) -> str:
    return _normalize_date(value).replace("-", "")


def _file_date(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _read_csv(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        return pd.read_csv(path, dtype=str).fillna("")
    except Exception as exc:  # pragma: no cover - corrupt cache is non-fatal
        logger.warning("hard_risk: unreadable cache %s: %s", path, exc)
        return None


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
    except Exception as exc:  # pragma: no cover - cache write is best-effort
        logger.warning("hard_risk: failed to write cache %s: %s", path, exc)


def _age_days(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 86400.0
    except Exception:
        return None


def _is_delist_name(name: str) -> bool:
    text = str(name or "").strip()
    return text.startswith("退") or "退市" in text


def _tushare_query(api_name: str, params: Dict[str, Any], fields: str, timeout: int = 25) -> pd.DataFrame:
    """Thin wrapper so tests can patch one import point."""
    from data_provider.tushare_client import query_tushare_api

    return query_tushare_api(api_name, params, fields=fields, timeout=timeout)


# ---------------------------------------------------------------------------
# cache loaders
# ---------------------------------------------------------------------------

def load_stock_basic_map(
    *,
    cache_dir: Optional[Path] = None,
    allow_online: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Any]]:
    """Return ``{code6: row}`` plus metadata (source / as_of / error)."""
    path = _cache_dir(cache_dir) / STOCK_BASIC_CACHE
    df = _read_csv(path)
    error = ""
    if df is None and allow_online:
        frames: List[pd.DataFrame] = []
        try:
            for status in ("L", "D"):
                part = _tushare_query(
                    "stock_basic",
                    {"exchange": "", "list_status": status},
                    "ts_code,name,industry,market,list_date,delist_date,list_status",
                )
                if part is not None and not part.empty:
                    frames.append(part)
            if frames:
                df = pd.concat(frames, ignore_index=True, sort=False).fillna("")
                df = df.drop_duplicates("ts_code", keep="first")
                _write_csv(path, df)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("hard_risk: stock_basic online refresh failed: %s", error)
            df = None
    if df is None:
        return {}, {
            "source": "stock_basic",
            "as_of": "",
            "error": error or f"未找到 {path}，且未开启在线补数",
            "path": str(path),
        }
    mapping: Dict[str, Dict[str, str]] = {}
    for row in df.to_dict("records"):
        code = _code6(row.get("ts_code") or row.get("symbol") or row.get("code"))
        if code:
            mapping[code] = {str(k): str(v) for k, v in row.items()}
    return mapping, {
        "source": "stock_basic",
        "as_of": _file_date(path),
        "error": error,
        "path": str(path),
    }


def _namechange_frame(allow_online: bool, cache_dir: Optional[Path]) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    path = _cache_dir(cache_dir) / NAMECHANGE_CACHE
    age = _age_days(path)
    meta: Dict[str, Any] = {"source": "namechange", "path": str(path), "error": ""}
    if age is not None and age <= NAMECHANGE_TTL_DAYS:
        df = _read_csv(path)
        if df is not None:
            meta["as_of"] = _file_date(path)
            return df, meta
    df = _read_csv(path) if age is not None else None
    if allow_online:
        frames: List[pd.DataFrame] = []
        end = date.today()
        try:
            for offset in range(NAMECHANGE_LOOKBACK_YEARS + 1):
                start = date(end.year - offset, 1, 1)
                stop = min(date(end.year - offset, 12, 31), end)
                if start > stop:
                    continue
                part = _tushare_query(
                    "namechange",
                    {"start_date": _compact_date(start), "end_date": _compact_date(stop)},
                    "ts_code,name,start_date,end_date,ann_date,change_reason",
                )
                if part is not None and not part.empty:
                    frames.append(part)
            if frames:
                fresh = pd.concat(frames, ignore_index=True, sort=False).fillna("")
                fresh = fresh.drop_duplicates(["ts_code", "name", "start_date", "end_date"])
                _write_csv(path, fresh)
                meta["as_of"] = _file_date(path)
                return fresh, meta
            meta["error"] = "namechange 返回空"
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("hard_risk: namechange online refresh failed: %s", meta["error"])
    if df is not None:
        meta["as_of"] = _file_date(path)
        meta.setdefault("stale", True)
        return df, meta
    meta["as_of"] = ""
    meta["error"] = meta["error"] or f"未找到 {path}，且未开启在线补数"
    return None, meta


def load_namechange_map(
    *,
    cache_dir: Optional[Path] = None,
    allow_online: bool = False,
) -> Tuple[Optional[Dict[str, List[Dict[str, str]]]], Dict[str, Any]]:
    """Return ``{code6: [namechange rows]}``; ``None`` means source unavailable."""
    df, meta = _namechange_frame(allow_online, cache_dir)
    if df is None:
        return None, meta
    mapping: Dict[str, List[Dict[str, str]]] = {}
    for row in df.to_dict("records"):
        code = _code6(row.get("ts_code"))
        if not code:
            continue
        mapping.setdefault(code, []).append({str(k): str(v) for k, v in row.items()})
    return mapping, meta


def load_suspend_map(
    reference_date: str,
    *,
    cache_dir: Optional[Path] = None,
    allow_online: bool = False,
) -> Tuple[Optional[Dict[str, Dict[str, str]]], Dict[str, Any]]:
    """Return ``{code6: suspend row}`` for *reference_date*.

    ``None`` means the source is unavailable (do not treat as "not suspended").
    """
    compact = _compact_date(reference_date)
    meta: Dict[str, Any] = {"source": "suspend_d", "as_of": _normalize_date(reference_date)}
    if not compact:
        meta["error"] = "缺少参考交易日，无法查询 suspend_d"
        return None, meta
    path = _cache_dir(cache_dir) / SUSPEND_CACHE_TEMPLATE.format(date=compact)
    df = _read_csv(path)
    if df is None and allow_online:
        try:
            df = _tushare_query(
                "suspend_d",
                {"trade_date": compact},
                "ts_code,trade_date,suspend_timing,suspend_type",
            )
            if df is None:
                df = pd.DataFrame(columns=["ts_code", "trade_date", "suspend_timing", "suspend_type"])
            df = df.fillna("")
            _write_csv(path, df)
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("hard_risk: suspend_d online refresh failed: %s", meta["error"])
            df = None
    if df is None:
        meta["error"] = meta.get("error") or f"未找到 {path}，且未开启在线补数"
        return None, meta
    mapping: Dict[str, Dict[str, str]] = {}
    for row in df.to_dict("records"):
        code = _code6(row.get("ts_code"))
        if code:
            mapping[code] = {str(k): str(v) for k, v in row.items()}
    meta["path"] = str(path)
    return mapping, meta


# ---------------------------------------------------------------------------
# per-stock verdict
# ---------------------------------------------------------------------------

def _current_namechange(records: Sequence[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Latest record whose ``end_date`` is empty (= still in effect)."""
    active = [r for r in records if not _normalize_date(r.get("end_date"))]
    if not active:
        return None
    return max(active, key=lambda r: _normalize_date(r.get("start_date")))


def _history_text(records: Sequence[Dict[str, str]], *, limit: int = 3) -> str:
    st_records = [r for r in records if _is_st_name(r.get("name") or "")]
    if not st_records:
        return ""
    st_records = sorted(st_records, key=lambda r: _normalize_date(r.get("start_date")), reverse=True)[:limit]
    parts = []
    for r in st_records:
        start = _normalize_date(r.get("start_date"))
        end = _normalize_date(r.get("end_date")) or "至今"
        parts.append(f"{r.get('name')}({start}~{end})")
    return "; ".join(parts)


def assess_one(
    code: str,
    *,
    stock_basic_map: Optional[Dict[str, Dict[str, str]]],
    stock_basic_meta: Dict[str, Any],
    namechange_map: Optional[Dict[str, List[Dict[str, str]]]],
    namechange_meta: Dict[str, Any],
    suspend_map: Optional[Dict[str, Dict[str, str]]],
    suspend_meta: Dict[str, Any],
    bars_last_date: str = "",
    local_bar_date: str = "",
) -> Dict[str, Any]:
    """Evaluate ST / delist / suspended for one code.

    Returns ``{"hard_risk_flags", "evidence", "unknown", "instrument_type",
    "not_applicable"}``.  Non-A-share instruments get ``not_applicable=True``
    with empty ``hard_risk_flags`` / ``unknown`` so they are never confused
    with "data missing".
    """
    evidence: List[Dict[str, Any]] = []
    hits: List[str] = []
    unknown: List[str] = []

    def add(flag: str, status: str, source: str, as_of: str, detail: str) -> None:
        evidence.append(
            {
                "flag": flag,
                "status": status,
                "source": source,
                "as_of": as_of,
                "detail": detail,
            }
        )

    normalized = _code6(code)
    if not _SIX_DIGIT.match(normalized):
        reason = f"非 A 股 6 位代码({code})，未做 ST/退市/停牌判定"
        for flag in ALL_FLAGS:
            add(flag, "unknown", "unsupported_market", "", reason)
            unknown.append(flag)
        return {
            "hard_risk_flags": [],
            "evidence": evidence,
            "unknown": sorted(set(unknown)),
            "instrument_type": UNKNOWN_INSTRUMENT,
            "not_applicable": False,
        }

    instrument_type, instrument_label = classify_instrument(normalized)
    if instrument_type not in (A_SHARE, UNKNOWN_INSTRUMENT):
        # 可转债 / ETF / 基金 / 债券 / 回购 / B 股: 不属于 A 股股票, 硬风险不适用。
        # 这不是"未知"——代码段已能确定标的类型, 不能静默当成风险股票放行或拦截。
        reason = f"{instrument_label}({normalized}) 非 A 股股票，ST/退市/停牌硬风险不适用"
        for flag in ALL_FLAGS:
            add(flag, "not_applicable", "instrument_type", "", reason)
        return {
            "hard_risk_flags": [],
            "evidence": evidence,
            "unknown": [],
            "instrument_type": instrument_type,
            "not_applicable": True,
        }

    # --- delist via stock_basic; ST 延迟到 namechange 之后判定 --------------
    basic_as_of = str(stock_basic_meta.get("as_of") or "")
    name = ""
    basic_available = False
    if stock_basic_map is None:
        reason = f"stock_basic 不可用: {stock_basic_meta.get('error') or 'unknown'}"
        add(FLAG_DELIST, "unknown", "stock_basic", basic_as_of, reason)
        unknown.append(FLAG_DELIST)
    else:
        basic = stock_basic_map.get(normalized)
        if basic is None:
            reason = f"stock_basic 无 {normalized} 记录: {stock_basic_meta.get('error') or '代码未收录'}"
            add(FLAG_DELIST, "unknown", "stock_basic", basic_as_of, reason)
            unknown.append(FLAG_DELIST)
        else:
            basic_available = True
            name = str(basic.get("name") or "").strip()
            list_status = str(basic.get("list_status") or "").strip().upper()
            delist_date = _normalize_date(basic.get("delist_date"))

            if list_status == "D":
                hits.append(FLAG_DELIST)
                add(
                    FLAG_DELIST,
                    "hit",
                    "stock_basic",
                    delist_date or basic_as_of,
                    f"stock_basic.list_status=D, delist_date={delist_date or '未提供'}",
                )
            elif _is_delist_name(name):
                hits.append(FLAG_DELIST)
                add(FLAG_DELIST, "hit", "stock_basic", basic_as_of, f"stock_basic.name={name} 处于退市整理期")
            else:
                add(
                    FLAG_DELIST,
                    "clear",
                    "stock_basic",
                    basic_as_of,
                    f"list_status={list_status or 'unknown'}, delist_date={delist_date or '空'}",
                )

    # --- namechange: 当前在册名称 (end_date 为空) 优先决定 ST ---------------
    nc_as_of = str(namechange_meta.get("as_of") or "")
    if namechange_map is None:
        if basic_available:
            if _is_st_name(name):
                hits.append(FLAG_ST)
                add(
                    FLAG_ST,
                    "hit",
                    "stock_basic",
                    basic_as_of,
                    f"stock_basic.name={name} 含 ST 标记（namechange 不可用，无法交叉确认）",
                )
            else:
                add(
                    FLAG_ST,
                    "clear",
                    "stock_basic",
                    basic_as_of,
                    f"stock_basic.name={name} 无 ST 标记（namechange 不可用）",
                )
        else:
            add(
                FLAG_ST,
                "unknown",
                "namechange",
                nc_as_of,
                f"namechange 不可用，且 stock_basic 无可用名称: {namechange_meta.get('error') or 'unknown'}",
            )
            if FLAG_ST not in unknown:
                unknown.append(FLAG_ST)
    else:
        records = namechange_map.get(normalized) or []
        current = _current_namechange(records)
        history = _history_text(records)
        if current is not None:
            current_name = str(current.get("name") or "").strip()
            current_as_of = _normalize_date(current.get("start_date")) or nc_as_of
            if _is_st_name(current_name):
                hits.append(FLAG_ST)
                add(
                    FLAG_ST,
                    "hit",
                    "namechange",
                    current_as_of,
                    f"namechange 当前在册名称={current_name} 含 ST 标记, "
                    f"change_reason={current.get('change_reason') or '未提供'}",
                )
            else:
                # 摘帽: 当前在册名称已无 ST, 即使 stock_basic.name 滞后也不能否决。
                detail = f"namechange 当前在册名称={current_name} 无 ST 标记"
                if _is_st_name(name):
                    detail += f"; stock_basic.name={name} 可能滞后，以 namechange 在册记录为准"
                if history:
                    detail += f"; 历史 ST(已摘帽): {history}"
                add(FLAG_ST, "clear", "namechange", current_as_of, detail)
        else:
            if records:
                latest = max(records, key=lambda r: _normalize_date(r.get("start_date")))
                detail = (
                    "namechange 无当前在册记录(end_date 均非空), "
                    f"最近更名={latest.get('name')} "
                    f"({_normalize_date(latest.get('start_date')) or '?'}~"
                    f"{_normalize_date(latest.get('end_date')) or '?'})"
                )
            else:
                detail = "namechange 快照未收录该代码（接口单次返回上限约束）"
            if basic_available:
                # 无在册记录时以 stock_basic 当前名称为准; 历史 ST 行 (end_date 非空) 永不 hit。
                if _is_st_name(name):
                    hits.append(FLAG_ST)
                    add(
                        FLAG_ST,
                        "hit",
                        "stock_basic",
                        basic_as_of,
                        f"stock_basic.name={name} 含 ST 标记; {detail}",
                    )
                else:
                    final_detail = f"stock_basic.name={name} 无 ST 标记; {detail}"
                    if history:
                        final_detail += f"; 历史 ST(已摘帽): {history}"
                    add(FLAG_ST, "clear", "stock_basic", basic_as_of, final_detail)
            else:
                add(
                    FLAG_ST,
                    "unknown",
                    "namechange",
                    nc_as_of,
                    f"{detail}; stock_basic 亦无记录，无法确认当前 ST",
                )
                if FLAG_ST not in unknown:
                    unknown.append(FLAG_ST)

    # --- suspended via suspend_d ------------------------------------------
    suspend_as_of = str(suspend_meta.get("as_of") or "")
    if suspend_map is None:
        add(
            FLAG_SUSPENDED,
            "unknown",
            "suspend_d",
            suspend_as_of,
            f"suspend_d 不可用，无法判定 {suspend_as_of or '参考日'} 是否停牌: "
            f"{suspend_meta.get('error') or 'unknown'}",
        )
        unknown.append(FLAG_SUSPENDED)
    elif normalized in suspend_map:
        row = suspend_map[normalized]
        hits.append(FLAG_SUSPENDED)
        add(
            FLAG_SUSPENDED,
            "hit",
            "suspend_d",
            suspend_as_of,
            f"suspend_d 命中 trade_date={row.get('trade_date') or suspend_as_of}, "
            f"type={row.get('suspend_type') or '未提供'}, reason={row.get('suspend_reason') or '未提供'}",
        )
    else:
        add(FLAG_SUSPENDED, "clear", "suspend_d", suspend_as_of, f"suspend_d 未列出该代码({suspend_as_of})")

    # --- local bars lag (auxiliary evidence, never a hit by itself) --------
    if bars_last_date and local_bar_date and bars_last_date < local_bar_date:
        add(
            FLAG_SUSPENDED,
            "unknown",
            "local_daily",
            bars_last_date,
            f"本地日线最新={bars_last_date} < 本地参考交易日={local_bar_date}，"
            "可能停牌或数据缺失，需 suspend_d 确认",
        )
        if FLAG_SUSPENDED not in hits and FLAG_SUSPENDED not in unknown:
            unknown.append(FLAG_SUSPENDED)

    return {
        "hard_risk_flags": sorted(set(hits)),
        "evidence": evidence,
        "unknown": sorted(set(unknown)),
        "instrument_type": instrument_type,
        "not_applicable": False,
    }


# ---------------------------------------------------------------------------
# snapshot builder
# ---------------------------------------------------------------------------

def _last_bar_date(bars: Sequence[Dict[str, Any]]) -> str:
    latest = ""
    for bar in bars or []:
        value = _normalize_date(bar.get("date") or bar.get("trade_date"))
        if value and value > latest:
            latest = value
    return latest


def build_hard_risk_snapshot(
    codes: Iterable[str],
    *,
    bars_by_code: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    reference_date: str = "",
    local_bar_date: str = "",
    cache_dir: Optional[Path] = None,
    allow_online: bool = False,
    stock_basic_map: Optional[Dict[str, Dict[str, str]]] = None,
    stock_basic_meta: Optional[Dict[str, Any]] = None,
    namechange_map: Optional[Dict[str, List[Dict[str, str]]]] = None,
    namechange_meta: Optional[Dict[str, Any]] = None,
    suspend_map: Optional[Dict[str, Dict[str, str]]] = None,
    suspend_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build ``{code: assess_one(...)}`` for all *codes*.

    Loaders are only invoked when the corresponding map is not injected, so
    tests can pass fixtures without touching disk or network.  ``allow_online``
    defaults to ``False`` to preserve recall Phase A's "local only" contract;
    the production pipeline opts in explicitly.
    """
    code_list = [str(c).strip() for c in codes if str(c or "").strip()]
    if stock_basic_map is None:
        stock_basic_map, resolved_meta = load_stock_basic_map(cache_dir=cache_dir, allow_online=allow_online)
        stock_basic_meta = resolved_meta if stock_basic_meta is None else stock_basic_meta
    if namechange_map is None:
        namechange_map, resolved_meta = load_namechange_map(cache_dir=cache_dir, allow_online=allow_online)
        namechange_meta = resolved_meta if namechange_meta is None else namechange_meta
    if suspend_map is None:
        suspend_map, resolved_meta = load_suspend_map(
            reference_date,
            cache_dir=cache_dir,
            allow_online=allow_online,
        )
        suspend_meta = resolved_meta if suspend_meta is None else suspend_meta

    stock_basic_meta = stock_basic_meta or {}
    namechange_meta = namechange_meta or {}
    suspend_meta = suspend_meta or {}
    bars_by_code = bars_by_code or {}

    result: Dict[str, Dict[str, Any]] = {}
    for code in code_list:
        result[code] = assess_one(
            code,
            stock_basic_map=stock_basic_map,
            stock_basic_meta=stock_basic_meta,
            namechange_map=namechange_map,
            namechange_meta=namechange_meta,
            suspend_map=suspend_map,
            suspend_meta=suspend_meta,
            bars_last_date=_last_bar_date(bars_by_code.get(code) or []),
            local_bar_date=local_bar_date,
        )
    return result


def summarize_hard_risk(fact_sheets: Sequence[Any]) -> Dict[str, Any]:
    """Aggregate per-flag hit / unknown / not_applicable counts for payload.

    ``unknowns`` and ``not_applicable_codes`` keep the per-code detail so a
    reader can distinguish "数据缺失但未否决" from "确认安全", and can audit
    which codes were excluded because they are not A-share stocks.
    """
    summary: Dict[str, Any] = {
        "evaluated": len(fact_sheets),
        "hit": {flag: 0 for flag in ALL_FLAGS},
        "unknown": {flag: 0 for flag in ALL_FLAGS},
        "not_applicable": {flag: 0 for flag in ALL_FLAGS},
        "flags": {flag: 0 for flag in ALL_FLAGS},
        "hits": [],
        "unknowns": [],
        "not_applicable_codes": [],
    }
    for sheet in fact_sheets:
        if sheet is None:
            continue
        evidence = list(getattr(sheet, "hard_risk_evidence", None) or [])
        seen_hit = set()
        seen_unknown = set()
        seen_na = set()
        for item in evidence:
            flag = str(item.get("flag") or "")
            if flag not in ALL_FLAGS:
                continue
            status = str(item.get("status") or "")
            if status == "hit":
                seen_hit.add(flag)
            elif status == "unknown":
                seen_unknown.add(flag)
            elif status == "not_applicable":
                seen_na.add(flag)
        code = str(getattr(sheet, "code", "") or "")
        for flag in seen_hit:
            summary["hit"][flag] += 1
            summary["flags"][flag] += 1
        for flag in seen_unknown - seen_hit - seen_na:
            summary["unknown"][flag] += 1
        for flag in seen_na - seen_hit - seen_unknown:
            summary["not_applicable"][flag] += 1
        if seen_hit:
            summary["hits"].append({"code": code, "flags": sorted(seen_hit)})
        unknown_flags = sorted(seen_unknown - seen_hit - seen_na)
        if unknown_flags:
            summary["unknowns"].append({"code": code, "flags": unknown_flags})
        if seen_na and not seen_hit and not seen_unknown:
            summary["not_applicable_codes"].append({"code": code, "flags": sorted(seen_na)})
    return summary
