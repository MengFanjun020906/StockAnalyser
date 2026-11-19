# -*- coding: utf-8 -*-
"""板块资金流观察值（FAC-01b / FAC-01c，仅展示，不参与排序/入选/desk 打分）。

设计文档：``2026-11-17/showcase-package-review/outputs/sector-flow-design.md``。

核心约束：
- **同一交易日只构建一次板块快照**，个股按 ``bk_code`` O(1) 查询；候选循环内
  绝不逐股调用板块资金流接口。
- 映射顺序：``bk_code`` 精确匹配 → 人工别名表精确等于 → 成分股反查；
  禁止任何子串/包含式匹配。
- 缺失分档：``<2`` 天 ``missing``；``2..window-1`` 天 ``partial`` 且权重重归一；
  ``0`` 是合法值，不是缺失。
- ``usage="observation"`` / ``display_only=True``；默认不进入 desk LLM 事实包。

本模块顶层不 import ``data_tools`` / ``committee`` / ``recall``，避免循环导入。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_RESOURCE_DIR = Path(__file__).resolve().parent / "resources"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SNAPSHOT_TMPL = "sector_flow_snapshot_{date}.json"
_CONSTITUENT_TMPL = "sector_flow_constituents_{date}.json"

_SOURCE_CATALOG = "akshare.stock_board_industry_name_em"
_SOURCE_RANK = "akshare.stock_sector_fund_flow_rank"
_SOURCE_HISTORY = "akshare.stock_sector_fund_flow_hist"
_SOURCE_CONSTITUENTS = "akshare.stock_board_industry_cons_em"

_BUILD_LOCK = threading.Lock()

DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 1,
    "window_days": 5,
    "universe_limit": 0,  # 0 = 全量行业板块，截面分位才可能为 complete
    "concurrency": 8,
    "retry": 1,
    "retry_backoff_s": 0.5,
    "request_timeout_s": 20,
    "constituent_fallback": False,
    "constituent_max_boards": 40,
    "weights": {
        "net_inflow_sum": 0.32,
        "main_inflow_pct_avg": 0.20,
        "positive_inflow_days": 0.18,
        "latest_net_inflow": 0.12,
        "inflow_acceleration": 0.10,
        "large_order_positive_days": 0.08,
    },
    "overheat": {"start_pct": 8.0, "slope": 30.0, "cap": 0.18},
    "state_thresholds": {
        "persistent_score": 0.65,
        "persistent_positive_days": 4,
        "net_inflow_score": 0.45,
        "neutral_score_low": 0.30,
        "neutral_score_high": 0.45,
        "outflow_positive_days_max": 1,
    },
    "snapshot": {
        "cache_subdir": "factor_eval_cache",
        "max_lag_days": 3,
        "error_retry_cooldown_minutes": 10,
    },
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    parsed = _safe_float(value)
    return int(parsed) if parsed is not None else None


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def normalize_board_name(value: Any) -> str:
    """NFKC + 去空白 + 大写归一；只用于「精确等于」比较。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text)
    return text.upper()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


def _load_yaml(filename: str) -> Dict[str, Any]:
    try:
        import yaml

        path = _RESOURCE_DIR / filename
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - 资源损坏时降级到默认值
        logger.warning("sector_flow: load %s failed: %s", filename, exc)
        return {}


@lru_cache(maxsize=1)
def load_settings() -> Dict[str, Any]:
    return _deep_merge(DEFAULT_SETTINGS, _load_yaml("sector_flow.yaml"))


@lru_cache(maxsize=1)
def load_aliases() -> Dict[str, Any]:
    """返回 ``{"canonical": {key: entry}, "alias_index": {normalized: [keys]}}``。"""
    raw = _load_yaml("sector_aliases.yaml")
    canonical: Dict[str, Dict[str, Any]] = {}
    alias_index: Dict[str, List[str]] = {}
    for entry in raw.get("canonical") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        canonical[key] = entry
        for alias in entry.get("aliases") or []:
            normalized = normalize_board_name(alias)
            if not normalized:
                continue
            bucket = alias_index.setdefault(normalized, [])
            if key not in bucket:
                bucket.append(key)
    return {"canonical": canonical, "alias_index": alias_index}


def _previous_weekday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _recent_weekdays(count: int, start: Optional[date] = None) -> List[str]:
    cursor = start or date.today()
    out: List[str] = []
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor = _previous_weekday(cursor)
    return out


def _resolve_cache_dir(cache_dir: Optional[str], settings: Dict[str, Any]) -> Path:
    if cache_dir:
        return Path(cache_dir).expanduser()
    subdir = ((settings.get("snapshot") or {}).get("cache_subdir") or "factor_eval_cache").strip()
    return _REPO_ROOT / "data" / subdir


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("sector_flow: read %s failed: %s", path, exc)
        return None


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# pure calculations (unit-testable, no IO)
# ---------------------------------------------------------------------------


def compute_percentile(values: Sequence[Optional[float]], target: Optional[float]) -> Optional[float]:
    """截面百分位 ``(less + 0.5 * equal) / n``，处理并列。"""
    clean = [v for v in values if v is not None]
    if target is None or not clean:
        return None
    less = sum(1 for v in clean if v < target)
    equal = sum(1 for v in clean if v == target)
    return round((less + 0.5 * equal) / len(clean), 4)


