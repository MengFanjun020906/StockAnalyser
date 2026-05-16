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
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_STOCKAPI_CODE_FLOW_UPDATE_HOUR = 15
_STOCKAPI_CODE_FLOW_UPDATE_MINUTE = 30
_STOCKAPI_MIN_INTERVAL_SECONDS = 0.08
_STOCKAPI_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
_STOCKAPI_CACHE_TTL_SECONDS = 15.0
_STOCKAPI_REQUEST_LOCK = threading.Lock()
_STOCKAPI_RESPONSE_CACHE: Dict[Tuple[str, Tuple[Tuple[str, str], ...], bool], Tuple[float, Dict[str, Any]]] = {}
_stockapi_last_request_at = 0.0

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
    return "https://www.stockapi.com.cn/v1/base/codeFlow"


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
        return (current - timedelta(days=1)).date()
    return current.date()


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

    def get_stockapi_hot_sectors(self, date: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
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

        if not sectors and errors:
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
            "source_chain": [],
            "errors": [],
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
            result["source_chain"].append(f"northbound_summary:{summary_source}")

        hist_df, hist_source, hist_errors = self._call_df_candidates([
            ("stock_hsgt_hist_em", {"symbol": "北向资金"}),
        ])
        result["errors"].extend(hist_errors)
        if hist_df is not None and not hist_df.empty:
            rows = hist_df.tail(effective_limit).to_dict(orient="records")
            result["history"] = [self._compact_northbound_history_row(row) for row in rows]
            result["source_chain"].append(f"northbound_history:{hist_source}")

        has_content = bool(result["summary"] or result["history"])
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

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
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

        stockapi_flow, stockapi_source, stockapi_errors = self._get_stockapi_capital_flow(stock_code)
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

    def _get_stockapi_capital_flow(self, stock_code: str) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
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
        windows: List[Tuple[datetime.date, datetime.date]] = []
        if token:
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
                "pageNo": "1",
                "pageSize": "20",
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
