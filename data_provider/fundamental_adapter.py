# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import os
import copy
import concurrent.futures
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests
from data_provider.tushare_client import query_tushare_api

logger = logging.getLogger(__name__)

_STOCKAPI_CODE_FLOW_UPDATE_HOUR = 15
_STOCKAPI_CODE_FLOW_UPDATE_MINUTE = 30
_STOCKAPI_MIN_INTERVAL_SECONDS = 0.08
_STOCKAPI_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
_STOCKAPI_CACHE_TTL_SECONDS = 15.0
_STOCKAPI_REQUEST_LOCK = threading.Lock()
_STOCKAPI_RESPONSE_CACHE: Dict[Tuple[str, Tuple[Tuple[str, str], ...], bool], Tuple[float, Dict[str, Any]]] = {}
_stockapi_last_request_at = 0.0
_CAPITAL_FLOW_AUDIT_SOURCES = (
    "tushare_moneyflow_dc",
    "tushare_moneyflow_ths",
    "tushare_moneyflow",
)
_CAPITAL_FLOW_AUDIT_FIELDS = (
    "main_net_inflow",
    "net_inflow",
    "main_inflow_5d",
    "net_inflow_5d",
)
_CAPITAL_FLOW_CONFLICT_MIN_ABS_CNY = 1_000_000.0
_CAPITAL_FLOW_CONFLICT_RELATIVE_DELTA = 0.5

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _akshare_fund_flow_market(stock_code: str) -> str:
    """Return AkShare market parameter for A-share fund-flow endpoints."""
    code = _normalize_code(stock_code)
    if code.startswith(("60", "68", "900")):
        return "sh"
    if code.startswith(("43", "81", "82", "83", "87", "88", "92")):
        return "bj"
    return "sz"


def _stockapi_code_flow_url() -> str:
    return (
        os.getenv("STOCKAPI_URL", "").strip()
        or "https://www.stockapi.com.cn/v1/base/codeFlow"
    )


def _stockapi_default_completed_date() -> str:
    now = datetime.now()
    if now.hour < 16:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _stockapi_code_flow_completed_date(now: Optional[datetime] = None) -> datetime.date:
    """StockAPI codeFlow is updated at 15:30; before that, query yesterday."""
    current = now or datetime.now()
    cutoff = current.replace(
        hour=_STOCKAPI_CODE_FLOW_UPDATE_HOUR,
        minute=_STOCKAPI_CODE_FLOW_UPDATE_MINUTE,
        second=0,
        microsecond=0,
    )
    if current < cutoff:
        return _previous_weekday((current - timedelta(days=1)).date())
    return _previous_weekday(current.date())


def _previous_weekday(value: date) -> date:
    """Return *value* if it is a weekday, otherwise the previous Friday."""
    result = value
    while result.weekday() >= 5:
        result = result - timedelta(days=1)
    return result


def _parse_stockapi_code_flow_date(value: Any, field_name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field_name} must be YYYY-MM-DD or YYYYMMDD")


def _normalize_stockapi_page_value(value: Any, default: int, minimum: int = 1, maximum: int = 200) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _to_tushare_ts_code(stock_code: str) -> str:
    raw = _safe_str(stock_code).upper()
    if "." in raw and raw.endswith((".SH", ".SZ", ".BJ", ".HK")):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _latest_completed_weekday(now: Optional[datetime] = None) -> date:
    current = now or datetime.now()
    cutoff = current.replace(hour=15, minute=30, second=0, microsecond=0)
    base_day = current.date() if current >= cutoff else (current - timedelta(days=1)).date()
    return _previous_weekday(base_day)


def _date_key_from_tushare(value: Any) -> str:
    text = _safe_str(value).replace("-", "")[:8]
    if len(text) != 8:
        return ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _sum_last_values(values_by_date: Dict[str, float], count: int) -> Optional[float]:
    rows = sorted(values_by_date.items(), key=lambda item: item[0])
    if not rows:
        return None
    return float(sum(value for _, value in rows[-count:]))


def _latest_value(values_by_date: Dict[str, float], latest_date: str) -> Optional[float]:
    return values_by_date.get(latest_date)


def _has_capital_flow_value(payload: Dict[str, Any]) -> bool:
    keys = (
        "main_net_inflow",
        "net_inflow",
        "large_net_inflow",
        "extra_large_net_inflow",
        "inflow_5d",
        "net_inflow_5d",
    )
    return any(payload.get(key) is not None for key in keys)


def _capital_flow_direction(value: Any) -> int:
    amount = _safe_float(value)
    if amount is None or abs(amount) < 1e-6:
        return 0
    return 1 if amount > 0 else -1


def _capital_flow_relative_delta(left: float, right: float) -> float:
    denominator = max(abs(left), abs(right), 1.0)
    return abs(left - right) / denominator


def _capital_flow_source_summary(flow: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "latest_date": flow.get("latest_date"),
        "main_net_inflow": flow.get("main_net_inflow"),
        "net_inflow": flow.get("net_inflow"),
        "main_inflow_definition": flow.get("main_inflow_definition"),
        "net_inflow_definition": flow.get("net_inflow_definition"),
    }


def _compare_capital_flow_sources(
    selected_source: str,
    selected_flow: Dict[str, Any],
    other_source: str,
    other_flow: Dict[str, Any],
) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    selected_date = selected_flow.get("latest_date")
    other_date = other_flow.get("latest_date")
    if selected_date and other_date and selected_date != other_date:
        conflicts.append({
            "type": "stale_source",
            "severity": "medium",
            "selected_source": selected_source,
            "other_source": other_source,
            "selected_date": selected_date,
            "other_date": other_date,
            "message": "Capital-flow sources use different latest trade dates; keep selected_flow_source authoritative.",
        })

    for field in _CAPITAL_FLOW_AUDIT_FIELDS:
        selected_value = _safe_float(selected_flow.get(field))
        other_value = _safe_float(other_flow.get(field))
        if selected_value is None or other_value is None:
            continue
        selected_direction = _capital_flow_direction(selected_value)
        other_direction = _capital_flow_direction(other_value)
        if selected_direction and other_direction and selected_direction != other_direction:
            conflicts.append({
                "type": "direction_conflict",
                "severity": "high",
                "field": field,
                "selected_source": selected_source,
                "other_source": other_source,
                "selected_value": selected_value,
                "other_value": other_value,
                "selected_date": selected_date,
                "other_date": other_date,
                "message": "Capital-flow source directions disagree; keep selected_flow_source authoritative.",
            })
            continue

        abs_delta = abs(selected_value - other_value)
        relative_delta = _capital_flow_relative_delta(selected_value, other_value)
        if (
            abs_delta >= _CAPITAL_FLOW_CONFLICT_MIN_ABS_CNY
            and relative_delta >= _CAPITAL_FLOW_CONFLICT_RELATIVE_DELTA
        ):
            conflicts.append({
                "type": "magnitude_divergence",
                "severity": "medium",
                "field": field,
                "selected_source": selected_source,
                "other_source": other_source,
                "selected_value": selected_value,
                "other_value": other_value,
                "absolute_delta": abs_delta,
                "relative_delta": round(relative_delta, 4),
                "selected_date": selected_date,
                "other_date": other_date,
                "message": "Capital-flow source magnitudes diverge; keep selected_flow_source authoritative.",
            })
    return conflicts


def _stockapi_endpoint_url(path: str) -> str:
    cleaned = _safe_str(path)
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return "https://www.stockapi.com.cn" + cleaned


def _stockapi_cache_key(
    endpoint: str,
    params: Dict[str, Any],
    *,
    has_token: bool,
) -> Tuple[str, Tuple[Tuple[str, str], ...], bool]:
    cache_params = tuple(sorted((str(key), str(value)) for key, value in params.items() if key != "token"))
    return endpoint, cache_params, has_token


def _stockapi_is_rate_limited(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("code") or "") == "88888"


def _stockapi_get_cached_payload(
    key: Tuple[str, Tuple[Tuple[str, str], ...], bool],
    *,
    max_age_s: float = _STOCKAPI_CACHE_TTL_SECONDS,
) -> Optional[Dict[str, Any]]:
    cached = _STOCKAPI_RESPONSE_CACHE.get(key)
    if not cached:
        return None
    ts, payload = cached
    if time.monotonic() - ts > max_age_s:
        _STOCKAPI_RESPONSE_CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def _stockapi_set_cached_payload(
    key: Tuple[str, Tuple[Tuple[str, str], ...], bool],
    payload: Dict[str, Any],
) -> None:
    _STOCKAPI_RESPONSE_CACHE[key] = (time.monotonic(), copy.deepcopy(payload))


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