def compute_board_metrics(
    rows: Sequence[Dict[str, Any]],
    window_days: int,
    expected_dates: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """逐日序列 → 5 日汇总事实；``0`` 是合法值，只有 ``None`` 才算缺失。"""
    ordered = sorted(
        (r for r in rows if isinstance(r, dict) and str(r.get("date") or "").strip()),
        key=lambda r: str(r["date"]),
    )
    valid = [r for r in ordered if _safe_float(r.get("main_amount")) is not None]
    amounts = [_safe_float(r.get("main_amount")) for r in valid]
    pcts = [_safe_float(r.get("main_amount_pct")) for r in valid]
    pcts = [p for p in pcts if p is not None]

    latest = amounts[-1] if amounts else None
    acceleration = None
    if len(amounts) >= 2 and latest is not None:
        prior_mean = sum(amounts[:-1]) / len(amounts[:-1])
        acceleration = round(latest - prior_mean, 2)

    large_days = None
    large_samples = 0
    large_positive = 0
    for row in valid:
        super_big = _safe_float(row.get("super_big_amount"))
        big = _safe_float(row.get("big_amount"))
        if super_big is None and big is None:
            continue
        large_samples += 1
        if (super_big or 0.0) + (big or 0.0) > 0:
            large_positive += 1
    if large_samples:
        large_days = large_positive

    present = {str(r.get("date")) for r in valid}
    expected = [str(d) for d in (expected_dates or []) if str(d)]
    missing_dates = [d for d in expected if d not in present]

    return {
        "sample_days": len(valid),
        "net_inflow_sum": round(sum(amounts), 2) if amounts else None,
        "main_inflow_pct_avg": round(sum(pcts) / len(pcts), 4) if pcts else None,
        "positive_inflow_days": sum(1 for a in amounts if a > 0) if amounts else None,
        "latest_net_inflow": round(latest, 2) if latest is not None else None,
        "inflow_acceleration": acceleration,
        "large_order_positive_days": large_days,
        "missing_dates": missing_dates,
        "window_days": int(window_days),
    }


def compute_composite(
    metrics: Dict[str, Any],
    percentiles: Dict[str, Optional[float]],
    weights: Dict[str, float],
    window_days: int,
) -> Optional[float]:
    """仅对可用分项加权，权重自动重归一；全缺失返回 ``None``。"""
    parts: List[Tuple[float, float]] = []

    def add_percentile(name: str) -> None:
        value = percentiles.get(name)
        weight = _safe_float(weights.get(name))
        if value is not None and weight:
            parts.append((weight, float(value)))

    def add_ratio(name: str, days_key: str) -> None:
        days = _safe_int(metrics.get(days_key))
        weight = _safe_float(weights.get(name))
        if days is not None and weight and window_days > 0:
            parts.append((weight, max(0.0, min(1.0, days / window_days))))

    add_percentile("net_inflow_sum")
    add_percentile("main_inflow_pct_avg")
    add_ratio("positive_inflow_days", "positive_inflow_days")
    add_percentile("latest_net_inflow")
    add_percentile("inflow_acceleration")
    add_ratio("large_order_positive_days", "large_order_positive_days")

    total_weight = sum(w for w, _ in parts)
    if not parts or total_weight <= 0:
        return None
    return round(sum(w * v for w, v in parts) / total_weight, 4)


def compute_overheat(
    sector_return: Optional[float],
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], str]:
    """过热惩罚；``None`` → ``(None, "missing")``，不扣分也不用 0 代替。"""
    if sector_return is None:
        return None, "missing"
    cfg = (settings or load_settings()).get("overheat") or {}
    start = _safe_float(cfg.get("start_pct"))
    slope = _safe_float(cfg.get("slope"))
    cap = _safe_float(cfg.get("cap"))
    if start is None or slope is None or cap is None or slope <= 0:
        return None, "missing"
    raw = max(0.0, float(sector_return) - start) / slope
    return round(min(cap, raw), 4), "ok"


def compute_score(
    metrics: Dict[str, Any],
    composite: Optional[float],
    overheat_penalty: Optional[float],
) -> Optional[float]:
    if composite is None:
        return None
    penalty = overheat_penalty or 0.0
    net = _safe_float(metrics.get("net_inflow_sum"))
    if net is None:
        return None
    if net > 0:
        score = 0.25 + 0.75 * composite - penalty
    else:
        score = max(0.0, 0.25 * composite - 0.10 - penalty)
    return round(max(0.0, min(1.0, score)), 4)


def map_state(
    score: Optional[float],
    metrics: Dict[str, Any],
    thresholds: Optional[Dict[str, Any]] = None,
) -> str:
    if score is None:
        return "unknown"
    cfg = thresholds or (load_settings().get("state_thresholds") or {})
    net = _safe_float(metrics.get("net_inflow_sum"))
    positive_days = _safe_int(metrics.get("positive_inflow_days"))
    if net is None or positive_days is None:
        return "unknown"

    persistent_score = _safe_float(cfg.get("persistent_score")) or 0.65
    persistent_days = _safe_int(cfg.get("persistent_positive_days"))
    persistent_days = 4 if persistent_days is None else persistent_days
    net_score = _safe_float(cfg.get("net_inflow_score")) or 0.45
    neutral_low = _safe_float(cfg.get("neutral_score_low"))
    neutral_low = 0.30 if neutral_low is None else neutral_low
    neutral_high = _safe_float(cfg.get("neutral_score_high"))
    neutral_high = 0.45 if neutral_high is None else neutral_high
    outflow_days_max = _safe_int(cfg.get("outflow_positive_days_max"))
    outflow_days_max = 1 if outflow_days_max is None else outflow_days_max

    if score >= persistent_score and positive_days >= persistent_days and net > 0:
        return "persistent_inflow"
    if score >= net_score and net > 0:
        return "net_inflow"
    if net < 0 and positive_days <= outflow_days_max:
        return "outflow"
    if neutral_low <= score <= neutral_high:
        return "neutral"
    if net < 0:
        return "outflow"
    return "neutral"


def _select_most_specific(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """行业类型内选「成分股更少（更具体）」的板块，并列按 bk_code 升序。"""
    usable = [c for c in candidates if isinstance(c, dict) and c.get("bk_code")]
    if not usable:
        return None

    def key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        size = _safe_int(item.get("member_count"))
        return (0 if size and size > 0 else 1, size if size and size > 0 else 10 ** 9, str(item["bk_code"]))

    return sorted(usable, key=key)[0]


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def resolve_board(
    stock_code: str,
    boards: Optional[Sequence[Dict[str, Any]]],
    catalog: Dict[str, Dict[str, Any]],
    aliases: Optional[Dict[str, Any]] = None,
    constituent_index: Optional[Dict[str, Sequence[str]]] = None,
    *,
    boards_error: bool = False,
) -> Dict[str, Any]:
    """个股 → 唯一主行业板块；顺序：code 精确 → alias 精确 → 成分股反查。"""
    aliases = aliases or load_aliases()
    catalog = catalog or {}
    normalized_boards: List[Dict[str, Any]] = []
    for board in boards or []:
        if not isinstance(board, dict):
            continue
        code = str(board.get("code") or board.get("bk_code") or "").strip()
        name = str(board.get("name") or board.get("bk_name") or "").strip()
        if not code and not name:
            continue
        normalized_boards.append(
            {"bk_code": code, "bk_name": name, "type": str(board.get("type") or "").strip()}
        )

    resolution: Dict[str, Any] = {
        "stock_code": str(stock_code or "").strip(),
        "selected": None,
        "candidate_boards": normalized_boards,
        "mapping_method": "unmapped",
        "mapping_confidence": "none",
        "industry_key": None,
        "missing_reason": None,
        "quality_notes": [],
    }

    # 1) bk_code 精确匹配（catalog 本身即东财行业板块全集）
    code_hits = [
        {
            "bk_code": board["bk_code"],
            "bk_name": catalog[board["bk_code"]].get("bk_name") or board["bk_name"],
            "member_count": catalog[board["bk_code"]].get("member_count"),
            "source_name": board["bk_name"],
        }
        for board in normalized_boards
        if board["bk_code"] and board["bk_code"] in catalog
    ]
    if code_hits:
        selected = _select_most_specific(code_hits)
        resolution.update(
            selected={"bk_code": selected["bk_code"], "bk_name": selected["bk_name"]},
            mapping_method="code",
            mapping_confidence="high",
        )
        resolution["industry_key"] = _industry_key_for(selected["bk_name"], aliases)
        return resolution

    # 2) 人工别名表精确等于（禁止子串）
    alias_index: Dict[str, List[str]] = aliases.get("alias_index") or {}
    matched_keys: List[str] = []
    matched_names: List[str] = []
    for board in normalized_boards:
        keys = alias_index.get(normalize_board_name(board["bk_name"])) or []
        if len(keys) > 1:
            resolution["quality_notes"].append(f"alias_ambiguous:{board['bk_name']}")
            continue
        if keys and keys[0] not in matched_keys:
            matched_keys.append(keys[0])
            matched_names.append(board["bk_name"])
    if len(matched_keys) == 1:
        canonical = (aliases.get("canonical") or {}).get(matched_keys[0]) or {}
        bk_code = str(canonical.get("bk_code") or "").strip()
        if bk_code in catalog:
            selected = catalog[bk_code]
            resolution.update(
                selected={"bk_code": bk_code, "bk_name": selected.get("bk_name") or canonical.get("display_name")},
                mapping_method="alias",
                mapping_confidence="high",
                industry_key=matched_keys[0],
            )
            return resolution
        resolution["quality_notes"].append("alias_without_catalog_code")
    elif len(matched_keys) > 1:
        resolution["quality_notes"].append("alias_ambiguous")

    # 3) 成分股反查（受控、默认关闭）
    if constituent_index:
        hits = [
            code
            for code in constituent_index.get(resolution["stock_code"], [])
            if code in catalog
        ]
        if hits:
            selected = _select_most_specific(
                [
                    {"bk_code": code, "bk_name": catalog[code].get("bk_name"), "member_count": catalog[code].get("member_count")}
                    for code in hits
                ]
            )
            resolution.update(
                selected={"bk_code": selected["bk_code"], "bk_name": selected["bk_name"]},
                mapping_method="constituent",
                mapping_confidence="medium",
            )
            resolution["industry_key"] = _industry_key_for(selected["bk_name"], aliases)
            return resolution

    if boards_error:
        resolution["missing_reason"] = "provider_failed"
    elif not normalized_boards:
        resolution["missing_reason"] = "no_bk_code"
    else:
        resolution["missing_reason"] = "unmapped_industry"
        resolution["mapping_confidence"] = "low"
    return resolution


def _industry_key_for(board_name: str, aliases: Dict[str, Any]) -> Optional[str]:
    keys = (aliases.get("alias_index") or {}).get(normalize_board_name(board_name)) or []
    return keys[0] if len(keys) == 1 else None


# ---------------------------------------------------------------------------
# data source (akshare 东财板块；lazy import)
# ---------------------------------------------------------------------------


class AkshareSectorSource:
    """akshare 东财行业板块数据源适配层（薄封装，便于测试注入假数据）。"""

    def __init__(self) -> None:
        import akshare as ak  # noqa: F401 — 延迟导入，避免测试/离线环境启动即失败

        self._ak = ak

    def board_catalog(self) -> List[Dict[str, Any]]:
        df = self._ak.stock_board_industry_name_em()
        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            code = str(row.get("板块代码") or "").strip()
            name = str(row.get("板块名称") or "").strip()
            if not code or not name:
                continue
            up = _safe_float(row.get("上涨家数")) or 0.0
            down = _safe_float(row.get("下跌家数")) or 0.0
            rows.append(
                {
                    "bk_code": code,
                    "bk_name": name,
                    "member_count": int(up + down) or None,
                }
            )
        return rows

    def flow_rank(self, indicator: str = "5日") -> Dict[str, Dict[str, Any]]:
        df = self._ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type="行业资金流")
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            name = str(row.get("名称") or "").strip()
            if not name:
                continue
            out[normalize_board_name(name)] = {
                "bk_name": name,
                "return_pct": _safe_float(row.get(f"{indicator}涨跌幅")),
                "net_inflow": _safe_float(row.get(f"{indicator}主力净流入-净额")),
                "net_inflow_pct": _safe_float(row.get(f"{indicator}主力净流入-净占比")),
            }
        return out

    def board_history(self, board_name: str, limit: int) -> List[Dict[str, Any]]:
        df = self._ak.stock_sector_fund_flow_hist(symbol=board_name)
        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            day = str(row.get("日期") or "").strip()
            if not day:
                continue
            rows.append(
                {
                    "date": day,
                    "main_amount": _safe_float(row.get("主力净流入-净额")),
                    "main_amount_pct": _safe_float(row.get("主力净流入-净占比")),
                    "super_big_amount": _safe_float(row.get("超大单净流入-净额")),
                    "big_amount": _safe_float(row.get("大单净流入-净额")),
                }
            )
        return rows[-max(1, int(limit)):]

    def board_constituents(self, board_name: str) -> List[str]:
        df = self._ak.stock_board_industry_cons_em(symbol=board_name)
        codes: List[str] = []
        for _, row in df.iterrows():
            code = str(row.get("代码") or "").strip()
            if code:
                codes.append(code.zfill(6))
        return codes