def _first_column_by_keywords(df: pd.DataFrame, keywords: List[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    for col in df.columns:
        col_s = str(col)
        if any(keyword in col_s for keyword in keywords):
            return col
    return None


def _row_date_value(row: pd.Series) -> Optional[str]:
    value = _pick_by_keywords(row, ["日期", "交易日", "时间", "date"])
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else (_safe_str(value) or None)


def _compact_numeric_row(row: pd.Series, field_map: Dict[str, List[str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for target_key, keywords in field_map.items():
        payload[target_key] = _safe_float(_pick_by_keywords(row, keywords))
    return payload


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _stockapi_get_json(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 3.0,
        use_cache: bool = False,
        cache_ttl_s: float = _STOCKAPI_CACHE_TTL_SECONDS,
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        global _stockapi_last_request_at
        request_params: Dict[str, Any] = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and str(value).strip() != ""
        }
        token = os.getenv("STOCKAPI_TOKEN", "").strip()
        if token:
            request_params["token"] = token
        cache_key = _stockapi_cache_key(endpoint, request_params, has_token=bool(token))
        if use_cache:
            cached = _stockapi_get_cached_payload(cache_key, max_age_s=cache_ttl_s)
            if cached is not None:
                return cached, []

        errors: List[str] = []
        attempts = 1 + len(_STOCKAPI_RETRY_BACKOFF_SECONDS)
        with _STOCKAPI_REQUEST_LOCK:
            for attempt in range(attempts):
                now = time.monotonic()
                wait_s = _STOCKAPI_MIN_INTERVAL_SECONDS - (now - _stockapi_last_request_at)
                if wait_s > 0:
                    time.sleep(wait_s)
                try:
                    response = requests.get(
                        _stockapi_endpoint_url(endpoint),
                        params=request_params,
                        timeout=timeout,
                    )
                    _stockapi_last_request_at = time.monotonic()
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:
                    return None, [f"stockapi:{endpoint}:{type(exc).__name__}:{exc}"]

                if not isinstance(payload, dict):
                    return None, [f"stockapi:{endpoint}:invalid_response"]
                if not _stockapi_is_rate_limited(payload):
                    if payload.get("code") == 20000 and use_cache:
                        _stockapi_set_cached_payload(cache_key, payload)
                    break
                errors.append(f"stockapi:{endpoint}:88888:{payload.get('msg')}")
                if attempt >= attempts - 1:
                    return payload, errors
                time.sleep(_STOCKAPI_RETRY_BACKOFF_SECONDS[attempt])

        if not isinstance(payload, dict):
            return None, [f"stockapi:{endpoint}:invalid_response"]
        if payload.get("code") != 20000:
            return payload, errors or [f"stockapi:{endpoint}:{payload.get('code')}:{payload.get('msg')}"]
        return payload, []

    def _stockapi_data_rows(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        use_cache: bool = False,
        cache_ttl_s: float = _STOCKAPI_CACHE_TTL_SECONDS,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        payload, errors = self._stockapi_get_json(
            endpoint,
            params=params,
            use_cache=use_cache,
            cache_ttl_s=cache_ttl_s,
        )
        if errors:
            return [], errors
        data = (payload or {}).get("data")
        if not isinstance(data, list):
            return [], [f"stockapi:{endpoint}:empty_data"]
        rows = [row for row in data if isinstance(row, dict)]
        if not rows:
            return [], [f"stockapi:{endpoint}:empty_data"]
        return rows, []

    def get_stockapi_limit_up_pool(self, date: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
        """Return StockAPI limit-up pool with reasons and sealing strength."""
        trade_date = _safe_str(date) or _stockapi_default_completed_date()
        effective_limit = max(1, min(int(limit or 30), 100))
        rows, errors = self._stockapi_data_rows("/v1/base/ZTPool", {"date": trade_date})

        items: List[Dict[str, Any]] = []
        for row in rows[:effective_limit]:
            items.append({
                "code": _safe_str(row.get("code")),
                "name": _safe_str(row.get("name")),
                "change_ratio": _safe_float(row.get("changeRatio")),
                "last_price": _safe_float(row.get("lastPrice")),
                "amount": _safe_float(row.get("amount")),
                "turnover_ratio": _safe_float(row.get("turnoverRatio")),
                "ceiling_amount": _safe_float(row.get("ceilingAmount")),
                "first_ceiling_time": _safe_str(row.get("firstCeilingTime")),
                "last_ceiling_time": _safe_str(row.get("lastCeilingTime")),
                "bomb_num": _safe_float(row.get("bombNum")),
                "limit_up_streak": _safe_float(row.get("lbNum")),
                "industry": _safe_str(row.get("industry")),
                "concepts": _safe_str(row.get("gl")),
                "stock_reason": _safe_str(row.get("stock_reason")),
                "plate_reason": _safe_str(row.get("plate_reason")),
                "plate_name": _safe_str(row.get("plate_name")),
                "time": _safe_str(row.get("time")) or trade_date,
            })

        return {
            "status": "partial" if items else "failed",
            "date": trade_date,
            "items": items,
            "source_chain": ["stockapi:ZTPool"] if items else [],
            "errors": errors,
        }

    def get_stockapi_hot_sectors(
        self,
        date: Optional[str] = None,
        limit: int = 20,
        *,
        allow_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Return StockAPI hot sectors ranked by capital inflow / strength."""
        trade_date = _safe_str(date) or _stockapi_default_completed_date()
        effective_limit = max(1, min(int(limit or 20), 100))
        rows, errors = self._stockapi_data_rows("/v1/hotBkJlrDr", {"date": trade_date})

        sectors: List[Dict[str, Any]] = []
        for row in rows[:effective_limit]:
            sectors.append({
                "id": _safe_str(row.get("id")),
                "bk_code": _safe_str(row.get("bkCode")),
                "bk_name": _safe_str(row.get("bkName")),
                "return_pct": _safe_float(row.get("qjzf")),
                "return_diff": _safe_float(row.get("diffQjzf")),
                "net_inflow": _safe_float(row.get("qjje")),
                "net_inflow_diff": _safe_float(row.get("diffQjje")),
                "inflow_days": _safe_float(row.get("jlrts")),
                "strength": _safe_float(row.get("qiangdu")),
                "strength_diff": _safe_float(row.get("diffQiangdu")),
                "time": _safe_str(row.get("time")) or trade_date,
            })

        if not sectors and errors and allow_fallback:
            fallback_sectors, fallback_errors, fallback_source = self._fallback_hot_sectors(effective_limit)
            if fallback_sectors:
                return {
                    "status": "partial",
                    "date": trade_date,
                    "sectors": fallback_sectors,
                    "source_chain": [fallback_source],
                    "errors": errors,
                    "degraded": True,
                    "primary_source": "stockapi:hotBkJlrDr",
                    "fallback_source": fallback_source,
                }
            errors = [*errors, *fallback_errors]

        return {
            "status": "partial" if sectors else "failed",
            "date": trade_date,
            "sectors": sectors,
            "source_chain": ["stockapi:hotBkJlrDr"] if sectors else [],
            "errors": errors,
        }

    def _fallback_hot_sectors(self, limit: int) -> Tuple[List[Dict[str, Any]], List[str], str]:
        """Fallback sector heat source when StockAPI hotBkJlrDr is unavailable."""
        try:
            import akshare as ak
        except Exception as exc:
            return [], [f"akshare:stock_board_industry_name_em:{type(exc).__name__}:{exc}"], "akshare:stock_board_industry_name_em"

        try:
            df = ak.stock_board_industry_name_em()
        except Exception as exc:
            return [], [f"akshare:stock_board_industry_name_em:{type(exc).__name__}:{exc}"], "akshare:stock_board_industry_name_em"
        if df is None or getattr(df, "empty", True):
            return [], ["akshare:stock_board_industry_name_em:empty_data"], "akshare:stock_board_industry_name_em"

        name_col = _first_column_by_keywords(df, ["板块名称", "名称", "板块", "行业", "name"])
        code_col = _first_column_by_keywords(df, ["板块代码", "代码", "code"])
        change_col = _first_column_by_keywords(df, ["涨跌幅", "涨幅", "change", "pct"])
        amount_col = _first_column_by_keywords(df, ["成交额", "金额", "amount"])
        turnover_col = _first_column_by_keywords(df, ["换手率", "turnover"])
        if name_col is None:
            return [], ["akshare:stock_board_industry_name_em:missing_name_column"], "akshare:stock_board_industry_name_em"

        rows: List[Dict[str, Any]] = []
        for _, row in df.head(max(1, limit)).iterrows():
            if not isinstance(row, pd.Series):
                continue
            rows.append({
                "id": None,
                "bk_code": _safe_str(row.get(code_col)) if code_col else "",
                "bk_name": _safe_str(row.get(name_col)),
                "return_pct": _safe_float(row.get(change_col)) if change_col else None,
                "return_diff": None,
                "net_inflow": None,
                "net_inflow_diff": None,
                "inflow_days": None,
                "strength": None,
                "strength_diff": None,
                "turnover_rate": _safe_float(row.get(turnover_col)) if turnover_col else None,
                "amount": _safe_float(row.get(amount_col)) if amount_col else None,
                "time": _stockapi_default_completed_date(),
                "source": "akshare:stock_board_industry_name_em",
                "fallback_reason": "StockAPI hotBkJlrDr unavailable; using sector performance ranking without StockAPI fund-flow fields.",
            })
        return rows, [], "akshare:stock_board_industry_name_em"

    def get_stockapi_sector_constituents(
        self,
        bk_code: str,
        page_no: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Return StockAPI sector/concept constituents with per-stock money-flow fields."""
        code = _safe_str(bk_code)
        if not code:
            return {
                "status": "failed",
                "bk_code": code,
                "items": [],
                "source_chain": [],
                "errors": ["stockapi:bkList:missing_bk_code"],
            }
        effective_page_no = max(1, int(page_no or 1))
        effective_page_size = max(1, min(int(page_size or 50), 100))
        rows, errors = self._stockapi_data_rows(
            "/v1/base/bkList",
            {"bkCode": code, "pageNo": effective_page_no, "pageSize": effective_page_size},
        )

        items: List[Dict[str, Any]] = []
        for row in rows:
            items.append({
                "code": _safe_str(row.get("f12")),
                "name": _safe_str(row.get("f14")),
                "last_price": _safe_float(row.get("f2")),
                "change_ratio": _safe_float(row.get("f3")),
                "main_net_inflow": _safe_float(row.get("f62")),
                "main_net_inflow_pct": _safe_float(row.get("f184")),
                "super_large_net_inflow": _safe_float(row.get("f66")),
                "super_large_net_inflow_pct": _safe_float(row.get("f69")),
                "large_net_inflow": _safe_float(row.get("f72")),
                "large_net_inflow_pct": _safe_float(row.get("f75")),
                "medium_net_inflow": _safe_float(row.get("f78")),
                "medium_net_inflow_pct": _safe_float(row.get("f81")),
                "small_net_inflow": _safe_float(row.get("f84")),
                "small_net_inflow_pct": _safe_float(row.get("f87")),
            })

        return {
            "status": "partial" if items else "failed",
            "bk_code": code,
            "page_no": effective_page_no,
            "page_size": effective_page_size,
            "items": items,
            "source_chain": ["stockapi:bkList"] if items else [],
            "errors": errors,
        }

    def get_stockapi_sector_flow_history(self, bk_code: str, limit: int = 10) -> Dict[str, Any]:
        """Return StockAPI sector/concept historical capital flow."""
        code = _safe_str(bk_code)
        if not code:
            return {
                "status": "failed",
                "bk_code": code,
                "history": [],
                "source_chain": [],
                "errors": ["stockapi:bkFlowHistory:missing_bk_code"],
            }
        effective_limit = max(1, min(int(limit or 10), 60))
        rows, errors = self._stockapi_data_rows("/v1/base/bkFlowHistory", {"bkCode": code})

        history: List[Dict[str, Any]] = []
        for row in rows[-effective_limit:]:
            history.append({
                "date": _safe_str(row.get("time")),
                "main_amount": _safe_float(row.get("mainAmount")),
                "main_amount_pct": _safe_float(row.get("mainAmountPercentage")),
                "super_big_amount": _safe_float(row.get("supperBigAmount")),
                "super_big_amount_pct": _safe_float(row.get("supperBigAmountPercentage")),
                "big_amount": _safe_float(row.get("bigAmount")),
                "big_amount_pct": _safe_float(row.get("bigAmountPercentage")),
                "middle_amount": _safe_float(row.get("middleAmount")),
                "middle_amount_pct": _safe_float(row.get("middleAmountPercentage")),
                "small_amount": _safe_float(row.get("minAmount")),
                "small_amount_pct": _safe_float(row.get("minAmountPercentage")),
            })

        return {
            "status": "partial" if history else "failed",
            "bk_code": code,
            "history": history,
            "source_chain": ["stockapi:bkFlowHistory"] if history else [],
            "errors": errors,
        }

    def get_stockapi_hot_sector_leaders(
        self,
        date: Optional[str] = None,
        bk_code: Optional[str] = None,
        limit: int = 30,
    ) -> Dict[str, Any]:
        """Return StockAPI hot-sector leader stocks.

        StockAPI documents this as the hot-sector leader endpoint
        (``/v1/hotBkJlrLongTou``).  The endpoint may require package access in
        some accounts; keep the response fail-open so callers can still use
        ``hotBkJlrDr`` sector diagnostics when leaders are unavailable.
        """
        trade_date = _safe_str(date) or _stockapi_default_completed_date()
        effective_limit = max(1, min(int(limit or 30), 100))
        params: Dict[str, Any] = {"date": trade_date}
        if _safe_str(bk_code):
            params["bkCode"] = _safe_str(bk_code)
        rows, errors = self._stockapi_data_rows("/v1/hotBkJlrLongTou", params)

        items: List[Dict[str, Any]] = []
        for row in rows[:effective_limit]:
            code = _safe_str(
                row.get("code")
                or row.get("stockCode")
                or row.get("f12")
                or row.get("tsCode")
            )
            name = _safe_str(
                row.get("name")
                or row.get("stockName")
                or row.get("f14")
            )
            items.append({
                "code": _normalize_code(code),
                "name": name,
                "bk_code": _safe_str(row.get("bkCode") or row.get("bk_code") or bk_code),
                "bk_name": _safe_str(row.get("bkName") or row.get("bk_name") or row.get("plateName")),
                "rank": _safe_float(row.get("rank") or row.get("order") or row.get("pm")),
                "change_ratio": _safe_float(row.get("zf") or row.get("changeRatio") or row.get("f3")),
                "last_price": _safe_float(row.get("lastPrice") or row.get("close") or row.get("f2")),
                "main_net_inflow": _safe_float(row.get("mainAmount") or row.get("main_net_inflow") or row.get("f62")),
                "net_inflow": _safe_float(row.get("qjje") or row.get("netInflow") or row.get("net_inflow")),
                "strength": _safe_float(row.get("qiangdu") or row.get("strength")),
                "reason": _safe_str(row.get("reason") or row.get("analyse") or row.get("stock_reason")),
                "time": _safe_str(row.get("time")) or trade_date,
                "raw": row,
            })

        return {
            "status": "partial" if items else "failed",
            "date": trade_date,
            "bk_code": _safe_str(bk_code),
            "items": [item for item in items if item.get("code")],
            "source_chain": ["stockapi:hotBkJlrLongTou"] if items else [],
            "errors": errors,
        }

    def get_stockapi_change_all_history(
        self,
        date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Return StockAPI all-market historical intraday change events."""
        trade_date = _safe_str(date) or _stockapi_default_completed_date()
        start = _safe_str(start_date) or trade_date
        end = _safe_str(end_date) or start
        effective_limit = max(1, min(int(limit or 50), 300))
        params: Dict[str, Any] = {"startDate": start, "endDate": end}
        event_type_value = _safe_str(event_type)
        if event_type_value:
            params["type"] = event_type_value
        rows, errors = self._stockapi_data_rows("/v1/change/allHistory", params)

        items: List[Dict[str, Any]] = []
        for row in rows[:effective_limit]:
            code = _normalize_code(row.get("code"))
            if not code:
                continue
            items.append({
                "code": code,
                "name": _safe_str(row.get("name")),
                "event_type": _safe_str(row.get("type")),
                "event_name": _safe_str(row.get("typeName") or row.get("type_name")),
                "info": _safe_str(row.get("info")),
                "time": _safe_str(row.get("time")),
                "date": _safe_str(row.get("dateId") or row.get("date") or start),
                "source": "stockapi:allHistory",
            })

        return {
            "status": "partial" if items else "failed",
            "date": trade_date,
            "start_date": start,
            "end_date": end,
            "event_type": event_type_value,
            "items": items,
            "source_chain": ["stockapi:allHistory"] if items else [],
            "errors": errors,
        }

    def get_stockapi_popularity_rank(self, limit: int = 30) -> Dict[str, Any]:
        """Return StockAPI stock popularity ranking with AI-generated reasons."""
        effective_limit = max(1, min(int(limit or 30), 100))
        rows, errors = self._stockapi_data_rows(
            "/v1/change/renQi",
            {},
            use_cache=True,
            cache_ttl_s=30.0,
        )

        items: List[Dict[str, Any]] = []
        for row in rows[:effective_limit]:
            items.append({
                "code": _safe_str(row.get("code")),
                "name": _safe_str(row.get("name")),
                "rank": _safe_float(row.get("order")),
                "popularity": _safe_float(row.get("rate")),
                "change_ratio": _safe_float(row.get("zf") if row.get("zf") is not None else row.get("rise_and_fall")),
                "reason": _safe_str(row.get("analyse")),
                "tag": row.get("tag") if isinstance(row.get("tag"), (dict, list)) else _safe_str(row.get("tag")),
            })

        return {
            "status": "partial" if items else "failed",
            "items": items,
            "source_chain": ["stockapi:renQi"] if items else [],
            "errors": errors,
        }

    def get_stockapi_hot_money_activity(
        self,
        stock_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 30,
    ) -> Dict[str, Any]:
        """Return StockAPI hot-money activity, per stock or rank by brokerage seat."""
        effective_limit = max(1, min(int(limit or 30), 100))
        end = _safe_str(end_date) or datetime.now().date().isoformat()
        start = _safe_str(start_date) or (datetime.now().date() - timedelta(days=30)).isoformat()
        code = _normalize_code(stock_code) if stock_code else ""

        if code:
            rows, errors = self._stockapi_data_rows(
                "/v1/youzi/gegu",
                {"code": code, "startDate": start, "endDate": end},
            )
            items: List[Dict[str, Any]] = []
            for row in rows[:effective_limit]:
                items.append({
                    "date": _safe_str(row.get("rq")),
                    "code": _safe_str(row.get("gpdm")),
                    "name": _safe_str(row.get("gpmc")),
                    "hot_money_name": _safe_str(row.get("yzmc")),
                    "broker_seat": _safe_str(row.get("yyb")),
                    "buy_amount": _safe_float(row.get("mrje")),
                    "sell_amount": _safe_float(row.get("mcje")),
                    "net_inflow": _safe_float(row.get("jlrje")),
                    "list_type": _safe_str(row.get("sblx")),
                    "concepts": _safe_str(row.get("gl")),
                })
            source = "stockapi:youzi/gegu"
            return {
                "status": "partial" if items else "failed",
                "mode": "stock",
                "stock_code": code,
                "start_date": start,
                "end_date": end,
                "items": items,
                "source_chain": [source] if items else [],
                "errors": errors,
            }

        rows, errors = self._stockapi_data_rows("/v1/youziRank", {"startDate": start, "endDate": end})
        items = []
        for row in rows[:effective_limit]:
            items.append({
                "hot_money_name": _safe_str(row.get("groupIcon")),
                "broker_seat": _safe_str(row.get("yybName")),
                "list_count": _safe_float(row.get("num")),
            })
        source = "stockapi:youziRank"
        if not items and errors:
            fallback_items, fallback_errors, fallback_source = self._fallback_hot_money_rank(start, end, effective_limit)
            if fallback_items:
                return {
                    "status": "partial",
                    "mode": "rank",
                    "start_date": start,
                    "end_date": end,
                    "items": fallback_items,
                    "source_chain": [fallback_source],
                    "errors": errors,
                    "degraded": True,
                    "primary_source": source,
                    "fallback_source": fallback_source,
                    "proxy_type": "dragon_tiger_stock_list",
                }
            errors = [*errors, *fallback_errors]
        return {
            "status": "partial" if items else "failed",
            "mode": "rank",
            "start_date": start,
            "end_date": end,
            "items": items,
            "source_chain": [source] if items else [],
            "errors": errors,
        }

    def _fallback_hot_money_rank(self, start_date: str, end_date: str, limit: int) -> Tuple[List[Dict[str, Any]], List[str], str]:
        """Fallback to dragon-tiger stock list when StockAPI hot-money rank is unavailable."""
        candidates = [
            ("stock_lhb_detail_em", {"start_date": start_date.replace("-", ""), "end_date": end_date.replace("-", "")}),
            ("stock_lhb_detail_em", {"start_date": start_date, "end_date": end_date}),
            ("stock_lhb_stock_statistic_em", {}),
        ]
        df, source, errors = self._call_df_candidates(candidates)
        fallback_source = f"akshare:{source or 'dragon_tiger'}"
        if df is None or getattr(df, "empty", True):
            return [], errors or [f"{fallback_source}:empty_data"], fallback_source

        code_col = _first_column_by_keywords(df, ["代码", "股票代码", "证券代码", "ts_code", "code"])
        name_col = _first_column_by_keywords(df, ["名称", "股票简称", "股票名称", "name"])
        date_col = _first_column_by_keywords(df, ["上榜日", "交易日", "日期", "date"])
        reason_col = _first_column_by_keywords(df, ["上榜原因", "解读", "原因", "类型"])
        net_col = _first_column_by_keywords(df, ["龙虎榜净买额", "净买额", "净额", "net"])
        buy_col = _first_column_by_keywords(df, ["龙虎榜买入额", "买入额", "买入", "buy"])
        sell_col = _first_column_by_keywords(df, ["龙虎榜卖出额", "卖出额", "卖出", "sell"])
        amount_col = _first_column_by_keywords(df, ["龙虎榜成交额", "成交额", "amount"])
        if code_col is None and name_col is None:
            return [], [*errors, f"{fallback_source}:missing_code_name_columns"], fallback_source

        items: List[Dict[str, Any]] = []
        for _, row in df.head(max(1, limit)).iterrows():
            if not isinstance(row, pd.Series):
                continue
            items.append({
                "date": _safe_str(row.get(date_col)) if date_col else "",
                "code": _normalize_code(row.get(code_col)) if code_col else "",
                "name": _safe_str(row.get(name_col)) if name_col else "",
                "hot_money_name": "",
                "broker_seat": "",
                "list_count": None,
                "reason": _safe_str(row.get(reason_col)) if reason_col else "",
                "net_inflow": _safe_float(row.get(net_col)) if net_col else None,
                "buy_amount": _safe_float(row.get(buy_col)) if buy_col else None,
                "sell_amount": _safe_float(row.get(sell_col)) if sell_col else None,
                "amount": _safe_float(row.get(amount_col)) if amount_col else None,
                "source": fallback_source,
                "proxy_type": "dragon_tiger_stock_list",
                "fallback_reason": "StockAPI youziRank unavailable; using dragon-tiger listed stocks as hot-money activity proxy without brokerage-seat ranking.",
            })
        return items, errors, fallback_source

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
        stop_error_keywords: Optional[List[str]] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                message = str(exc).strip().replace("\n", " ")
                if len(message) > 220:
                    message = message[:217] + "..."
                error_text = f"{func_name}:{type(exc).__name__}:{message}"
                errors.append(error_text)
                if stop_error_keywords and any(keyword in error_text for keyword in stop_error_keywords):
                    break
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_market_capital_flow(self, top_n: int = 5) -> Dict[str, Any]:
        """Return market/sector/industry capital flow snapshots."""
        effective_top_n = max(1, min(int(top_n or 5), 20))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "market_flow": {},
            "individual_rankings": {"top": [], "bottom": []},
            "industry_rankings": {"top": [], "bottom": []},
            "concept_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        market_df, market_source, market_errors = self._call_df_candidates([
            ("stock_market_fund_flow", {}),
        ])
        result["errors"].extend(market_errors)
        if market_df is not None and not market_df.empty:
            row = market_df.iloc[0]
            result["market_flow"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "main_net_inflow": ["主力净流入", "主力净额", "净流入", "净额"],
                        "super_large_net_inflow": ["超大单净流入", "超大单净额"],
                        "large_net_inflow": ["大单净流入", "大单净额"],
                        "medium_net_inflow": ["中单净流入", "中单净额"],
                        "small_net_inflow": ["小单净流入", "小单净额"],
                    },
                ),
            }
            result["source_chain"].append(f"market_flow:{market_source}")

        for key, candidates in {
            "individual_rankings": [
                ("stock_fund_flow_individual", {"symbol": "即时"}),
                ("stock_individual_fund_flow_rank", {"indicator": "今日"}),
                ("stock_individual_fund_flow_rank", {"indicator": "5日"}),
            ],
            "industry_rankings": [
                ("stock_fund_flow_industry", {"symbol": "即时"}),
                ("stock_sector_fund_flow_rank", {}),
                ("stock_sector_fund_flow_summary", {}),
            ],
            "concept_rankings": [
                ("stock_fund_flow_concept", {"symbol": "即时"}),
                ("stock_concept_fund_flow_hist", {}),
            ],
        }.items():
            df, source, errors = self._call_df_candidates(candidates)
            result["errors"].extend(errors)
            rankings = self._rank_flow_dataframe(df, effective_top_n)
            if rankings["top"] or rankings["bottom"]:
                result[key] = rankings
                result["source_chain"].append(f"{key}:{source}")

        has_content = bool(
            any(v is not None for v in result["market_flow"].values())
            or any(result[key]["top"] or result[key]["bottom"] for key in ("individual_rankings", "industry_rankings", "concept_rankings"))
        )
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_northbound_capital_flow(self, limit: int = 10) -> Dict[str, Any]:
        """Return northbound / Stock Connect capital-flow summary."""
        effective_limit = max(1, min(int(limit or 10), 60))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "summary": {},
            "history": [],
            "data_quality": {
                "summary_numeric_fields": 0,
                "history_rows": 0,
                "history_rows_with_numeric": 0,
                "core_numeric_available": False,
            },
            "source_chain": [],
            "errors": [],
            "warnings": [],
        }

        summary_df, summary_source, summary_errors = self._call_df_candidates([
            ("stock_hsgt_fund_flow_summary_em", {}),
        ])
        result["errors"].extend(summary_errors)
        if summary_df is not None and not summary_df.empty:
            row = summary_df.iloc[0]
            result["summary"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "northbound_net_inflow": ["北向资金", "北上资金", "沪股通", "深股通", "净流入", "净买入"],
                        "southbound_net_inflow": ["南向资金", "港股通"],
                        "buy_amount": ["买入", "流入"],
                        "sell_amount": ["卖出", "流出"],
                    },
                ),
            }
            result["data_quality"]["summary_numeric_fields"] = sum(
                1
                for key in ("northbound_net_inflow", "southbound_net_inflow", "buy_amount", "sell_amount")
                if result["summary"].get(key) is not None
            )
            result["source_chain"].append(f"northbound_summary:{summary_source}")

        hist_df, hist_source, hist_errors = self._call_df_candidates([
            ("stock_hsgt_hist_em", {"symbol": "北向资金"}),
        ])
        result["errors"].extend(hist_errors)
        if hist_df is not None and not hist_df.empty:
            rows = hist_df.tail(effective_limit).to_dict(orient="records")
            result["history"] = [self._compact_northbound_history_row(row) for row in rows]
            result["data_quality"]["history_rows"] = len(result["history"])
            result["data_quality"]["history_rows_with_numeric"] = sum(
                1
                for row in result["history"]
                if any(row.get(key) is not None for key in ("net_inflow", "buy_amount", "sell_amount", "balance"))
            )
            result["source_chain"].append(f"northbound_history:{hist_source}")

        has_content = bool(result["summary"] or result["history"])
        result["data_quality"]["core_numeric_available"] = bool(
            result["data_quality"]["summary_numeric_fields"]
            or result["data_quality"]["history_rows_with_numeric"]
        )
        if has_content and not result["data_quality"]["core_numeric_available"]:
            result["warnings"].append(
                "northbound_source_returned_rows_but_no_numeric_flow_fields"
            )
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_margin_trading_summary(self, limit: int = 10) -> Dict[str, Any]:
        """Return margin financing/securities-lending summary for A-share markets."""
        effective_limit = max(1, min(int(limit or 10), 60))
        result: Dict[str, Any] = {
            "status": "not_supported",
            "account_info": {},
            "sse": [],
            "szse": [],
            "source_chain": [],
            "errors": [],
        }

        account_df, account_source, account_errors = self._call_df_candidates([
            ("stock_margin_account_info", {}),
        ])
        result["errors"].extend(account_errors)
        if account_df is not None and not account_df.empty:
            row = account_df.iloc[0]
            result["account_info"] = {
                "date": _row_date_value(row),
                **_compact_numeric_row(
                    row,
                    {
                        "financing_balance": ["融资余额"],
                        "securities_lending_balance": ["融券余额"],
                        "margin_balance": ["两融余额", "融资融券余额"],
                        "financing_buy_amount": ["融资买入额"],
                        "financing_repayment_amount": ["融资偿还额"],
                    },
                ),
            }
            result["source_chain"].append(f"margin_account:{account_source}")

        now = datetime.now()
        start_date = (now - timedelta(days=effective_limit * 3)).strftime("%Y%m%d")
        end_date = now.strftime("%Y%m%d")
        sse_df, sse_source, sse_errors = self._call_df_candidates([
            ("stock_margin_sse", {"start_date": start_date, "end_date": end_date}),
        ])
        result["errors"].extend(sse_errors)
        if sse_df is not None and not sse_df.empty:
            result["sse"] = [
                self._compact_margin_row(row)
                for row in sse_df.tail(effective_limit).to_dict(orient="records")
            ]
            result["source_chain"].append(f"margin_sse:{sse_source}")

        szse_df, szse_source, szse_errors = self._call_df_candidates([
            ("stock_margin_szse", {"date": end_date}),
        ])
        result["errors"].extend(szse_errors)
        if szse_df is not None and not szse_df.empty:
            result["szse"] = [
                self._compact_margin_row(row)
                for row in szse_df.tail(effective_limit).to_dict(orient="records")
            ]
            result["source_chain"].append(f"margin_szse:{szse_source}")

        has_content = bool(result["account_info"] or result["sse"] or result["szse"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _rank_flow_dataframe(self, df: Optional[pd.DataFrame], top_n: int) -> Dict[str, List[Dict[str, Any]]]:
        rankings: Dict[str, List[Dict[str, Any]]] = {"top": [], "bottom": []}
        if df is None or df.empty:
            return rankings
        name_col = _first_column_by_keywords(df, ["名称", "板块", "行业", "股票", "代码", "name"])
        flow_col = _first_column_by_keywords(df, ["主力净流入", "净流入", "净额", "资金流入", "flow"])
        if name_col is None or flow_col is None:
            return rankings
        work_df = df[[name_col, flow_col]].copy()
        work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
        work_df = work_df.dropna(subset=[flow_col])
        if work_df.empty:
            return rankings
        rankings["top"] = [
            {"name": _safe_str(row[name_col]), "net_inflow": float(row[flow_col])}
            for _, row in work_df.nlargest(top_n, flow_col).iterrows()
        ]
        rankings["bottom"] = [
            {"name": _safe_str(row[name_col]), "net_inflow": float(row[flow_col])}
            for _, row in work_df.nsmallest(top_n, flow_col).iterrows()
        ]
        return rankings

    def _compact_northbound_history_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        series = pd.Series(row)
        return {
            "date": _row_date_value(series),
            **_compact_numeric_row(
                series,
                {
                    "net_inflow": ["净流入", "净买入", "当日成交净买额"],
                    "buy_amount": ["买入", "流入"],
                    "sell_amount": ["卖出", "流出"],
                    "balance": ["余额"],
                },
            ),
        }

    def _compact_margin_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        series = pd.Series(row)
        return {
            "date": _row_date_value(series),
            **_compact_numeric_row(
                series,
                {
                    "financing_balance": ["融资余额"],
                    "financing_buy_amount": ["融资买入额"],
                    "financing_repayment_amount": ["融资偿还额"],
                    "securities_lending_balance": ["融券余额"],
                    "margin_balance": ["融资融券余额", "两融余额"],
                },
            ),
        }

    def get_capital_flow(
        self,
        stock_code: str,
        top_n: int = 5,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page_no: int = 1,
        page_size: int = 50,
        budget_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Return stock capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        source_getters: Tuple[
            Tuple[str, Callable[..., Tuple[Dict[str, Any], Optional[str], List[str]]]],
            ...,
        ] = (
            ("tushare_moneyflow_dc", self._get_tushare_moneyflow_dc_capital_flow),
            ("tushare_moneyflow_ths", self._get_tushare_moneyflow_ths_capital_flow),
            ("tushare_moneyflow", self._get_tushare_capital_flow),
        )
        if budget_seconds is None:
            tushare_hit = self._get_capital_flow_tushare_serial(
                source_getters,
                stock_code,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            tushare_hit = self._get_capital_flow_tushare_budgeted(
                source_getters,
                stock_code,
                start_date=start_date,
                end_date=end_date,
                budget_seconds=budget_seconds,
            )
        result["errors"].extend(tushare_hit.get("errors", []))
        result["source_chain"].extend(tushare_hit.get("source_chain", []))
        selected_label = tushare_hit.get("selected_label")
        flow = tushare_hit.get("flow")
        if selected_label and isinstance(flow, dict) and flow:
            stock_flow = dict(flow)
            stock_flow["selected_flow_source"] = selected_label
            stock_flow["flow_sources"] = {selected_label: flow}
            result["stock_flow"] = stock_flow
            result["status"] = "partial"
            return result

        stockapi_flow, stockapi_source, stockapi_errors = self._get_stockapi_capital_flow(
            stock_code,
            start_date=start_date,
            end_date=end_date,
            page_no=page_no,
            page_size=page_size,
        )
        if stockapi_flow:
            result["stock_flow"] = stockapi_flow
            result["source_chain"].append(f"capital_stock:{stockapi_source}")
            result["status"] = "partial"
            return result

        result["errors"].extend(stockapi_errors)
        if stockapi_errors:
            result["source_chain"].append("capital_stock:stockapi_codeFlow")
        if any(":not_supported:" in str(error) for error in stockapi_errors):
            result["status"] = "not_supported"
        elif result["errors"]:
            result["status"] = "failed"
        return result

    def _get_capital_flow_tushare_serial(
        self,
        source_getters: Tuple[
            Tuple[str, Callable[..., Tuple[Dict[str, Any], Optional[str], List[str]]]],
            ...,
        ],
        stock_code: str,
        *,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "selected_label": None,
            "flow": {},
            "source_chain": [],
            "errors": [],
        }
        for label, getter in source_getters:
            flow, source, errors = getter(
                stock_code,
                start_date=start_date,
                end_date=end_date,
            )
            if flow:
                result["selected_label"] = label
                result["flow"] = flow
                result["source_chain"].append(f"capital_stock:{source or label}")
                return result
            result["errors"].extend(errors)
            if errors:
                result["source_chain"].append(f"capital_stock:{label}")
        return result

    def _get_capital_flow_tushare_budgeted(
        self,
        source_getters: Tuple[
            Tuple[str, Callable[..., Tuple[Dict[str, Any], Optional[str], List[str]]]],
            ...,
        ],
        stock_code: str,
        *,
        start_date: Optional[str],
        end_date: Optional[str],
        budget_seconds: float,
    ) -> Dict[str, Any]:
        total_budget = max(1.0, float(budget_seconds or 0.0))
        stockapi_reserve = 3.0 if total_budget >= 6.0 else 0.0
        tushare_budget = max(0.75, min(8.0, total_budget - stockapi_reserve))
        query_timeout = max(1.0, min(8.0, tushare_budget))
        started_at = time.monotonic()
        completed: Dict[str, Tuple[Dict[str, Any], Optional[str], List[str]]] = {}
        result: Dict[str, Any] = {
            "selected_label": None,
            "flow": {},
            "source_chain": [],
            "errors": [],
        }
        futures: Dict[concurrent.futures.Future, str] = {}
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(source_getters),
            thread_name_prefix="capital-flow-tushare",
        )
        try:
            for label, getter in source_getters:
                futures[
                    executor.submit(
                        getter,
                        stock_code,
                        start_date=start_date,
                        end_date=end_date,
                        query_timeout=query_timeout,
                    )
                ] = label

            pending = set(futures.keys())
            deadline = started_at + tushare_budget
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=min(0.1, max(0.01, remaining)),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    label = futures[future]
                    try:
                        flow, source, errors = future.result()
                    except Exception as exc:
                        flow, source, errors = {}, None, [f"{label}:{type(exc).__name__}:{exc}"]
                    completed[label] = (flow or {}, source, list(errors or []))

                selected = self._select_completed_capital_flow(source_getters, completed)
                if selected:
                    selected_label, selected_flow, selected_source = selected
                    result["selected_label"] = selected_label
                    result["flow"] = selected_flow
                    for label, _getter in source_getters:
                        if label == selected_label:
                            break
                        _flow, _source, errors = completed.get(label, ({}, None, []))
                        result["errors"].extend(errors)
                        if errors:
                            result["source_chain"].append(f"capital_stock:{label}")
                    result["source_chain"].append(f"capital_stock:{selected_source or selected_label}")
                    return result

            for future in list(pending):
                label = futures[future]
                future.cancel()
                completed.setdefault(label, ({}, None, [f"{label}:timeout:{tushare_budget:.1f}s"]))

            selected = self._select_completed_capital_flow(
                source_getters,
                completed,
                allow_pending_predecessors=True,
            )
            if selected:
                selected_label, selected_flow, selected_source = selected
                result["selected_label"] = selected_label
                result["flow"] = selected_flow
                for label, _getter in source_getters:
                    if label == selected_label:
                        break
                    _flow, _source, errors = completed.get(label, ({}, None, []))
                    result["errors"].extend(errors)
                    if errors:
                        result["source_chain"].append(f"capital_stock:{label}")
                result["source_chain"].append(f"capital_stock:{selected_source or selected_label}")
                return result

            for label, _getter in source_getters:
                _flow, _source, errors = completed.get(label, ({}, None, [f"{label}:timeout:{tushare_budget:.1f}s"]))
                result["errors"].extend(errors)
                if errors:
                    result["source_chain"].append(f"capital_stock:{label}")
            return result
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _select_completed_capital_flow(
        source_getters: Tuple[
            Tuple[str, Callable[..., Tuple[Dict[str, Any], Optional[str], List[str]]]],
            ...,
        ],
        completed: Dict[str, Tuple[Dict[str, Any], Optional[str], List[str]]],
        *,
        allow_pending_predecessors: bool = False,
    ) -> Optional[Tuple[str, Dict[str, Any], Optional[str]]]:
        for label, _getter in source_getters:
            if label not in completed:
                if allow_pending_predecessors:
                    continue
                return None
            flow, source, _errors = completed[label]
            if flow:
                return label, flow, source
        return None

    def audit_capital_flow_sources(
        self,
        stock_code: str,
        selected_source: Optional[str],
        selected_flow: Dict[str, Any],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Best-effort compare non-selected Tushare capital-flow sources."""
        selected_label = _safe_str(selected_source) or "unknown"
        result: Dict[str, Any] = {
            "status": "no_comparison",
            "selected_flow_source": selected_label,
            "checked_sources": [],
            "source_summaries": {
                selected_label: _capital_flow_source_summary(selected_flow)
                if isinstance(selected_flow, dict) else {}
            },
            "source_conflicts": [],
            "warnings": [],
            "source_chain": [],
            "errors": [],
        }
        if not isinstance(selected_flow, dict) or not _has_capital_flow_value(selected_flow):
            result["status"] = "not_applicable"
            result["errors"].append("capital_flow_audit:no_selected_flow")
            return result

        getter_by_source = {
            "tushare_moneyflow_dc": self._get_tushare_moneyflow_dc_capital_flow,
            "tushare_moneyflow_ths": self._get_tushare_moneyflow_ths_capital_flow,
            "tushare_moneyflow": self._get_tushare_capital_flow,
        }
        for label in _CAPITAL_FLOW_AUDIT_SOURCES:
            if label == selected_label:
                continue
            getter = getter_by_source[label]
            flow, source, errors = getter(
                stock_code,
                start_date=start_date,
                end_date=end_date,
            )
            if flow:
                result["checked_sources"].append(label)
                result["source_chain"].append(f"capital_flow_audit:{source or label}")
                result["source_summaries"][label] = _capital_flow_source_summary(flow)
                result["source_conflicts"].extend(
                    _compare_capital_flow_sources(
                        selected_label,
                        selected_flow,
                        label,
                        flow,
                    )
                )
            else:
                result["errors"].extend(errors)
                if errors:
                    result["source_chain"].append(f"capital_flow_audit:{label}")

        if result["source_conflicts"]:
            result["status"] = "conflict"
            result["warnings"].append(
                "capital_flow_source_conflict:"
                f"{len(result['source_conflicts'])} conflict(s); "
                f"keep selected_flow_source={selected_label} authoritative"
            )
        elif result["checked_sources"]:
            result["status"] = "ok"
        return result

    def _select_capital_flow(self, flow_sources: Dict[str, Dict[str, Any]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Pick one backward-compatible stock_flow while retaining all source payloads."""
        for source in ("tushare_moneyflow_dc", "tushare_moneyflow_ths", "tushare_moneyflow"):
            flow = flow_sources.get(source)
            if isinstance(flow, dict) and _has_capital_flow_value(flow):
                return source, dict(flow)
        return None

    def _tushare_capital_window(
        self,
        stock_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
        *,
        source_label: str,
    ) -> Tuple[Optional[str], Optional[date], Optional[date], List[str]]:
        code = _normalize_code(stock_code)
        if not re.fullmatch(r"\d{6}", code or ""):
            return None, None, None, [f"{source_label}:not_supported:{stock_code}"]

        try:
            explicit_start = _parse_stockapi_code_flow_date(start_date, "start_date")
            explicit_end = _parse_stockapi_code_flow_date(end_date, "end_date")
        except ValueError as exc:
            return code, None, None, [f"{source_label}:invalid_date:{exc}"]

        latest_queryable_date = _latest_completed_weekday()
        window_end = explicit_end or latest_queryable_date
        if explicit_start is not None:
            window_start = explicit_start
        elif explicit_end is not None:
            window_start = explicit_end - timedelta(days=20)
        else:
            window_start = latest_queryable_date - timedelta(days=20)
        if window_start > window_end:
            return code, None, None, [f"{source_label}:invalid_date:start_date_after_end_date"]
        return code, window_start, window_end, []

    def _query_tushare_capital_frame(
        self,
        api_name: str,
        stock_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
        fields: str,
        *,
        source_label: str,
        query_timeout: float = 10.0,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        code, window_start, window_end, errors = self._tushare_capital_window(
            stock_code,
            start_date,
            end_date,
            source_label=source_label,
        )
        if errors:
            return None, code, errors
        assert code is not None and window_start is not None and window_end is not None
        params = {
            "ts_code": _to_tushare_ts_code(code),
            "start_date": window_start.strftime("%Y%m%d"),
            "end_date": window_end.strftime("%Y%m%d"),
        }
        try:
            df = query_tushare_api(
                api_name,
                params=params,
                fields=fields,
                timeout=max(1, int(query_timeout)),
            )
        except Exception as exc:
            return None, code, [f"{source_label}:{type(exc).__name__}:{exc}"]
        if df is None or df.empty:
            return None, code, [f"{source_label}:empty_data"]
        return df, code, []

    def _get_tushare_moneyflow_dc_capital_flow(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        query_timeout: float = 10.0,
    ) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        source = "tushare_moneyflow_dc"
        fields = (
            "trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate,"
            "buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
            "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate"
        )
        df, _, errors = self._query_tushare_capital_frame(
            "moneyflow_dc",
            stock_code,
            start_date,
            end_date,
            fields,
            source_label=source,
            query_timeout=query_timeout,
        )
        if errors:
            return {}, None, errors
        assert df is not None

        main_by_date: Dict[str, float] = {}
        net_by_date: Dict[str, float] = {}
        raw_by_date: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            date_key = _date_key_from_tushare(row.get("trade_date"))
            if not date_key:
                continue
            net_amount = _safe_float(row.get("net_amount"))
            buy_elg = _safe_float(row.get("buy_elg_amount"))
            buy_lg = _safe_float(row.get("buy_lg_amount"))
            buy_md = _safe_float(row.get("buy_md_amount"))
            buy_sm = _safe_float(row.get("buy_sm_amount"))
            if net_amount is not None:
                main_by_date[date_key] = float(net_amount) * 10000.0
            if any(value is not None for value in (buy_elg, buy_lg)):
                net_by_date[date_key] = float((buy_elg or 0.0) + (buy_lg or 0.0)) * 10000.0
            raw_by_date[date_key] = {
                "ts_code": _safe_str(row.get("ts_code")),
                "name": _safe_str(row.get("name")),
                "pct_change": _safe_float(row.get("pct_change")),
                "close": _safe_float(row.get("close")),
                "net_amount_10k_cny": net_amount,
                "net_amount_rate": _safe_float(row.get("net_amount_rate")),
                "buy_elg_amount_10k_cny": buy_elg,
                "buy_elg_amount_rate": _safe_float(row.get("buy_elg_amount_rate")),
                "buy_lg_amount_10k_cny": buy_lg,
                "buy_lg_amount_rate": _safe_float(row.get("buy_lg_amount_rate")),
                "buy_md_amount_10k_cny": buy_md,
                "buy_md_amount_rate": _safe_float(row.get("buy_md_amount_rate")),
                "buy_sm_amount_10k_cny": buy_sm,
                "buy_sm_amount_rate": _safe_float(row.get("buy_sm_amount_rate")),
            }

        if not main_by_date:
            return {}, None, [f"{source}:no_main_amount"]
        latest_date = sorted(main_by_date)[-1]
        latest_raw = raw_by_date.get(latest_date, {})
        return {
            "main_net_inflow": _latest_value(main_by_date, latest_date),
            "main_inflow_5d": _sum_last_values(main_by_date, 5),
            "main_inflow_10d": _sum_last_values(main_by_date, 10),
            "net_inflow": _latest_value(net_by_date, latest_date),
            "net_inflow_5d": _sum_last_values(net_by_date, 5),
            "net_inflow_10d": _sum_last_values(net_by_date, 10),
            "inflow_5d": _sum_last_values(main_by_date, 5),
            "inflow_10d": _sum_last_values(main_by_date, 10),
            "latest_date": latest_date,
            "source_update": "tushare_moneyflow_dc_after_market_close",
            "amount_unit": "CNY",
            "raw_amount_unit": "10k CNY",
            "main_inflow_definition": "moneyflow_dc.net_amount * 10000 (Eastmoney main-force net inflow)",
            "net_inflow_definition": "(moneyflow_dc.buy_elg_amount + moneyflow_dc.buy_lg_amount) * 10000",
            "latest_price": latest_raw.get("close"),
            "pct_change": latest_raw.get("pct_change"),
            "net_inflow_rate": latest_raw.get("net_amount_rate"),
            "extra_large_net_inflow": (
                latest_raw.get("buy_elg_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_elg_amount_10k_cny") is not None else None
            ),
            "extra_large_net_inflow_rate": latest_raw.get("buy_elg_amount_rate"),
            "large_net_inflow": (
                latest_raw.get("buy_lg_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_lg_amount_10k_cny") is not None else None
            ),
            "large_net_inflow_rate": latest_raw.get("buy_lg_amount_rate"),
            "medium_net_inflow": (
                latest_raw.get("buy_md_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_md_amount_10k_cny") is not None else None
            ),
            "medium_net_inflow_rate": latest_raw.get("buy_md_amount_rate"),
            "small_net_inflow": (
                latest_raw.get("buy_sm_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_sm_amount_10k_cny") is not None else None
            ),
            "small_net_inflow_rate": latest_raw.get("buy_sm_amount_rate"),
            "source": "tushare:moneyflow_dc",
            "latest_raw": latest_raw,
        }, source, []

    def _get_tushare_moneyflow_ths_capital_flow(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        query_timeout: float = 10.0,
    ) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        source = "tushare_moneyflow_ths"
        fields = (
            "trade_date,ts_code,name,pct_change,latest,net_amount,net_d5_amount,"
            "buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,"
            "buy_sm_amount,buy_sm_amount_rate"
        )
        df, _, errors = self._query_tushare_capital_frame(
            "moneyflow_ths",
            stock_code,
            start_date,
            end_date,
            fields,
            source_label=source,
            query_timeout=query_timeout,
        )
        if errors:
            return {}, None, errors
        assert df is not None

        large_by_date: Dict[str, float] = {}
        net_by_date: Dict[str, float] = {}
        raw_by_date: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            date_key = _date_key_from_tushare(row.get("trade_date"))
            if not date_key:
                continue
            net_amount = _safe_float(row.get("net_amount"))
            net_d5_amount = _safe_float(row.get("net_d5_amount"))
            buy_lg = _safe_float(row.get("buy_lg_amount"))
            buy_md = _safe_float(row.get("buy_md_amount"))
            buy_sm = _safe_float(row.get("buy_sm_amount"))
            if buy_lg is not None:
                large_by_date[date_key] = float(buy_lg) * 10000.0
            if net_amount is not None:
                net_by_date[date_key] = float(net_amount) * 10000.0
            raw_by_date[date_key] = {
                "ts_code": _safe_str(row.get("ts_code")),
                "name": _safe_str(row.get("name")),
                "pct_change": _safe_float(row.get("pct_change")),
                "latest": _safe_float(row.get("latest")),
                "net_amount_10k_cny": net_amount,
                "net_d5_amount_10k_cny": net_d5_amount,
                "buy_lg_amount_10k_cny": buy_lg,
                "buy_lg_amount_rate": _safe_float(row.get("buy_lg_amount_rate")),
                "buy_md_amount_10k_cny": buy_md,
                "buy_md_amount_rate": _safe_float(row.get("buy_md_amount_rate")),
                "buy_sm_amount_10k_cny": buy_sm,
                "buy_sm_amount_rate": _safe_float(row.get("buy_sm_amount_rate")),
            }

        if not large_by_date and not net_by_date:
            return {}, None, [f"{source}:no_main_amount"]
        latest_date = sorted((large_by_date or net_by_date).keys())[-1]
        latest_raw = raw_by_date.get(latest_date, {})
        net_d5 = latest_raw.get("net_d5_amount_10k_cny")
        return {
            "main_net_inflow": _latest_value(large_by_date, latest_date),
            "main_inflow_5d": _sum_last_values(large_by_date, 5),
            "main_inflow_10d": _sum_last_values(large_by_date, 10),
            "net_inflow": _latest_value(net_by_date, latest_date),
            "net_inflow_5d": net_d5 * 10000.0 if net_d5 is not None else _sum_last_values(net_by_date, 5),
            "net_inflow_10d": _sum_last_values(net_by_date, 10),
            "inflow_5d": _sum_last_values(large_by_date, 5),
            "inflow_10d": _sum_last_values(large_by_date, 10),
            "latest_date": latest_date,
            "source_update": "tushare_moneyflow_ths_after_market_close",
            "amount_unit": "CNY",
            "raw_amount_unit": "10k CNY",
            "main_inflow_definition": "moneyflow_ths.buy_lg_amount * 10000 (THS large-order net inflow)",
            "net_inflow_definition": "moneyflow_ths.net_amount * 10000 (THS net inflow)",
            "latest_price": latest_raw.get("latest"),
            "pct_change": latest_raw.get("pct_change"),
            "large_net_inflow": (
                latest_raw.get("buy_lg_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_lg_amount_10k_cny") is not None else None
            ),
            "large_net_inflow_rate": latest_raw.get("buy_lg_amount_rate"),
            "medium_net_inflow": (
                latest_raw.get("buy_md_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_md_amount_10k_cny") is not None else None
            ),
            "medium_net_inflow_rate": latest_raw.get("buy_md_amount_rate"),
            "small_net_inflow": (
                latest_raw.get("buy_sm_amount_10k_cny") * 10000.0
                if latest_raw.get("buy_sm_amount_10k_cny") is not None else None
            ),
            "small_net_inflow_rate": latest_raw.get("buy_sm_amount_rate"),
            "source": "tushare:moneyflow_ths",
            "latest_raw": latest_raw,
        }, source, []

    def _get_tushare_capital_flow(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        query_timeout: float = 10.0,
    ) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        code = _normalize_code(stock_code)
        if not re.fullmatch(r"\d{6}", code or ""):
            return {}, None, [f"tushare_moneyflow:not_supported:{stock_code}"]

        try:
            explicit_start = _parse_stockapi_code_flow_date(start_date, "start_date")
            explicit_end = _parse_stockapi_code_flow_date(end_date, "end_date")
        except ValueError as exc:
            return {}, None, [f"tushare_moneyflow:invalid_date:{exc}"]

        latest_queryable_date = _latest_completed_weekday()
        window_end = explicit_end or latest_queryable_date
        if explicit_start is not None:
            window_start = explicit_start
        elif explicit_end is not None:
            window_start = explicit_end - timedelta(days=20)
        else:
            window_start = latest_queryable_date - timedelta(days=20)
        if window_start > window_end:
            return {}, None, ["tushare_moneyflow:invalid_date:start_date_after_end_date"]

        params = {
            "ts_code": _to_tushare_ts_code(code),
            "start_date": window_start.strftime("%Y%m%d"),
            "end_date": window_end.strftime("%Y%m%d"),
        }
        fields = (
            "ts_code,trade_date,buy_lg_amount,sell_lg_amount,"
            "buy_elg_amount,sell_elg_amount,net_mf_amount"
        )
        try:
            df = query_tushare_api(
                "moneyflow",
                params=params,
                fields=fields,
                timeout=max(1, int(query_timeout)),
            )
        except Exception as exc:
            return {}, None, [f"tushare_moneyflow:{type(exc).__name__}:{exc}"]

        if df is None or df.empty:
            return {}, None, ["tushare_moneyflow:empty_data"]

        net_by_date: Dict[str, float] = {}
        main_by_date: Dict[str, float] = {}
        raw_by_date: Dict[str, Dict[str, Any]] = {}
        for _, row in df.iterrows():
            trade_date = _safe_str(row.get("trade_date")).replace("-", "")[:8]
            if len(trade_date) != 8:
                continue
            date_key = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            net_amount = _safe_float(row.get("net_mf_amount"))
            buy_lg = _safe_float(row.get("buy_lg_amount"))
            sell_lg = _safe_float(row.get("sell_lg_amount"))
            buy_elg = _safe_float(row.get("buy_elg_amount"))
            sell_elg = _safe_float(row.get("sell_elg_amount"))
            if net_amount is not None:
                net_by_date[date_key] = float(net_amount) * 10000.0
            if any(value is not None for value in (buy_lg, sell_lg, buy_elg, sell_elg)):
                main_amount = (buy_lg or 0.0) + (buy_elg or 0.0) - (sell_lg or 0.0) - (sell_elg or 0.0)
                main_by_date[date_key] = float(main_amount) * 10000.0
            raw_by_date[date_key] = {
                "net_mf_amount_10k_cny": net_amount,
                "buy_lg_amount_10k_cny": buy_lg,
                "sell_lg_amount_10k_cny": sell_lg,
                "buy_elg_amount_10k_cny": buy_elg,
                "sell_elg_amount_10k_cny": sell_elg,
            }

        net_rows = sorted(net_by_date.items(), key=lambda item: item[0])
        main_rows = sorted(main_by_date.items(), key=lambda item: item[0])
        if not net_rows and not main_rows:
            return {}, None, ["tushare_moneyflow:no_main_amount"]

        latest_date = (main_rows or net_rows)[-1][0]
        latest_main = main_by_date.get(latest_date)
        latest_net = net_by_date.get(latest_date)
        main_amounts = [item[1] for item in main_rows]
        net_amounts = [item[1] for item in net_rows]
        return {
            "main_net_inflow": latest_main,
            "main_inflow_5d": float(sum(main_amounts[-5:])) if main_amounts else None,
            "main_inflow_10d": float(sum(main_amounts[-10:])) if main_amounts else None,
            "net_inflow": latest_net,
            "net_inflow_5d": float(sum(net_amounts[-5:])) if net_amounts else None,
            "net_inflow_10d": float(sum(net_amounts[-10:])) if net_amounts else None,
            # Backward-compatible aliases now point to the main-force口径.
            "inflow_5d": float(sum(main_amounts[-5:])) if main_amounts else None,
            "inflow_10d": float(sum(main_amounts[-10:])) if main_amounts else None,
            "latest_date": latest_date,
            "source_update": "tushare_moneyflow_after_market_close",
            "amount_unit": "CNY",
            "raw_amount_unit": "10k CNY",
            "main_inflow_definition": "(buy_lg_amount + buy_elg_amount - sell_lg_amount - sell_elg_amount) * 10000",
            "net_inflow_definition": "net_mf_amount * 10000",
            "latest_raw": raw_by_date.get(latest_date, {}),
        }, "tushare_moneyflow", []

    def _get_stockapi_capital_flow(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page_no: int = 1,
        page_size: int = 50,
    ) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        """
        Fetch historical stock capital flow from stockapi.com.cn.

        The endpoint is updated after market close and returns daily history, so it
        is used to fill the daily fields expected by get_capital_flow without
        touching Eastmoney individual fund-flow endpoints by default.
        """
        code = _normalize_code(stock_code)
        if not re.fullmatch(r"\d{6}", code or ""):
            return {}, None, [f"stockapi_codeFlow:not_supported:{stock_code}"]

        token = os.getenv("STOCKAPI_TOKEN", "").strip()
        latest_queryable_date = _stockapi_code_flow_completed_date()
        normalized_page_no = _normalize_stockapi_page_value(page_no, default=1)
        normalized_page_size = _normalize_stockapi_page_value(page_size, default=50)
        windows: List[Tuple[date, date]] = []
        try:
            explicit_start = _parse_stockapi_code_flow_date(start_date, "start_date")
            explicit_end = _parse_stockapi_code_flow_date(end_date, "end_date")
        except ValueError as exc:
            return {}, None, [f"stockapi_codeFlow:invalid_date:{exc}"]

        if explicit_start is not None or explicit_end is not None:
            window_end = explicit_end or latest_queryable_date
            window_start = explicit_start or window_end
            if window_start > window_end:
                return {}, None, ["stockapi_codeFlow:invalid_date:start_date_after_end_date"]
            windows.append((window_start, window_end))
        elif token:
            window_end = latest_queryable_date
            lower_bound = latest_queryable_date - timedelta(days=90)
            while window_end >= lower_bound and len(windows) < 4:
                window_start = max(lower_bound, window_end - timedelta(days=20))
                windows.append((window_start, window_end))
                window_end = window_start - timedelta(days=1)
        else:
            end_date = latest_queryable_date - timedelta(days=4)
            window_end = end_date
            lower_bound = end_date - timedelta(days=14)
            while window_end >= lower_bound and len(windows) < 3:
                window_start = max(lower_bound, window_end - timedelta(days=4))
                windows.append((window_start, window_end))
                window_end = window_start - timedelta(days=1)

        rows: List[Any] = []
        errors: List[str] = []
        for start_date, window_end in windows:
            params: Dict[str, Any] = {
                "code": code,
                "startDate": start_date.isoformat(),
                "endDate": window_end.isoformat(),
                "pageNo": str(normalized_page_no),
                "pageSize": str(normalized_page_size),
            }
            if token:
                params["token"] = token

            try:
                response = requests.get(_stockapi_code_flow_url(), params=params, timeout=3.0)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                errors.append(f"stockapi_codeFlow:{type(exc).__name__}:{exc}")
                continue

            if not isinstance(payload, dict):
                errors.append("stockapi_codeFlow:invalid_response")
                continue
            if payload.get("code") != 20000:
                errors.append(f"stockapi_codeFlow:{payload.get('code')}:{payload.get('msg')}")
                continue

            data = payload.get("data")
            if isinstance(data, list):
                rows.extend(data)
                if data:
                    break

        if not rows:
            if errors:
                return {}, None, errors
            return {}, None, ["stockapi_codeFlow:empty_data"]

        normalized_by_date: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = _safe_float(row.get("mainAmount"))
            date_text = _safe_str(row.get("date"))
            if amount is None or not date_text:
                continue
            normalized_by_date[date_text] = amount
        normalized_rows = sorted(normalized_by_date.items(), key=lambda item: item[0])
        if not normalized_rows:
            return {}, None, ["stockapi_codeFlow:no_main_amount"]

        amounts = [item[1] for item in normalized_rows]
        latest_date, latest_main = normalized_rows[-1]
        return {
            "main_net_inflow": latest_main,
            "inflow_5d": float(sum(amounts[-5:])),
            "inflow_10d": float(sum(amounts[-10:])),
            "latest_date": latest_date,
            "source_update": "after_market_close",
        }, "stockapi_codeFlow", []

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