def _default_fetcher_manager() -> Any:
    from data_provider.base import DataFetcherManager  # lazy import

    return DataFetcherManager()


# ---------------------------------------------------------------------------
# snapshot build / cache
# ---------------------------------------------------------------------------


def _fetch_series_with_retry(
    source: AkshareSectorSource,
    board_name: str,
    limit: int,
    retries: int,
    backoff_s: float = 0.0,
) -> Tuple[List[Dict[str, Any]], str]:
    """单板块逐日序列；失败按 ``backoff_s * (attempt + 1)`` 线性退避。"""
    attempts = max(0, int(retries)) + 1
    last_error = ""
    for attempt in range(attempts):
        try:
            return source.board_history(board_name, limit), ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts and backoff_s > 0:
                time.sleep(backoff_s * (attempt + 1))
    return [], last_error


def _select_universe(
    mapped_codes: Sequence[str],
    catalog: Dict[str, Dict[str, Any]],
    rank5: Dict[str, Dict[str, Any]],
    limit: int,
) -> List[str]:
    """候选映射板块优先；``limit <= 0`` 时返回全量 catalog。

    全量模式下剩下的板块按 ``bk_code`` 升序补齐，保证构建顺序确定、可复现；
    ``limit > 0`` 时沿用「按 5 日主力净流入绝对值降序补齐」的截断策略。
    """
    ordered: List[str] = []
    for code in mapped_codes:
        if code in catalog and code not in ordered:
            ordered.append(code)

    limit = int(limit or 0)
    if limit <= 0:
        for code in sorted(catalog):
            if code not in ordered:
                ordered.append(code)
        return ordered

    if not ordered:
        return ordered
    limit = max(len(ordered), limit)
    if len(ordered) >= limit:
        return ordered

    ranked: List[Tuple[float, str]] = []
    for normalized_name, row in rank5.items():
        net = _safe_float(row.get("net_inflow"))
        code = _normalized_name_to_code(normalized_name, catalog)
        if code and code not in ordered:
            ranked.append((abs(net) if net is not None else 0.0, code))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for _, code in ranked:
        if len(ordered) >= limit:
            break
        if code not in ordered:
            ordered.append(code)
    return ordered


def _normalized_name_to_code(normalized_name: str, catalog: Dict[str, Dict[str, Any]]) -> Optional[str]:
    for code, entry in catalog.items():
        if normalize_board_name(entry.get("bk_name")) == normalized_name:
            return code
    return None


def build_constituent_index(
    source: AkshareSectorSource,
    catalog: Dict[str, Dict[str, Any]],
    codes: Sequence[str],
    *,
    cache_dir: Optional[str] = None,
    as_of_date: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """按日缓存的成分股反查索引（最后一档，默认关闭）。

    只对 ``codes`` 中尚未出现的股票提前结束扫描；扫描上限由
    ``constituent_max_boards`` 控制，避免全市场逐板块请求。
    """
    settings = settings or load_settings()
    wanted = {str(c) for c in codes if c}
    if not wanted:
        return {}
    cache = _resolve_cache_dir(cache_dir, settings)
    cache_path = cache / _CONSTITUENT_TMPL.format(date=as_of_date or "latest")
    cached = _read_json(cache_path) or {}
    index: Dict[str, List[str]] = {
        code: list(boards) for code, boards in (cached.get("index") or {}).items()
    }
    remaining = wanted - set(index)
    if not remaining:
        return index

    max_boards = max(1, int(settings.get("constituent_max_boards") or 40))
    ordered = sorted(
        (item for item in catalog.values() if item.get("bk_code") and item.get("bk_name")),
        key=lambda item: (_safe_int(item.get("member_count")) or 10 ** 9, str(item["bk_code"])),
    )
    for meta in ordered[:max_boards]:
        if not remaining:
            break
        try:
            members = source.board_constituents(meta["bk_name"])
        except Exception as exc:
            logger.debug("sector_flow: constituents %s failed: %s", meta.get("bk_name"), exc)
            continue
        for stock_code in members:
            if stock_code in remaining:
                index.setdefault(stock_code, []).append(str(meta["bk_code"]))
                remaining.discard(stock_code)
    _write_json_atomic(cache_path, {"as_of_date": as_of_date, "index": index})
    return index


def _fetch_board_series(
    source: AkshareSectorSource,
    boards_meta: Sequence[Tuple[str, Dict[str, Any]]],
    *,
    window_days: int,
    concurrency: int,
    retries: int,
    backoff_s: float,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    series: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    if not boards_meta:
        return series, errors
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _fetch_series_with_retry,
                source,
                meta.get("bk_name") or "",
                window_days + 2,
                retries,
                backoff_s,
            ): code
            for code, meta in boards_meta
        }
        for future, code in futures.items():
            try:
                rows, error = future.result()
            except Exception as exc:  # pragma: no cover - 防御性
                rows, error = [], f"{type(exc).__name__}: {exc}"
            series[code] = rows
            if error:
                errors[code] = error
    return series, errors


def _resolve_window_dates(
    series: Dict[str, Sequence[Dict[str, Any]]],
    as_of_date: Optional[str],
    window_days: int,
) -> Tuple[str, List[str]]:
    all_dates = sorted({str(r.get("date")) for rows in series.values() for r in rows if r.get("date")})
    if as_of_date:
        all_dates = [d for d in all_dates if d <= as_of_date]
    resolved_as_of = all_dates[-1] if all_dates else (as_of_date or "")
    return resolved_as_of, all_dates[-window_days:]


def _build_board_record(
    meta: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    *,
    window_dates: Sequence[str],
    window_days: int,
    sector_return: Optional[float],
    error: str,
) -> Dict[str, Any]:
    allowed = set(window_dates)
    window_rows = [r for r in rows if str(r.get("date") or "") in allowed]
    metrics = compute_board_metrics(window_rows, window_days, expected_dates=window_dates)
    record: Dict[str, Any] = {
        "bk_code": meta.get("bk_code"),
        "bk_name": meta.get("bk_name"),
        "member_count": meta.get("member_count"),
        "metrics": metrics,
        "sector_return_window": sector_return,
        "return_source": _SOURCE_RANK if sector_return is not None else "missing",
        "error": error,
    }
    if metrics["sample_days"] < 2:
        record.update(
            status="missing",
            missing_reason="provider_failed" if error else "insufficient_history",
        )
    elif metrics["sample_days"] < window_days:
        record.update(status="partial", missing_reason=None)
    else:
        record.update(status="ok", missing_reason=None)
    return record


def _apply_cross_section(
    raw: Dict[str, Dict[str, Any]],
    settings: Dict[str, Any],
    window_days: int,
) -> None:
    """基于当前 ``raw`` 全量重算分位与下游分数（重试后也复用）。"""
    usable = [r for r in raw.values() if r["metrics"]["sample_days"] >= 2]
    percentile_values = {
        "net_inflow_sum": [r["metrics"]["net_inflow_sum"] for r in usable],
        "main_inflow_pct_avg": [r["metrics"]["main_inflow_pct_avg"] for r in usable],
        "latest_net_inflow": [r["metrics"]["latest_net_inflow"] for r in usable],
    }
    for record in raw.values():
        metrics = record["metrics"]
        percentiles = {
            "net_inflow_sum": compute_percentile(percentile_values["net_inflow_sum"], metrics.get("net_inflow_sum")),
            "main_inflow_pct_avg": compute_percentile(
                percentile_values["main_inflow_pct_avg"], metrics.get("main_inflow_pct_avg")
            ),
            "latest_net_inflow": compute_percentile(
                percentile_values["latest_net_inflow"], metrics.get("latest_net_inflow")
            ),
        }
        record["percentiles"] = percentiles
        if record["status"] == "missing":
            record.update(composite=None, overheat_penalty=None, overheat_status="missing", score=None, state="unknown")
            continue
        record["composite"] = compute_composite(
            metrics, percentiles, settings.get("weights") or {}, window_days
        )
        penalty, overheat_status = compute_overheat(record.get("sector_return_window"), settings)
        record["overheat_penalty"] = penalty
        record["overheat_status"] = overheat_status
        record["score"] = compute_score(metrics, record["composite"], penalty)
        record["state"] = map_state(record["score"], metrics, settings.get("state_thresholds") or {})


def _snapshot_meta(
    raw: Dict[str, Dict[str, Any]],
    errors: Dict[str, str],
    catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """覆盖率与分位口径：全量发起且无抓取失败才记 ``complete``。"""
    failed_boards = [
        {"bk_code": code, "bk_name": (raw.get(code) or {}).get("bk_name"), "error": message}
        for code, message in sorted(errors.items())
    ]
    insufficient_history = sorted(
        code
        for code, record in raw.items()
        if record.get("status") == "missing" and record.get("missing_reason") == "insufficient_history"
    )
    sample_size = sum(1 for record in raw.values() if record["metrics"]["sample_days"] >= 2)
    coverage = round(len(raw) / len(catalog), 4) if catalog else 0.0
    complete = bool(catalog) and len(raw) >= len(catalog) and not failed_boards
    return {
        "percentile_universe": "complete" if complete else "partial",
        "universe_coverage": coverage,
        "percentile_sample_size": sample_size,
        "universe_size": len(raw),
        "catalog_size": len(catalog),
        "failed_boards": failed_boards,
        "insufficient_history_boards": insufficient_history,
    }


def _build_snapshot_payload(
    source: AkshareSectorSource,
    *,
    catalog: Dict[str, Dict[str, Any]],
    rank5: Dict[str, Dict[str, Any]],
    universe_codes: Sequence[str],
    as_of_date: Optional[str],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    window_days = max(1, int(settings.get("window_days") or 5))
    concurrency = max(1, int(settings.get("concurrency") or 8))
    retries = max(0, int(settings.get("retry") or 0))
    backoff_s = _safe_float(settings.get("retry_backoff_s")) or 0.0

    boards_meta = [(code, catalog[code]) for code in universe_codes if code in catalog]
    series, errors = _fetch_board_series(
        source,
        boards_meta,
        window_days=window_days,
        concurrency=concurrency,
        retries=retries,
        backoff_s=backoff_s,
    )
    resolved_as_of, window_dates = _resolve_window_dates(series, as_of_date, window_days)

    raw: Dict[str, Dict[str, Any]] = {}
    for code, meta in boards_meta:
        rank_row = rank5.get(normalize_board_name(meta.get("bk_name"))) or {}
        raw[code] = _build_board_record(
            meta,
            series.get(code, []),
            window_dates=window_dates,
            window_days=window_days,
            sector_return=_safe_float(rank_row.get("return_pct")),
            error=errors.get(code) or "",
        )
    _apply_cross_section(raw, settings, window_days)

    payload: Dict[str, Any] = {
        "version": 1,
        "as_of_date": resolved_as_of,
        "window_dates": list(window_dates),
        "generated_at": _now_iso(),
        "window_days": window_days,
        "unit": "yuan",
        "source_chain": [_SOURCE_CATALOG, _SOURCE_RANK, _SOURCE_HISTORY],
        "board_catalog": catalog,
        "boards": raw,
        "errors": errors,
    }
    payload.update(_snapshot_meta(raw, errors, catalog))
    return payload


def _should_retry_failed(
    snapshot: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> bool:
    if not (snapshot or {}).get("errors"):
        return False
    cooldown = _safe_int((settings.get("snapshot") or {}).get("error_retry_cooldown_minutes"))
    if cooldown is None or cooldown <= 0:
        return True
    stamp = snapshot.get("last_error_retry_at")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(str(stamp))
    except Exception:
        return True
    return (now or datetime.now()) - last >= timedelta(minutes=cooldown)


def retry_failed_boards(
    snapshot: Dict[str, Any],
    *,
    cache_dir: Optional[str] = None,
    source: Optional[AkshareSectorSource] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """只重取失败板块并重算截面分位；成功覆盖、失败保留，不整批重来。"""
    settings = settings or load_settings()
    snapshot = dict(snapshot or {})
    raw = dict(snapshot.get("boards") or {})
    errors = dict(snapshot.get("errors") or {})
    if not errors:
        return snapshot

    catalog: Dict[str, Dict[str, Any]] = dict(snapshot.get("board_catalog") or {})
    window_days = max(1, int(snapshot.get("window_days") or settings.get("window_days") or 5))
    concurrency = max(1, int(settings.get("concurrency") or 8))
    retries = max(0, int(settings.get("retry") or 0))
    backoff_s = _safe_float(settings.get("retry_backoff_s")) or 0.0
    source = source or AkshareSectorSource()

    boards_meta = [
        (code, catalog.get(code) or {"bk_code": code, "bk_name": (raw.get(code) or {}).get("bk_name")})
        for code in sorted(errors)
    ]
    series, new_errors = _fetch_board_series(
        source,
        boards_meta,
        window_days=window_days,
        concurrency=concurrency,
        retries=retries,
        backoff_s=backoff_s,
    )

    window_dates = list(snapshot.get("window_dates") or [])
    if not window_dates:
        _, window_dates = _resolve_window_dates(series, snapshot.get("as_of_date"), window_days)

    for code, meta in boards_meta:
        if code in new_errors:
            errors[code] = new_errors[code]
            continue
        existing = raw.get(code) or {}
        raw[code] = _build_board_record(
            meta,
            series.get(code, []),
            window_dates=window_dates,
            window_days=window_days,
            sector_return=existing.get("sector_return_window"),
            error="",
        )
        errors.pop(code, None)

    _apply_cross_section(raw, settings, window_days)
    snapshot.update(
        boards=raw,
        errors=errors,
        window_days=window_days,
        window_dates=window_dates,
        generated_at=_now_iso(),
        last_error_retry_at=_now_iso(),
    )
    snapshot.update(_snapshot_meta(raw, errors, catalog))
    if cache_dir and snapshot.get("as_of_date"):
        _write_json_atomic(
            _resolve_cache_dir(cache_dir, settings) / _SNAPSHOT_TMPL.format(date=snapshot["as_of_date"]),
            snapshot,
        )
    return snapshot


def build_or_load_snapshot(
    as_of_date: Optional[str] = None,
    *,
    universe_codes: Optional[Sequence[str]] = None,
    cache_dir: Optional[str] = None,
    force_refresh: bool = False,
    retry_failed: bool = False,
    source: Optional[AkshareSectorSource] = None,
    settings: Optional[Dict[str, Any]] = None,
    catalog: Optional[Dict[str, Dict[str, Any]]] = None,
    rank5: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """按交易日构建/读取一次板块快照（同一进程内串行，避免重复构建）。

    ``retry_failed=True`` 时，若缓存快照存在且覆盖所需板块，只对失败板块做
    单板块重试（受 ``snapshot.error_retry_cooldown_minutes`` 冷却约束），不会
    整批重跑。
    """
    settings = settings or load_settings()
    cache = _resolve_cache_dir(cache_dir, settings)
    universe = [str(c) for c in (universe_codes or []) if str(c or "").strip()]
    candidates = [as_of_date] if as_of_date else _recent_weekdays(5)

    if not force_refresh:
        snapshot = _find_acceptable_snapshot(cache, candidates, universe, settings)
        if snapshot:
            if retry_failed and _should_retry_failed(snapshot, settings):
                return retry_failed_boards(snapshot, cache_dir=str(cache), source=source, settings=settings)
            return snapshot

    with _BUILD_LOCK:
        if not force_refresh:
            snapshot = _find_acceptable_snapshot(cache, candidates, universe, settings)
            if snapshot:
                if retry_failed and _should_retry_failed(snapshot, settings):
                    return retry_failed_boards(snapshot, cache_dir=str(cache), source=source, settings=settings)
                return snapshot

        source = source or AkshareSectorSource()
        if catalog is None:
            catalog = {item["bk_code"]: item for item in source.board_catalog() if item.get("bk_code")}
        if rank5 is None:
            rank5 = source.flow_rank("5日")

        selected = _select_universe(universe, catalog, rank5, int(settings.get("universe_limit") or 0))
        snapshot = _build_snapshot_payload(
            source,
            catalog=catalog,
            rank5=rank5,
            universe_codes=selected,
            as_of_date=as_of_date,
            settings=settings,
        )
        if snapshot.get("as_of_date"):
            _write_json_atomic(
                cache / _SNAPSHOT_TMPL.format(date=snapshot["as_of_date"]),
                snapshot,
            )
        return snapshot


def _snapshot_covers(snapshot: Dict[str, Any], universe: Sequence[str]) -> bool:
    if not universe:
        return True
    boards = snapshot.get("boards") or {}
    return all(code in boards for code in universe)


def _snapshot_acceptable(
    snapshot: Dict[str, Any],
    universe: Sequence[str],
    settings: Dict[str, Any],
) -> bool:
    """缓存快照是否可直接复用。

    全量模式（``universe_limit <= 0``）下还要求快照的 ``universe_size`` 不小于
    ``catalog_size``，避免 FAC-01b 遗留的 120 板块快照被继续当成可用快照。
    """
    if not _snapshot_covers(snapshot, universe):
        return False
    if int(settings.get("universe_limit") or 0) <= 0:
        catalog_size = _safe_int(snapshot.get("catalog_size")) or 0
        if catalog_size and int(snapshot.get("universe_size") or 0) < catalog_size:
            return False
    return True


def _find_acceptable_snapshot(
    cache: Path,
    candidates: Sequence[str],
    universe: Sequence[str],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    for candidate in candidates:
        snapshot = _read_json(cache / _SNAPSHOT_TMPL.format(date=candidate))
        if snapshot and _snapshot_acceptable(snapshot, universe, settings):
            return snapshot
    return None


def load_snapshot_for_date(
    as_of_date: Optional[str],
    *,
    cache_dir: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    max_lag_days: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], int]:
    """读取不晚于 ``as_of_date`` 的最近快照；返回 ``(snapshot, lag_trading_days)``。"""
    settings = settings or load_settings()
    cache = _resolve_cache_dir(cache_dir, settings)
    if max_lag_days is None:
        max_lag_days = int((settings.get("snapshot") or {}).get("max_lag_days") or 3)

    cursor: Optional[date] = None
    if as_of_date:
        try:
            cursor = datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()
        except Exception:
            cursor = None
    else:
        cursor = date.today()

    lag = 0
    while cursor is not None and lag <= max_lag_days:
        snapshot = _read_json(cache / _SNAPSHOT_TMPL.format(date=cursor.isoformat()))
        if snapshot:
            return snapshot, lag
        cursor = _previous_weekday(cursor)
        lag += 1
    return None, lag


# ---------------------------------------------------------------------------
# fact build / attach / summarize
# ---------------------------------------------------------------------------


def build_sector_flow_fact(
    stock_code: str,
    resolution: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
) -> Any:
    """把映射结果 + 快照记录转成 ``SectorFlowFact``（缺失一律 ``None``）。"""
    from src.agent.candidate_experts_v2.schemas import SectorFlowFact

    settings = settings or load_settings()
    window_days = max(1, int(settings.get("window_days") or 5))
    snapshot = snapshot or {}
    as_of_date = snapshot.get("as_of_date")
    source_chain = list(snapshot.get("source_chain") or [])

    fact = SectorFlowFact(
        as_of_date=as_of_date or None,
        window_days=window_days,
        unit=snapshot.get("unit") or "yuan",
        computed_at=_now_iso(),
        source_chain=source_chain,
        mapping_method=resolution.get("mapping_method") or "unmapped",
        mapping_confidence=resolution.get("mapping_confidence") or "none",
        industry_key=resolution.get("industry_key"),
        candidate_boards=list(resolution.get("candidate_boards") or []),
        quality_notes=list(resolution.get("quality_notes") or []),
    )

    selected = resolution.get("selected") or {}
    if not selected.get("bk_code"):
        fact.status = "missing"
        fact.missing_reason = resolution.get("missing_reason") or "unmapped_industry"
        fact.state = "unknown"
        return fact

    fact.bk_code = str(selected.get("bk_code"))
    fact.bk_name_raw = selected.get("bk_name")
    record = (snapshot.get("boards") or {}).get(fact.bk_code)
    if not record:
        fact.status = "missing"
        fact.missing_reason = "provider_failed"
        fact.state = "unknown"
        fact.quality_notes.append("board_missing_in_snapshot")
        return fact

    metrics = record.get("metrics") or {}
    fact.sample_days = int(metrics.get("sample_days") or 0)
    fact.net_inflow_sum = metrics.get("net_inflow_sum")
    fact.main_inflow_pct_avg = metrics.get("main_inflow_pct_avg")
    fact.positive_inflow_days = metrics.get("positive_inflow_days")
    fact.latest_net_inflow = metrics.get("latest_net_inflow")
    fact.inflow_acceleration = metrics.get("inflow_acceleration")
    fact.large_order_positive_days = metrics.get("large_order_positive_days")
    fact.sector_return_window = record.get("sector_return_window")
    fact.return_source = record.get("return_source")

    record_status = record.get("status") or "missing"
    if record_status == "missing" or fact.sample_days < 2:
        fact.status = "missing"
        fact.missing_reason = record.get("missing_reason") or "insufficient_history"
        fact.state = "unknown"
        if metrics.get("missing_dates"):
            fact.quality_notes.append(f"missing_dates:{','.join(metrics['missing_dates'])}")
        return fact

    if record_status == "partial" or fact.sample_days < window_days:
        fact.status = "partial"
        fact.score_partial = True
        fact.quality_notes.append(f"sample_days={fact.sample_days}/{window_days}")
        if metrics.get("missing_dates"):
            fact.quality_notes.append(f"missing_dates:{','.join(metrics['missing_dates'])}")
    else:
        fact.status = "ok"

    fact.composite = record.get("composite")
    fact.overheat_penalty = record.get("overheat_penalty")
    fact.overheat_status = record.get("overheat_status") or "missing"
    fact.score = record.get("score")
    fact.state = record.get("state") or "unknown"
    if fact.overheat_status == "missing":
        fact.quality_notes.append("sector_return_missing:no_overheat_penalty")
    if snapshot.get("percentile_universe") == "partial":
        fact.quality_notes.append(
            f"percentile_universe=partial({snapshot.get('universe_size')}/{snapshot.get('catalog_size')})"
        )
    return fact


def attach_sector_flow(
    targets: Sequence[Any],
    *,
    as_of_date: Optional[str] = None,
    enabled: bool = False,
    cache_dir: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    fetcher_manager: Any = None,
    source: Optional[AkshareSectorSource] = None,
    max_workers: int = 4,
    boards_by_code: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """候选确定后统一挂载板块资金流观察值；关闭时立即返回。

    ``targets`` 只需有 ``.code`` 与 ``.fact_sheet`` 属性（FeatureRow /
    AggregatedCandidate 均满足）。任何异常只记日志，不抛到主流程。
    """
    if not enabled:
        return {"enabled": False}

    settings = settings or load_settings()
    rows = [t for t in targets if getattr(t, "fact_sheet", None) is not None and getattr(t, "code", "")]
    if not rows:
        return {"enabled": True, "as_of_date": None, "coverage": 0.0, "status_counts": {}, "mapping_method_counts": {}, "missing_reason_top3": [], "total": 0}

    try:
        cache = _resolve_cache_dir(cache_dir, settings)
        source = source or AkshareSectorSource()
        cached, lag_days = load_snapshot_for_date(as_of_date, cache_dir=str(cache), settings=settings)
        catalog: Dict[str, Dict[str, Any]] = dict((cached or {}).get("board_catalog") or {})
        rank5: Dict[str, Dict[str, Any]] = {}
        if not catalog:
            catalog = {item["bk_code"]: item for item in source.board_catalog() if item.get("bk_code")}

        boards_map: Dict[str, Any] = boards_by_code if boards_by_code is not None else {}
        if boards_by_code is None:
            manager = fetcher_manager or _default_fetcher_manager()
            with ThreadPoolExecutor(max_workers=max(1, int(max_workers or 1))) as pool:
                futures = {pool.submit(manager.get_belong_boards, code): code for code in {t.code for t in rows}}
                for future, code in futures.items():
                    try:
                        boards_map[code] = future.result()
                    except Exception as exc:
                        logger.debug("sector_flow: belong_boards %s failed: %s", code, exc)
                        boards_map[code] = None

        aliases = load_aliases()
        resolutions = {
            row.code: resolve_board(
                row.code,
                boards_map.get(row.code),
                catalog,
                aliases,
                boards_error=boards_map.get(row.code) is None,
            )
            for row in rows
        }

        if bool(settings.get("constituent_fallback")) and any(
            not resolution.get("selected") for resolution in resolutions.values()
        ):
            unmatched = [code for code, resolution in resolutions.items() if not resolution.get("selected")]
            index = build_constituent_index(
                source,
                catalog,
                unmatched,
                cache_dir=str(cache),
                as_of_date=(cached or {}).get("as_of_date") or as_of_date,
                settings=settings,
            )
            for code in unmatched:
                if index.get(code):
                    resolutions[code] = resolve_board(
                        code,
                        boards_map.get(code),
                        catalog,
                        aliases,
                        constituent_index=index,
                        boards_error=boards_map.get(code) is None,
                    )

        mapped_codes = [
            resolution["selected"]["bk_code"]
            for resolution in resolutions.values()
            if resolution.get("selected")
        ]
        if cached and _snapshot_acceptable(cached, mapped_codes, settings):
            snapshot = cached
            if _should_retry_failed(snapshot, settings):
                snapshot = retry_failed_boards(
                    snapshot, cache_dir=str(cache), source=source, settings=settings
                )
        else:
            if not rank5:
                rank5 = source.flow_rank("5日")
            snapshot = build_or_load_snapshot(
                as_of_date,
                universe_codes=mapped_codes,
                cache_dir=str(cache),
                retry_failed=True,
                source=source,
                settings=settings,
                catalog=catalog,
                rank5=rank5,
            )

        facts = []
        for row in rows:
            fact = build_sector_flow_fact(row.code, resolutions[row.code], snapshot, settings)
            if cached and lag_days and snapshot is cached and fact.status != "missing":
                max_lag = int((settings.get("snapshot") or {}).get("max_lag_days") or 3)
                if lag_days > max_lag:
                    fact.status = "missing"
                    fact.missing_reason = "date_mismatch"
                    fact.score = None
                    fact.state = "unknown"
                else:
                    fact.quality_notes.append(f"snapshot_lag_days={lag_days}")
            row.fact_sheet.sector_flow = fact
            facts.append(fact)
        return summarize_sector_flow(facts, enabled=True)
    except Exception as exc:
        logger.warning("sector_flow: attach failed: %s", exc, exc_info=True)
        return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}


def summarize_sector_flow(facts: Sequence[Any], *, enabled: bool = True) -> Dict[str, Any]:
    items = [f for f in facts if f is not None]
    status_counts: Dict[str, int] = {}
    method_counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    as_of_dates: Dict[str, int] = {}
    for fact in items:
        status = getattr(fact, "status", "missing")
        status_counts[status] = status_counts.get(status, 0) + 1
        method = getattr(fact, "mapping_method", "unmapped")
        method_counts[method] = method_counts.get(method, 0) + 1
        reason = getattr(fact, "missing_reason", None)
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        day = getattr(fact, "as_of_date", None)
        if day:
            as_of_dates[day] = as_of_dates.get(day, 0) + 1

    total = len(items)
    covered = status_counts.get("ok", 0) + status_counts.get("partial", 0)
    top_reasons = sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {
        "enabled": enabled,
        "total": total,
        "coverage": round(covered / total, 4) if total else 0.0,
        "status_counts": status_counts,
        "mapping_method_counts": method_counts,
        "missing_reason_top3": [{"reason": reason, "count": count} for reason, count in top_reasons],
        "as_of_date": max(as_of_dates.items(), key=lambda kv: (kv[1], kv[0]))[0] if as_of_dates else None,
        "usage": "observation",
        "display_only": True,
    }


def serialize_for_display(fact: Any) -> Optional[Dict[str, Any]]:
    if fact is None:
        return None
    fields = (
        "status",
        "state",
        "score",
        "score_partial",
        "as_of_date",
        "bk_code",
        "bk_name_raw",
        "industry_key",
        "mapping_method",
        "mapping_confidence",
        "window_days",
        "sample_days",
        "unit",
        "net_inflow_sum",
        "main_inflow_pct_avg",
        "positive_inflow_days",
        "latest_net_inflow",
        "inflow_acceleration",
        "large_order_positive_days",
        "sector_return_window",
        "return_source",
        "composite",
        "overheat_penalty",
        "overheat_status",
        "missing_reason",
        "quality_notes",
        "source_chain",
        "computed_at",
        "usage",
        "display_only",
    )
    payload: Dict[str, Any] = {}
    for name in fields:
        value = getattr(fact, name, None)
        if value is None or value == []:
            continue
        payload[name] = value
    return payload


# ---------------------------------------------------------------------------
# diagnostics: 20-stock mapping probe
# ---------------------------------------------------------------------------


def probe_sector_mapping(
    codes: Sequence[str],
    *,
    source: Optional[AkshareSectorSource] = None,
    fetcher_manager: Any = None,
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    """映射对拍：返回每只股票的所属行业板块、code/alias 命中与未命中原因。"""
    source = source or AkshareSectorSource()
    manager = fetcher_manager or _default_fetcher_manager()
    catalog = {item["bk_code"]: item for item in source.board_catalog() if item.get("bk_code")}
    aliases = load_aliases()

    with ThreadPoolExecutor(max_workers=max(1, int(max_workers or 1))) as pool:
        futures = {pool.submit(manager.get_belong_boards, code): code for code in codes}
        boards_by_code = {}
        for future, code in futures.items():
            try:
                boards_by_code[code] = future.result()
            except Exception:
                boards_by_code[code] = None

    results: List[Dict[str, Any]] = []
    for code in codes:
        boards = boards_by_code.get(code)
        resolution = resolve_board(code, boards, catalog, aliases, boards_error=boards is None)
        results.append(
            {
                "code": code,
                "board_count": len(boards or []),
                "industry_hits": [
                    board
                    for board in (boards or [])
                    if str(board.get("code") or "") in catalog
                ],
                "mapping_method": resolution["mapping_method"],
                "mapping_confidence": resolution["mapping_confidence"],
                "selected": resolution["selected"],
                "industry_key": resolution["industry_key"],
                "missing_reason": resolution["missing_reason"],
                "quality_notes": resolution["quality_notes"],
            }
        )
    return results
