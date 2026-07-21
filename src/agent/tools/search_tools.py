# -*- coding: utf-8 -*-
"""
Search tools — wraps SearchService methods as agent-callable tools.

Tools:
- search_stock_news: search latest stock news
- score_stock_news_sentiment: score company-level news and announcement events
- search_stock_prompt_intel: search a user's single-stock prompt with the stock anchor
- search_comprehensive_intel: multi-dimensional intelligence search
"""

import logging
import hashlib
import sys
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.agent.sentiment.news_events import score_news_items
from src.agent.tools.registry import ToolParameter, ToolDefinition
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))
_ORZ_DAILYNEWS_ENDPOINT = "https://orz.ai/api/v1/dailynews/"
_CLS_TELEGRAPH_V1_ENDPOINT = "https://www.cls.cn/v1/roll/get_roll_list"
_CLS_TELEGRAPH_ENDPOINT = _CLS_TELEGRAPH_V1_ENDPOINT
_CLS_TELEGRAPH_PAGE_URL = "https://www.cls.cn/telegraph"
_XUEQIU_HOT_PAGE_URL = "https://xueqiu.com/"
_SINA_FINANCE_PAGE_URL = "https://finance.sina.com.cn/7x24/"
_EASTMONEY_FINANCE_PAGE_URL = "https://finance.eastmoney.com/"
_ORZ_DAILYNEWS_PLATFORM_META = {
    "cls": {
        "source": "财联社电报",
        "provider": "orz.dailynews.cls",
        "page_url": _CLS_TELEGRAPH_PAGE_URL,
    },
    "xueqiu": {
        "source": "雪球热榜",
        "provider": "orz.dailynews.xueqiu",
        "page_url": _XUEQIU_HOT_PAGE_URL,
    },
    "sina_finance": {
        "source": "新浪财经7x24",
        "provider": "orz.dailynews.sina_finance",
        "page_url": _SINA_FINANCE_PAGE_URL,
    },
    "eastmoney": {
        "source": "东方财富快讯",
        "provider": "orz.dailynews.eastmoney",
        "page_url": _EASTMONEY_FINANCE_PAGE_URL,
    },
}
_MACRO_FINANCE_PLATFORMS = ("sina_finance", "eastmoney")
_MACRO_DAILYNEWS_KEYWORDS = (
    "非农",
    "就业数据",
    "失业率",
    "初请失业金",
    "ADP",
    "CPI",
    "PPI",
    "PMI",
    "GDP",
    "PCE",
    "通胀",
    "美联储",
    "FOMC",
    "加息",
    "降息",
    "利率",
    "国债收益率",
    "美元指数",
    "人民币汇率",
    "汇率",
    "央行",
    "人民银行",
    "公开市场",
    "逆回购",
    "MLF",
    "LPR",
    "降准",
    "存款准备金率",
    "社融",
    "M2",
    "流动性",
    "净投放",
    "净回笼",
    "财政政策",
    "货币政策",
    "油价",
    "原油",
    "大宗商品",
)
_ASCII_MACRO_KEYWORDS = {"ADP", "CPI", "PPI", "PMI", "GDP", "PCE", "FOMC", "MLF", "LPR", "M2"}
_CENTRAL_BANK_ENTITY_KEYWORDS = {"央行", "人民银行"}
_CENTRAL_BANK_OPERATION_KEYWORDS = {
    "公开市场",
    "逆回购",
    "正回购",
    "MLF",
    "LPR",
    "降准",
    "降息",
    "加息",
    "存款准备金率",
    "流动性",
    "净投放",
    "净回笼",
    "货币政策",
}
_MACRO_SEARCH_FALLBACK_QUERIES = (
    ("us_nfp", "美国 非农 就业数据 美联储 最新"),
    ("china_open_market", "央行 逆回购 公开市场 操作 净投放 最新"),
    ("china_liquidity", "人民银行 MLF LPR 降准 降息 流动性 最新"),
    ("global_macro", "CPI PPI PMI 美联储 利率 美元指数 最新"),
)
_ORZ_DAILYNEWS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
_CLS_TELEGRAPH_HEADERS = {
    **_ORZ_DAILYNEWS_HEADERS,
    "Referer": _CLS_TELEGRAPH_PAGE_URL,
}


def _run_search_task_with_timeout(
    task: Callable[[], Any],
    timeout_seconds: float,
    task_name: str,
) -> Tuple[Any, Optional[str], int]:
    timeout_value = max(0.0, float(timeout_seconds or 0.0))
    start = time.time()
    if timeout_value <= 0:
        return None, f"{task_name} timeout", 0
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(task)
        try:
            return future.result(timeout=timeout_value), None, int((time.time() - start) * 1000)
        except FuturesTimeoutError:
            future.cancel()
            return None, f"{task_name} timeout", int(timeout_value * 1000)
    except Exception as exc:
        return None, str(exc), int((time.time() - start) * 1000)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db
    return get_db()


def _get_search_service():
    """Return shared SearchService singleton."""
    from src.search_service import get_search_service
    return get_search_service()


def _canonical_search_code(stock_code: str) -> str:
    from data_provider.base import canonical_stock_code, normalize_stock_code

    return canonical_stock_code(normalize_stock_code(str(stock_code or "").strip()))


def _resolve_stock_name(stock_code: str, stock_name: Optional[str] = None) -> str:
    code = str(stock_code or "").strip()
    current = str(stock_name or "").strip()
    if is_meaningful_stock_name(current, code):
        return current
    for name in (STOCK_NAME_MAP.get(code), get_index_stock_name(code)):
        if is_meaningful_stock_name(name, code):
            return str(name)
    return current or code


def _to_tushare_ts_code(stock_code: str) -> str:
    code = str(stock_code or "").strip().upper()
    if "." in code and code.endswith((".SH", ".SZ", ".BJ")):
        return code
    digits = re.sub(r"\D", "", code)
    if not digits:
        return code
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _to_yfinance_symbol(stock_code: str) -> str:
    code = str(stock_code or "").strip().upper()
    if not code:
        return ""
    if any(code.endswith(suffix) for suffix in (".SS", ".SZ", ".BJ", ".HK")):
        return code
    if "." in code:
        return code
    digits = re.sub(r"\D", "", code)
    if not digits:
        return code
    if len(digits) == 5:
        return f"{digits}.HK"
    if digits.startswith(("6", "9")):
        return f"{digits}.SS"
    if digits.startswith(("0", "2", "3")):
        return f"{digits}.SZ"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    return code


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "重要", "重点", "是"}


def _normalize_cls_stock_id(stock_id: Any) -> Dict[str, str]:
    raw = str(stock_id or "").strip()
    if not raw:
        return {"raw": "", "code": "", "ts_code": ""}
    lowered = raw.lower()
    digits = re.sub(r"\D", "", raw)
    market = ""
    if lowered.startswith("sh"):
        market = "SH"
    elif lowered.startswith("sz"):
        market = "SZ"
    elif lowered.startswith("bj"):
        market = "BJ"
    elif "." in raw:
        left, right = raw.rsplit(".", 1)
        return {"raw": raw, "code": re.sub(r"\D", "", left), "ts_code": f"{left}.{right.upper()}"}
    elif digits.startswith(("6", "9")):
        market = "SH"
    elif digits.startswith(("8", "4")):
        market = "BJ"
    elif digits:
        market = "SZ"
    return {
        "raw": raw,
        "code": digits or raw,
        "ts_code": f"{digits}.{market}" if digits and market else "",
    }


def _cls_timestamp(ts_value: Any) -> Dict[str, Any]:
    ts = _safe_int(ts_value, 0)
    if ts <= 0:
        return {"published_ts": 0, "published_at": "", "date": "", "time": ""}
    dt = datetime.fromtimestamp(ts, tz=_CN_TZ)
    return {
        "published_ts": ts,
        "published_at": dt.isoformat(),
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
    }


def _dailynews_timestamp(value: Any) -> Dict[str, Any]:
    if isinstance(value, (int, float)) or str(value or "").strip().isdigit():
        return _cls_timestamp(value)

    text = str(value or "").strip()
    if not text:
        return {"published_ts": 0, "published_at": "", "date": "", "time": ""}

    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text[:19], fmt).replace(tzinfo=_CN_TZ)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_CN_TZ)
            else:
                parsed = parsed.astimezone(_CN_TZ)
        except ValueError:
            return {"published_ts": 0, "published_at": "", "date": "", "time": ""}

    ts = int(parsed.timestamp())
    return {
        "published_ts": ts,
        "published_at": parsed.isoformat(),
        "date": parsed.strftime("%Y-%m-%d"),
        "time": parsed.strftime("%H:%M:%S"),
    }


def _orz_dailynews_query_url(platform: str) -> str:
    return f"{_ORZ_DAILYNEWS_ENDPOINT}?platform={str(platform or '').strip()}"


def _extract_cls_roll_data(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        roll_data = data.get("roll_data")
        if isinstance(roll_data, list):
            return [item for item in roll_data if isinstance(item, dict)]
        linked = data.get("l")
        if isinstance(linked, dict):
            rows = [item for _, item in sorted(linked.items(), reverse=True)]
            return [item for item in rows if isinstance(item, dict)]
        if isinstance(linked, list):
            return [item for item in linked if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _normalize_cls_subjects(subjects: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(subjects, list):
        return normalized
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        name = str(subject.get("subject_name") or subject.get("name") or "").strip()
        if not name:
            continue
        normalized.append({
            "id": _safe_int(subject.get("subject_id") or subject.get("id"), 0),
            "name": name,
            "plate_id": _safe_int(subject.get("plate_id"), 0),
            "channel": str(subject.get("channel") or "").strip(),
        })
    return normalized


def _normalize_cls_stocks(stock_list: Any) -> List[Dict[str, Any]]:
    stocks: List[Dict[str, Any]] = []
    if not isinstance(stock_list, list):
        return stocks
    for stock in stock_list:
        if not isinstance(stock, dict):
            continue
        stock_id = stock.get("StockID") or stock.get("stock_id") or stock.get("code")
        code_info = _normalize_cls_stock_id(stock_id)
        name = str(stock.get("name") or stock.get("Name") or stock.get("stock_name") or "").strip()
        if not (code_info["code"] or name):
            continue
        stocks.append({
            "name": name,
            "code": code_info["code"],
            "ts_code": code_info["ts_code"],
            "raw_stock_id": code_info["raw"],
            "last": _safe_float(stock.get("last"), 0.0),
            "change_pct": _safe_float(stock.get("RiseRange"), 0.0),
            "status": str(stock.get("status") or "").strip(),
        })
    return stocks


def _normalize_orz_dailynews_item(row: Dict[str, Any], *, platform: str) -> Dict[str, Any]:
    platform_key = str(platform or "").strip().lower()
    meta = _ORZ_DAILYNEWS_PLATFORM_META.get(
        platform_key,
        {
            "source": f"orz dailynews {platform_key or 'unknown'}",
            "provider": f"orz.dailynews.{platform_key or 'unknown'}",
            "page_url": _ORZ_DAILYNEWS_ENDPOINT,
        },
    )
    source_label = str(row.get("_source_label") or meta["source"])
    provider = str(row.get("_provider") or meta["provider"])
    default_url = str(row.get("_page_url") or meta["page_url"])

    rank = _safe_int(row.get("rank"), 0)
    score = _safe_float(row.get("score"), 0.0)
    raw_item_id = row.get("id") or row.get("news_id") or row.get("article_id") or row.get("rank")
    item_id = str(raw_item_id or "").strip()
    title = str(row.get("title") or "").strip()
    brief = str(row.get("brief") or row.get("summary") or "").strip()
    content = str(row.get("content") or "").strip()
    snippet = brief or content or title
    display_title = title or snippet[:80]
    timestamp = _dailynews_timestamp(
        row.get("publish_time") or row.get("published_at") or row.get("published_date") or row.get("ctime")
    )
    level = str(row.get("level") or "").strip().upper()
    subjects = _normalize_cls_subjects(row.get("subjects"))
    stocks = _normalize_cls_stocks(row.get("stock_list"))
    important_flags = {
        "level": level,
        "jpush": _safe_int(row.get("jpush"), 0),
        "recommend": _safe_int(row.get("recommend"), 0),
        "is_top": _safe_int(row.get("is_top"), 0),
        "is_fad": _safe_int(row.get("is_fad"), 0),
        "rank": rank,
        "score": score,
    }
    return {
        "id": item_id,
        "title": display_title,
        "brief": brief,
        "content": content,
        "snippet": snippet,
        "url": str(row.get("url") or (f"https://www.cls.cn/detail/{item_id}" if platform_key == "cls" and item_id else default_url)),
        "source": source_label,
        "provider": provider,
        **timestamp,
        "level": level,
        "is_important": level in {"A", "B"} or (0 < rank <= 10) or score >= 100 or any(
            important_flags[key] for key in ("jpush", "recommend", "is_top", "is_fad")
        ),
        "subjects": subjects,
        "subject_names": [item["name"] for item in subjects],
        "stocks": stocks,
        "author": str(row.get("author") or "").strip(),
        "reading_num": _safe_int(row.get("reading_num"), 0),
        "comment_num": _safe_int(row.get("comment_num"), 0),
        "share_num": _safe_int(row.get("share_num"), 0),
        "raw_type": row.get("type"),
        "rank": rank,
        "score": score,
        "raw_source": str(row.get("source") or platform_key).strip(),
        "importance_flags": important_flags,
    }


def _normalize_cls_telegraph_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_orz_dailynews_item(row, platform="cls")


def _fetch_orz_dailynews_payload(platform: str, timeout_seconds: float) -> Dict[str, Any]:
    response = requests.get(
        _ORZ_DAILYNEWS_ENDPOINT,
        params={"platform": platform},
        headers=_ORZ_DAILYNEWS_HEADERS,
        timeout=max(1.0, float(timeout_seconds or 6.0)),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("orz dailynews returned non-object JSON")
    return body


def _make_cls_v1_sign(params: Dict[str, Any]) -> str:
    sign_str = "&".join(f"{key}={params[key]}" for key in sorted(params.keys()))
    sha1 = hashlib.sha1(sign_str.encode("utf-8")).hexdigest()
    return hashlib.md5(sha1.encode("utf-8")).hexdigest()


def _fetch_cls_telegraph_v1_payload(
    *,
    last_time: int,
    count: int,
    category: str = "",
    timeout_seconds: float,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "app": "CailianpressWeb",
        "os": "web",
        "sv": "8.4.6",
        "refresh_type": "1",
        "rn": str(max(1, min(int(count or 20), 50))),
        "last_time": str(max(1, int(last_time or time.time()))),
        "category": str(category or ""),
    }
    params["sign"] = _make_cls_v1_sign(params)
    response = requests.get(
        _CLS_TELEGRAPH_V1_ENDPOINT,
        params=params,
        headers=_CLS_TELEGRAPH_HEADERS,
        timeout=max(1.0, float(timeout_seconds or 6.0)),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("cls v1 roll returned non-object JSON")
    return body


def _fetch_cls_telegraph_payload(params: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
    last_time = _safe_int((params or {}).get("last_time"), int(time.time()))
    count = _safe_int((params or {}).get("rn") or (params or {}).get("count"), 20)
    category = str((params or {}).get("category") or "")
    return _fetch_cls_telegraph_v1_payload(
        last_time=last_time,
        count=count,
        category=category,
        timeout_seconds=timeout_seconds,
    )


def _normalize_cls_v1_row(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(row)
    payload.setdefault("url", f"https://www.cls.cn/detail/{payload.get('id')}")
    payload["_provider"] = "cls.v1.roll"
    payload["_source_label"] = "财联社电报"
    payload["_page_url"] = _CLS_TELEGRAPH_PAGE_URL
    return _normalize_orz_dailynews_item(payload, platform="cls")


def _dailynews_payload_error(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return "invalid dailynews response"

    status = payload.get("status")
    if status is not None and str(status).strip().lower() not in {"200", "0", "ok", "success"}:
        return str(payload.get("msg") or payload.get("message") or status)

    errno = payload.get("errno")
    if errno not in (0, "0", None):
        return str(payload.get("msg") or payload.get("message") or errno)

    return None


def _filter_dailynews_items(
    items: List[Dict[str, Any]],
    *,
    effective_limit: int,
    important_only: bool,
    keyword: str,
    last_time: int = 0,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    filtered = list(items)
    if important_only:
        filtered = [item for item in filtered if item.get("is_important")]
    if last_time > 0:
        filtered = [
            item for item in filtered
            if _safe_int(item.get("published_ts"), 0) <= last_time
        ]
    if min_score > 0:
        filtered = [
            item for item in filtered
            if _safe_float(item.get("score"), 0.0) >= min_score
        ]
    keyword_text = str(keyword or "").strip()
    if keyword_text:
        lowered_keyword = keyword_text.lower()
        filtered = [
            item for item in filtered
            if lowered_keyword in " ".join([
                str(item.get("title") or ""),
                str(item.get("brief") or ""),
                str(item.get("content") or ""),
                " ".join(str(name) for name in item.get("subject_names") or []),
                " ".join(str(stock.get("name") or "") for stock in item.get("stocks") or []),
            ]).lower()
        ]
    return filtered[:effective_limit]


def _dailynews_item_text(item: Dict[str, Any]) -> str:
    return " ".join([
        str(item.get("title") or ""),
        str(item.get("brief") or ""),
        str(item.get("content") or ""),
        " ".join(str(name) for name in item.get("subject_names") or []),
        " ".join(str(stock.get("name") or "") for stock in item.get("stocks") or [] if isinstance(stock, dict)),
    ])


def _macro_keywords_for_dailynews_item(item: Dict[str, Any]) -> List[str]:
    text = _dailynews_item_text(item)
    lowered = text.lower()
    matched: List[str] = []
    for keyword in _MACRO_DAILYNEWS_KEYWORDS:
        keyword_text = str(keyword or "").strip()
        if not keyword_text:
            continue
        if _dailynews_text_has_macro_keyword(text, lowered, keyword_text):
            matched.append(keyword_text)
    return matched


def _is_macro_dailynews_item(item: Dict[str, Any]) -> bool:
    return bool(_macro_keywords_for_dailynews_item(item))


def _dailynews_text_has_macro_keyword(text: str, lowered: str, keyword: str) -> bool:
    if keyword in _CENTRAL_BANK_ENTITY_KEYWORDS:
        return keyword in text and any(
            _dailynews_text_has_macro_keyword(text, lowered, term)
            for term in _CENTRAL_BANK_OPERATION_KEYWORDS
        )
    if keyword.upper() in _ASCII_MACRO_KEYWORDS:
        return bool(re.search(rf"(?<![A-Za-z0-9.]){re.escape(keyword)}(?![A-Za-z0-9.])", text, re.IGNORECASE))
    return keyword.lower() in lowered


def _split_dailynews_platforms(value: str = "") -> List[str]:
    requested = [part.strip().lower() for part in str(value or "").split(",") if part.strip()]
    platforms = requested or list(_MACRO_FINANCE_PLATFORMS)
    allowed = set(_ORZ_DAILYNEWS_PLATFORM_META)
    result: List[str] = []
    for platform in platforms:
        if platform in allowed and platform not in result:
            result.append(platform)
    return result or list(_MACRO_FINANCE_PLATFORMS)


def _macro_item_dedup_key(item: Dict[str, Any]) -> str:
    return "|".join(
        str(part or "")
        for part in (
            item.get("provider"),
            item.get("id"),
            item.get("url"),
            item.get("title"),
            item.get("published_at") or item.get("published_date"),
        )
    )


def _normalize_macro_search_result(result: Any, *, query_id: str, query: str, provider: str, rank: int) -> Optional[Dict[str, Any]]:
    title = str(getattr(result, "title", "") or "").strip()
    snippet = str(getattr(result, "snippet", "") or "").strip()
    url = str(getattr(result, "url", "") or "").strip()
    source = str(getattr(result, "source", "") or "search_general_news").strip()
    published_date = getattr(result, "published_date", None)
    text = " ".join([title, snippet, str(published_date or ""), query])
    matched_keywords = _macro_keywords_for_dailynews_item({
        "title": title,
        "brief": snippet,
        "content": "",
        "subject_names": [],
        "stocks": [],
    })
    if not matched_keywords:
        return None
    item_id = hashlib.sha1("|".join([query_id, title, url, str(published_date or "")]).encode("utf-8")).hexdigest()[:16]
    timestamp = _dailynews_timestamp(published_date)
    return {
        "id": item_id,
        "title": title or snippet[:80] or query,
        "brief": snippet,
        "content": snippet,
        "snippet": snippet,
        "url": url,
        "source": source,
        "provider": f"search_general_news:{provider or 'unknown'}",
        **timestamp,
        "level": "",
        "is_important": True,
        "subjects": [],
        "subject_names": [],
        "stocks": [],
        "author": "",
        "reading_num": 0,
        "comment_num": 0,
        "share_num": 0,
        "raw_type": "search_general_news",
        "rank": rank,
        "score": max(0.0, 100.0 - rank),
        "raw_source": source,
        "importance_flags": {"search_fallback": 1, "query_id": query_id},
        "macro_keywords": matched_keywords,
        "is_macro": True,
        "fallback_query": query,
        "fallback_query_id": query_id,
        "published_date": str(published_date or ""),
        "search_text": text,
    }


def _handle_get_orz_dailynews(
    *,
    platform: str,
    limit: int,
    important_only: bool = False,
    keyword: str = "",
    last_time: int = 0,
    min_score: float = 0.0,
    timeout_seconds: float = 6.0,
) -> dict:
    started = time.time()
    platform_key = str(platform or "").strip().lower()
    effective_limit = max(1, min(_safe_int(limit, 20), 50))
    important_flag = _coerce_bool(important_only)
    effective_timeout = max(1.0, min(float(timeout_seconds or 6.0), 15.0))
    effective_last_time = _safe_int(last_time, 0)
    effective_min_score = max(0.0, _safe_float(min_score, 0.0))
    keyword_text = str(keyword or "").strip()
    provider = f"orz.dailynews.{platform_key}"
    query_url = _orz_dailynews_query_url(platform_key)

    payload, err, fetch_ms = _run_search_task_with_timeout(
        lambda: _fetch_orz_dailynews_payload(platform_key, timeout_seconds=effective_timeout),
        effective_timeout + 1.0,
        provider,
    )
    source_chain: List[Dict[str, Any]] = [{
        "provider": provider,
        "endpoint": _ORZ_DAILYNEWS_ENDPOINT,
        "params": {"platform": platform_key},
        "page_url": _ORZ_DAILYNEWS_PLATFORM_META.get(platform_key, {}).get("page_url", _ORZ_DAILYNEWS_ENDPOINT),
        "result": "pending",
        "duration_ms": fetch_ms,
    }]
    if err or not isinstance(payload, dict):
        source_chain[0]["result"] = "error"
        return {
            "status": "error",
            "provider": provider,
            "query_url": query_url,
            "endpoint": _ORZ_DAILYNEWS_ENDPOINT,
            "results_count": 0,
            "results": [],
            "important_only": important_flag,
            "keyword": keyword_text,
            "last_time": effective_last_time,
            "min_score": effective_min_score,
            "source_chain": source_chain,
            "errors": [str(err or "invalid orz dailynews response")],
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    payload_error = _dailynews_payload_error(payload)
    if payload_error:
        source_chain[0]["result"] = "error"
        source_chain[0]["payload_status"] = payload.get("status")
        source_chain[0]["errno"] = payload.get("errno")
        return {
            "status": "error",
            "provider": provider,
            "query_url": query_url,
            "endpoint": _ORZ_DAILYNEWS_ENDPOINT,
            "results_count": 0,
            "results": [],
            "important_only": important_flag,
            "keyword": keyword_text,
            "last_time": effective_last_time,
            "min_score": effective_min_score,
            "source_chain": source_chain,
            "errors": [payload_error],
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    rows = _extract_cls_roll_data(payload)
    items = [
        _normalize_orz_dailynews_item(row, platform=platform_key)
        for row in rows
    ]
    items = _filter_dailynews_items(
        items,
        effective_limit=effective_limit,
        important_only=important_flag,
        keyword=keyword_text,
        last_time=effective_last_time,
        min_score=effective_min_score,
    )
    source_chain[0]["result"] = "ok" if items else "empty"
    source_chain[0]["count"] = len(items)
    source_chain[0]["payload_status"] = payload.get("status")

    return {
        "status": "ok" if items else "empty",
        "provider": provider,
        "query_url": query_url,
        "endpoint": _ORZ_DAILYNEWS_ENDPOINT,
        "results_count": len(items),
        "results": items,
        "important_only": important_flag,
        "keyword": keyword_text,
        "last_time": effective_last_time,
        "min_score": effective_min_score,
        "next_last_time": min(
            [item["published_ts"] for item in items if item.get("published_ts")] or [0]
        ),
        "source_chain": source_chain,
        "errors": [],
        "elapsed_ms": int((time.time() - started) * 1000),
        "notes": [
            "Data source: https://orz.ai/api/v1/dailynews/",
            "The upstream feed exposes ranked dailynews items; cursor support is emulated by local published_ts filtering.",
        ],
    }


def _handle_get_cls_telegraph_news(
    limit: int = 20,
    important_only: bool = False,
    keyword: str = "",
    last_time: int = 0,
    timeout_seconds: float = 6.0,
) -> dict:
    """Fetch 财联社电报 from cls.cn first, falling back to orz dailynews."""

    started = time.time()
    effective_limit = max(1, min(_safe_int(limit, 20), 50))
    important_flag = _coerce_bool(important_only)
    effective_timeout = max(1.0, min(float(timeout_seconds or 6.0), 15.0))
    effective_last_time = _safe_int(last_time, 0)
    keyword_text = str(keyword or "").strip()
    provider = "cls.v1.roll"
    first_cursor = effective_last_time or int(time.time())
    query_url = _CLS_TELEGRAPH_V1_ENDPOINT
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    rows: List[Dict[str, Any]] = []
    seen_row_keys: set[str] = set()
    cursor = first_cursor
    base_rounds = max((effective_limit + 19) // 20, 1)
    rounds = max(base_rounds + 2, 3) if keyword_text or important_flag else base_rounds

    for _ in range(rounds):
        params = {
            "app": "CailianpressWeb",
            "os": "web",
            "sv": "8.4.6",
            "refresh_type": "1",
            "rn": "20",
            "last_time": str(cursor),
            "category": "",
        }
        signed_params = dict(params)
        signed_params["sign"] = _make_cls_v1_sign(signed_params)
        payload, err, fetch_ms = _run_search_task_with_timeout(
            lambda p=dict(params): _fetch_cls_telegraph_v1_payload(
                last_time=_safe_int(p.get("last_time"), first_cursor),
                count=_safe_int(p.get("rn"), 20),
                category=str(p.get("category") or ""),
                timeout_seconds=effective_timeout,
            ),
            effective_timeout + 1.0,
            provider,
        )
        entry: Dict[str, Any] = {
            "provider": provider,
            "endpoint": _CLS_TELEGRAPH_V1_ENDPOINT,
            "params": signed_params,
            "page_url": _CLS_TELEGRAPH_PAGE_URL,
            "result": "pending",
            "duration_ms": fetch_ms,
        }
        source_chain.append(entry)
        if err or not isinstance(payload, dict):
            entry["result"] = "error"
            errors.append(str(err or "invalid cls v1 response"))
            break
        payload_error = _dailynews_payload_error(payload)
        if payload_error:
            entry["result"] = "error"
            entry["errno"] = payload.get("errno")
            errors.append(payload_error)
            break
        batch = _extract_cls_roll_data(payload)
        entry["result"] = "ok" if batch else "empty"
        entry["count"] = len(batch)
        entry["errno"] = payload.get("errno")
        if not batch:
            break
        fresh_batch: List[Dict[str, Any]] = []
        for row in batch:
            row_key = str(row.get("id") or row.get("news_id") or row.get("ctime") or json.dumps(row, sort_keys=True, default=str))
            if row_key in seen_row_keys:
                continue
            seen_row_keys.add(row_key)
            fresh_batch.append(row)
        if not fresh_batch:
            break
        rows.extend(fresh_batch)
        normalized_so_far = [_normalize_cls_v1_row(row) for row in rows]
        filtered_so_far = _filter_dailynews_items(
            normalized_so_far,
            effective_limit=effective_limit,
            important_only=important_flag,
            keyword=keyword_text,
            last_time=effective_last_time,
        )
        if len(filtered_so_far) >= effective_limit:
            break
        next_cursor = min([_safe_int(row.get("ctime"), 0) for row in batch if _safe_int(row.get("ctime"), 0)] or [0])
        if next_cursor <= 0 or next_cursor >= cursor:
            break
        cursor = next_cursor

    if rows:
        items = [_normalize_cls_v1_row(row) for row in rows]
        items = _filter_dailynews_items(
            items,
            effective_limit=effective_limit,
            important_only=important_flag,
            keyword=keyword_text,
            last_time=effective_last_time,
        )
        return {
            "status": "ok" if items else "empty",
            "provider": provider,
            "query_url": query_url,
            "endpoint": _CLS_TELEGRAPH_V1_ENDPOINT,
            "results_count": len(items),
            "results": items,
            "important_only": important_flag,
            "keyword": keyword_text,
            "last_time": effective_last_time,
            "min_score": 0.0,
            "next_last_time": min([item["published_ts"] for item in items if item.get("published_ts")] or [0]),
            "source_chain": source_chain,
            "errors": errors,
            "elapsed_ms": int((time.time() - started) * 1000),
            "notes": [
                "Primary data source: https://www.cls.cn/v1/roll/get_roll_list",
                "Fallback data source: https://orz.ai/api/v1/dailynews/?platform=cls",
            ],
        }

    fallback = _handle_get_orz_dailynews(
        platform="cls",
        limit=limit,
        important_only=important_only,
        keyword=keyword,
        last_time=last_time,
        timeout_seconds=timeout_seconds,
    )
    fallback_chain = fallback.get("source_chain") if isinstance(fallback.get("source_chain"), list) else []
    fallback_errors = fallback.get("errors") if isinstance(fallback.get("errors"), list) else []
    fallback["source_chain"] = [*source_chain, *fallback_chain]
    fallback["errors"] = [*errors, *fallback_errors]
    fallback["elapsed_ms"] = int((time.time() - started) * 1000)
    if fallback.get("status") == "ok":
        fallback["provider"] = "cls.v1.roll+orz.dailynews.cls"
    return fallback


def _handle_get_xueqiu_hot_news(
    limit: int = 20,
    keyword: str = "",
    min_score: float = 0.0,
    timeout_seconds: float = 6.0,
) -> dict:
    """Fetch 雪球热榜 items through the orz dailynews feed."""

    return _handle_get_orz_dailynews(
        platform="xueqiu",
        limit=limit,
        keyword=keyword,
        min_score=min_score,
        timeout_seconds=timeout_seconds,
    )


def _handle_get_macro_finance_news(
    limit: int = 30,
    platforms: str = "",
    keyword: str = "",
    include_search_fallback: bool = True,
    search_days: int = 3,
    timeout_seconds: float = 6.0,
) -> dict:
    """Fetch macro-finance items from finance-oriented orz dailynews feeds."""

    started = time.time()
    effective_limit = max(1, min(_safe_int(limit, 30), 50))
    effective_timeout = max(1.0, min(float(timeout_seconds or 6.0), 15.0))
    keyword_text = str(keyword or "").strip()
    fallback_enabled = _coerce_bool(include_search_fallback)
    effective_search_days = max(1, min(_safe_int(search_days, 3), 30))
    selected_platforms = _split_dailynews_platforms(platforms)
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    merged: List[Dict[str, Any]] = []
    seen = set()

    for platform in selected_platforms:
        result = _handle_get_orz_dailynews(
            platform=platform,
            limit=50,
            important_only=False,
            keyword="",
            timeout_seconds=effective_timeout,
        )
        source_chain.extend(result.get("source_chain") or [])
        if result.get("status") == "error":
            errors.extend(str(item) for item in result.get("errors") or [])
            continue
        for item in result.get("results") or []:
            if not isinstance(item, dict):
                continue
            if keyword_text and keyword_text.lower() not in _dailynews_item_text(item).lower():
                continue
            matched_keywords = _macro_keywords_for_dailynews_item(item)
            if not matched_keywords:
                continue
            dedup_key = _macro_item_dedup_key(item)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            enriched = dict(item)
            enriched["macro_keywords"] = matched_keywords
            enriched["is_macro"] = True
            merged.append(enriched)

    if fallback_enabled and len(merged) < effective_limit:
        try:
            service = _get_search_service()
            if not getattr(service, "is_available", False):
                source_chain.append({
                    "provider": "search_general_news",
                    "result": "disabled",
                    "days": effective_search_days,
                    "queries": [query for _, query in _MACRO_SEARCH_FALLBACK_QUERIES],
                })
            else:
                for query_id, query in _MACRO_SEARCH_FALLBACK_QUERIES:
                    if len(merged) >= effective_limit:
                        break
                    response = service.search_general_news(
                        query,
                        max_results=3,
                        days=effective_search_days,
                    )
                    provider = str(getattr(response, "provider", "") or "search_general_news")
                    ok = bool(getattr(response, "success", False))
                    source_chain.append({
                        "provider": provider,
                        "result": "ok" if ok else "failed",
                        "query_id": query_id,
                        "query": query,
                        "days": effective_search_days,
                        "max_results": 3,
                    })
                    if not ok:
                        errors.append(str(getattr(response, "error_message", "") or f"{query_id} search failed"))
                        continue
                    for rank, result in enumerate(getattr(response, "results", []) or [], start=1):
                        item = _normalize_macro_search_result(
                            result,
                            query_id=query_id,
                            query=query,
                            provider=provider,
                            rank=rank,
                        )
                        if not item:
                            continue
                        if keyword_text and keyword_text.lower() not in _dailynews_item_text(item).lower():
                            continue
                        dedup_key = _macro_item_dedup_key(item)
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)
                        merged.append(item)
                        if len(merged) >= effective_limit:
                            break
        except Exception as exc:
            errors.append(str(exc))
            source_chain.append({
                "provider": "search_general_news",
                "result": "error",
                "error": str(exc),
                "days": effective_search_days,
            })

    merged.sort(
        key=lambda item: (
            -_safe_int(item.get("published_ts"), 0),
            _safe_int(item.get("rank"), 9999) or 9999,
            -_safe_float(item.get("score"), 0.0),
        )
    )
    items = merged[:effective_limit]
    for chain in source_chain:
        if isinstance(chain, dict) and chain.get("result") == "ok":
            chain_provider = str(chain.get("provider") or "")
            chain["macro_count"] = sum(
                1
                for item in items
                if str(item.get("provider") or "") in {chain_provider, f"search_general_news:{chain_provider}"}
            )

    return {
        "status": "ok" if items else ("partial" if errors else "empty"),
        "provider": "orz.dailynews.macro_finance",
        "query_url": ",".join(_orz_dailynews_query_url(platform) for platform in selected_platforms),
        "endpoint": _ORZ_DAILYNEWS_ENDPOINT,
        "platforms": selected_platforms,
        "results_count": len(items),
        "results": items,
        "keyword": keyword_text,
        "include_search_fallback": fallback_enabled,
        "search_days": effective_search_days,
        "macro_keywords": list(_MACRO_DAILYNEWS_KEYWORDS),
        "source_chain": source_chain,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
        "notes": [
            "Data source: https://orz.ai/api/v1/dailynews/",
            "Macro layer is filtered locally from finance-oriented platforms, currently sina_finance and eastmoney by default.",
        ],
    }


def _openinvest_path() -> Optional[Path]:
    root = Path(__file__).resolve().parents[3] / "openInvest"
    return root if root.exists() else None


def _ensure_openinvest_import_path() -> Optional[Path]:
    root = _openinvest_path()
    if root is None:
        return None
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return root


def _raw_openinvest_item_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "title": str(getattr(item, "title", "") or ""),
        "snippet": str(getattr(item, "snippet", "") or ""),
        "url": str(getattr(item, "url", "") or ""),
        "source": str(getattr(item, "src_name", "") or ""),
        "published_date": getattr(item, "published_at", None),
        "fetched_at": getattr(item, "fetched_at", None),
        "raw_meta": getattr(item, "raw_meta", {}) or {},
    }


def _handle_search_openinvest_news(
    stock_code: str = "",
    stock_name: str = "",
    symbol: str = "",
    query: str = "",
    include_yfinance: bool = True,
    include_rss: bool = False,
    include_ddgs: bool = False,
    max_results: int = 8,
    timeout_seconds: float = 12.0,
) -> dict:
    """Fetch ticker-linked/news-source items using openInvest's news adapters."""

    started = time.time()
    root = _ensure_openinvest_import_path()
    if root is None:
        return {
            "status": "unavailable",
            "provider": "openInvest.news_sources",
            "results": [],
            "results_count": 0,
            "source_chain": [{"provider": "openInvest.news_sources", "result": "missing_openInvest_dir"}],
            "errors": ["openInvest directory not found"],
        }

    resolved_name = _resolve_stock_name(stock_code, stock_name) if stock_code else str(stock_name or "").strip()
    yf_symbol = str(symbol or "").strip() or _to_yfinance_symbol(stock_code)
    effective_query = str(query or "").strip()
    if not effective_query and (stock_code or resolved_name):
        effective_query = " ".join(item for item in (resolved_name, stock_code, "新闻") if item)
    effective_limit = max(1, min(20, int(max_results or 8)))
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    items: List[Dict[str, Any]] = []

    try:
        from services.news_sources import fetch_all
        from services.news_sources.rss_feed import load_default_feeds
    except Exception as exc:
        return {
            "status": "unavailable",
            "provider": "openInvest.news_sources",
            "results": [],
            "results_count": 0,
            "source_chain": [{"provider": "openInvest.news_sources", "result": "import_failed"}],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    queries = [effective_query] if include_ddgs and effective_query else []
    symbols = [yf_symbol] if include_yfinance and yf_symbol else []
    rss_feeds = load_default_feeds()[:4] if include_rss else []
    if include_ddgs:
        try:
            __import__("ddgs")
        except Exception as exc:
            source_chain.append({
                "provider": "openInvest.ddgs_news",
                "result": "missing_dependency",
                "error": f"{type(exc).__name__}: {exc}",
            })
            queries = []
    if include_rss and not rss_feeds:
        source_chain.append({"provider": "openInvest.rss_feed", "result": "no_feeds"})

    raw_items, err, elapsed_ms = _run_search_task_with_timeout(
        lambda: fetch_all(
            queries=queries,
            symbols=symbols,
            rss_feeds=rss_feeds,
            max_per_source=effective_limit,
            extract_fulltext=False,
            timeout_sec=max(1.0, float(timeout_seconds or 12.0)),
        ),
        max(1.0, float(timeout_seconds or 12.0) + 1.0),
        "openinvest_news_sources",
    )
    if err:
        errors.append(str(err))
    for item in raw_items or []:
        row = _raw_openinvest_item_to_dict(item)
        if row.get("title") and row.get("url"):
            items.append(row)

    provider_counts: Dict[str, int] = {}
    for item in items:
        provider = str(item.get("source") or "unknown").split(":", 1)[0]
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
    for provider in ("yfinance", "rss", "ddgs"):
        requested = (
            (provider == "yfinance" and include_yfinance and bool(yf_symbol))
            or (provider == "rss" and include_rss)
            or (provider == "ddgs" and include_ddgs and bool(effective_query))
        )
        if requested:
            source_chain.append({
                "provider": f"openInvest.{provider}",
                "result": "ok" if provider_counts.get(provider, 0) else "empty",
                "count": provider_counts.get(provider, 0),
            })

    scored = score_news_items(items)
    return {
        "status": "ok" if items else ("error" if errors else "empty"),
        "provider": "openInvest.news_sources",
        "stock_code": _canonical_search_code(stock_code) if stock_code else "",
        "stock_name": resolved_name,
        "symbol": yf_symbol,
        "query": effective_query,
        "results_count": len(items),
        "results": items[:effective_limit],
        "message_score": scored["message_score"],
        "message_state": scored["message_state"],
        "event_tags": scored["event_tags"],
        "risk_flags": scored["risk_flags"],
        "source_chain": source_chain,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
        "fetch_elapsed_ms": elapsed_ms,
        "notes": [
            "yfinance source is ticker-linked and useful for US/HK/A-share Yahoo symbols",
            "ddgs full search is optional and requires ddgs dependency",
        ],
    }


def _persist_news_response(
    *,
    stock_code: str,
    stock_name: str,
    dimension: str,
    response,
) -> None:
    """Best-effort news persistence for Agent search tools."""
    if not response or not getattr(response, "success", False) or not getattr(response, "results", None):
        return

    code = _canonical_search_code(stock_code)
    try:
        saved_count = _get_db().save_news_intel(
            code=code,
            name=stock_name,
            dimension=dimension,
            query=response.query,
            response=response,
            query_context=None,
        )
        logger.info(
            "Agent news intel persisted for %s (dimension=%s, new_records=%s)",
            code,
            dimension,
            saved_count,
        )
    except Exception as exc:
        logger.warning(
            "Agent news intel persistence failed for %s (dimension=%s): %s",
            code,
            dimension,
            exc,
        )


def _fetch_tushare_announcements_result(stock_code: str, *, lookback_hours: int) -> Dict[str, Any]:
    """Best-effort Tushare announcement fetch using the HTTP API directly."""
    try:
        from src.config import get_config
        token = str(getattr(get_config(), "tushare_token", "") or "").strip()
    except Exception:
        token = ""
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(hours=max(1, int(lookback_hours or 72)))
    date_window = {
        "start_date": start_dt.strftime("%Y%m%d"),
        "end_date": end_dt.strftime("%Y%m%d"),
    }
    if not token:
        return {
            "status": "disabled",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": "TUSHARE_TOKEN is not configured",
        }
    payload = {
        "api_name": "anns_d",
        "token": token,
        "params": {
            "ts_code": _to_tushare_ts_code(stock_code),
            "start_date": date_window["start_date"],
            "end_date": date_window["end_date"],
        },
        "fields": "ann_date,ts_code,name,title,url",
    }
    try:
        from data_provider.tushare_client import get_tushare_http_url

        response = requests.post(get_tushare_http_url(), json=payload, timeout=5)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.debug("Tushare announcement fetch skipped for %s: %s", stock_code, exc)
        return {
            "status": "error",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": str(exc),
        }

    if body.get("code") not in (0, "0", None):
        logger.debug("Tushare announcement fetch failed for %s: %s", stock_code, body.get("msg"))
        return {
            "status": "error",
            "items": [],
            "provider": "Tushare.anns_d",
            "date_window": date_window,
            "error": str(body.get("msg") or body.get("code")),
        }
    data = body.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    announcements: List[Dict[str, Any]] = []
    for row in items:
        if not isinstance(row, list):
            continue
        record = dict(zip(fields, row))
        title = str(record.get("title") or "").strip()
        if not title:
            continue
        announcements.append({
            "title": title,
            "snippet": title,
            "url": str(record.get("url") or "").strip(),
            "source": "Tushare.anns_d",
            "published_date": str(record.get("ann_date") or "").strip(),
        })
    return {
        "status": "ok" if announcements else "empty",
        "items": announcements,
        "provider": "Tushare.anns_d",
        "date_window": date_window,
    }


def _fetch_tushare_announcements(stock_code: str, *, lookback_hours: int) -> List[Dict[str, Any]]:
    return list(
        _fetch_tushare_announcements_result(stock_code, lookback_hours=lookback_hours).get("items")
        or []
    )


def _response_to_news_items(response) -> List[Dict[str, Any]]:
    if not response or not getattr(response, "success", False):
        return []
    return [
        {
            "title": getattr(item, "title", ""),
            "snippet": getattr(item, "snippet", ""),
            "url": getattr(item, "url", ""),
            "source": getattr(item, "source", ""),
            "published_date": getattr(item, "published_date", None),
        }
        for item in getattr(response, "results", []) or []
    ]


def _handle_search_stock_news(stock_code: str, stock_name: str) -> dict:
    """Search latest news for a stock."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    response = service.search_stock_news(stock_code, stock_name, max_results=5)

    if not response.success:
        return {
            "query": response.query,
            "success": False,
            "error": response.error_message,
        }

    _persist_news_response(
        stock_code=stock_code,
        stock_name=stock_name,
        dimension="latest_news",
        response=response,
    )

    return {
        "query": response.query,
        "provider": response.provider,
        "success": True,
        "results_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "source": r.source,
                "published_date": r.published_date,
            }
            for r in response.results
        ],
    }


def _handle_score_stock_news_sentiment(
    stock_code: str,
    stock_name: Optional[str] = None,
    lookback_hours: int = 72,
    include_announcements: bool = True,
) -> dict:
    """Score one stock's message/news state from search news and announcements."""
    service = _get_search_service()
    resolved_name = _resolve_stock_name(stock_code, stock_name)
    news_items: List[Dict[str, Any]] = []
    search_payload: Dict[str, Any] = {"success": False, "results_count": 0}

    if getattr(service, "is_available", False):
        focus_keywords = [
            f"{resolved_name} {stock_code} 公告 业绩 订单 合作 减持 监管 问询 回购 增持 最近"
        ]
        try:
            response = service.search_stock_news(
                stock_code,
                resolved_name,
                max_results=8,
                focus_keywords=focus_keywords,
                max_provider_attempts=2,
            )
        except TypeError as exc:
            if "max_provider_attempts" not in str(exc):
                raise
            response = service.search_stock_news(
                stock_code,
                resolved_name,
                max_results=8,
                focus_keywords=focus_keywords,
            )
        if getattr(response, "success", False):
            _persist_news_response(
                stock_code=stock_code,
                stock_name=resolved_name,
                dimension="message_sentiment",
                response=response,
            )
        news_items.extend(_response_to_news_items(response))
        search_payload = {
            "query": getattr(response, "query", ""),
            "provider": getattr(response, "provider", ""),
            "success": bool(getattr(response, "success", False)),
            "results_count": len(getattr(response, "results", []) or []),
            **({"error": getattr(response, "error_message", None)} if getattr(response, "error_message", None) else {}),
        }
    else:
        search_payload = {"success": False, "error": "No search engine available"}

    announcements: List[Dict[str, Any]] = []
    announcements_payload: Dict[str, Any] = {
        "provider": "Tushare.anns_d",
        "enabled": include_announcements,
        "count": 0,
        "status": "disabled",
    }
    if include_announcements:
        announcements_result = _fetch_tushare_announcements_result(stock_code, lookback_hours=lookback_hours)
        announcements = list(announcements_result.get("items") or [])
        announcements_payload = {
            "provider": announcements_result.get("provider") or "Tushare.anns_d",
            "enabled": include_announcements,
            "count": len(announcements),
            "status": announcements_result.get("status") or ("ok" if announcements else "empty"),
            "date_window": announcements_result.get("date_window"),
            **({"error": announcements_result.get("error")} if announcements_result.get("error") else {}),
        }
        news_items.extend(announcements)

    scored = score_news_items(news_items)
    return {
        "status": "ok" if scored["news_count"] else "empty",
        "code": _canonical_search_code(stock_code),
        "name": resolved_name,
        "lookback_hours": lookback_hours,
        "include_announcements": include_announcements,
        "message_score": scored["message_score"],
        "message_state": scored["message_state"],
        "news_count": scored["news_count"],
        "positive_count": scored["positive_count"],
        "negative_count": scored["negative_count"],
        "uncertain_count": scored["uncertain_count"],
        "event_tags": scored["event_tags"],
        "events": scored["events"],
        "risk_flags": scored["risk_flags"],
        "summary": scored["summary"],
        "sources": {
            "search": search_payload,
            "announcements": announcements_payload,
        },
    }


_PROMPT_QUERY_STOPWORDS = {
    "这个",
    "一下",
    "看看",
    "帮我",
    "有吗",
    "了吗",
    "如何",
    "怎么样",
    "为什么",
    "什么",
    "现在",
    "最近",
    "今天",
    "昨天",
}


def _extract_prompt_search_terms(user_prompt: str, limit: int = 8) -> List[str]:
    text = str(user_prompt or "").strip()
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._%+-]*|[\u4e00-\u9fa5]{2,}", text)
    terms: List[str] = []
    seen = set()
    for token in tokens:
        cleaned = token.strip(" ，。！？；;：:、（）()[]【】\"'")
        if not cleaned or cleaned in _PROMPT_QUERY_STOPWORDS:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def _build_stock_prompt_query(
    stock_code: str,
    stock_name: str = "",
    user_prompt: str = "",
    search_scope: str = "auto",
) -> str:
    code = str(stock_code or "").strip()
    name = _resolve_stock_name(code, stock_name)
    prompt = str(user_prompt or "").strip()
    scope = str(search_scope or "auto").strip().lower()
    terms = _extract_prompt_search_terms(prompt)
    anchors = [item for item in [name, code] if item]
    scope_terms: List[str] = []

    if scope == "announcement" or any(word in prompt for word in ("公告", "年报", "季报", "互动", "投资者关系", "监管函", "问询函")):
        scope_terms.extend(["公告", "投资者关系", "年报"])
    elif scope == "price" or any(word in prompt for word in ("走势", "股价", "涨停", "跌停", "K线", "资金")):
        scope_terms.extend(["走势", "股价", "资金"])
    elif scope == "news" or any(word in prompt for word in ("新闻", "消息", "传闻", "订单", "合同", "合作")):
        scope_terms.extend(["新闻", "消息"])
    elif scope == "risk" or any(word in prompt for word in ("风险", "处罚", "减持", "诉讼", "立案", "问询")):
        scope_terms.extend(["风险", "处罚", "减持", "问询"])

    query_parts: List[str] = []
    for item in anchors + terms + scope_terms:
        if item and item not in query_parts:
            query_parts.append(item)

    if not query_parts:
        return prompt
    return " ".join(query_parts[:14])


def _handle_search_stock_prompt_intel(
    stock_code: str,
    stock_name: Optional[str] = None,
    user_prompt: str = "",
    search_scope: str = "auto",
    days: int = 30,
    max_results: int = 6,
) -> dict:
    """Search the user's single-stock prompt using the configured search providers."""
    service = _get_search_service()
    resolved_name = _resolve_stock_name(stock_code, stock_name)
    query = _build_stock_prompt_query(stock_code, resolved_name, user_prompt, search_scope)
    effective_days = max(1, min(int(days or 30), 365))
    effective_limit = max(1, min(int(max_results or 6), 10))

    if not getattr(service, "is_available", False):
        return {
            "status": "disabled",
            "stock_code": _canonical_search_code(stock_code),
            "stock_name": resolved_name,
            "user_prompt": str(user_prompt or ""),
            "query": query,
            "results_count": 0,
            "results": [],
            "source_chain": [{
                "provider": "search_general_news",
                "result": "disabled",
                "days": effective_days,
                "max_results": effective_limit,
            }],
            "errors": ["No search engine available (no API keys configured)"],
        }

    response = service.search_general_news(
        query,
        max_results=effective_limit,
        days=effective_days,
    )

    if not getattr(response, "success", False):
        return {
            "status": "failed",
            "stock_code": _canonical_search_code(stock_code),
            "stock_name": resolved_name,
            "user_prompt": str(user_prompt or ""),
            "query": getattr(response, "query", query),
            "provider": getattr(response, "provider", ""),
            "results_count": 0,
            "results": [],
            "source_chain": [{
                "provider": getattr(response, "provider", "") or "search_general_news",
                "result": "failed",
                "days": effective_days,
                "max_results": effective_limit,
            }],
            "errors": [str(getattr(response, "error_message", "") or "search failed")],
        }

    _persist_news_response(
        stock_code=stock_code,
        stock_name=resolved_name,
        dimension="prompt_intel",
        response=response,
    )

    results = [
        {
            "title": r.title,
            "snippet": r.snippet,
            "url": r.url,
            "source": r.source,
            "published_date": r.published_date,
        }
        for r in getattr(response, "results", []) or []
    ]
    return {
        "status": "ok" if results else "empty",
        "stock_code": _canonical_search_code(stock_code),
        "stock_name": resolved_name,
        "user_prompt": str(user_prompt or ""),
        "query": getattr(response, "query", query),
        "provider": getattr(response, "provider", ""),
        "results_count": len(results),
        "results": results,
        "source_chain": [{
            "provider": getattr(response, "provider", "") or "search_general_news",
            "result": "ok" if results else "empty",
            "days": effective_days,
            "max_results": effective_limit,
        }],
        "errors": [],
    }


search_stock_news_tool = ToolDefinition(
    name="search_stock_news",
    description="Search for the latest news articles about a specific stock. "
                "Requires both stock_code and stock_name for accurate search. "
                "Returns news titles, snippets, sources, and URLs.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_stock_news,
    category="search",
)


search_openinvest_news_tool = ToolDefinition(
    name="search_openinvest_news",
    description=(
        "Fetch news through openInvest's multi-source news adapters. "
        "Use this when configured search engines are unavailable or when ticker-linked Yahoo/yfinance "
        "news may be more relevant than generic keyword search. DDGS/RSS are optional sources."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Optional stock code, e.g. '600519' or 'AAPL'. Used to infer a yfinance symbol.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Optional stock name, e.g. '贵州茅台'.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="symbol",
            type="string",
            description="Optional explicit yfinance symbol, e.g. '600519.SS', '0700.HK', or 'AAPL'.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="query",
            type="string",
            description="Optional DDGS query. Only used when include_ddgs=true and ddgs is installed.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="include_yfinance",
            type="boolean",
            description="Whether to fetch yfinance ticker-linked news.",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="include_rss",
            type="boolean",
            description="Whether to fetch openInvest's default finance RSS feeds.",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="include_ddgs",
            type="boolean",
            description="Whether to fetch DuckDuckGo news via ddgs. Requires optional ddgs dependency.",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="max_results",
            type="integer",
            description="Max normalized news items to return (default: 8, max: 20).",
            required=False,
            default=8,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description="Wall-clock timeout in seconds for the openInvest news fetch.",
            required=False,
            default=12.0,
        ),
    ],
    handler=_handle_search_openinvest_news,
    category="search",
)


get_cls_telegraph_news_tool = ToolDefinition(
    name="get_cls_telegraph_news",
    description=(
        "Fetch 财联社电报 items from https://www.cls.cn/v1/roll/get_roll_list, with orz dailynews fallback. "
        "Use this for intraday market news, policy/material/company catalysts, and theme-catalyst evidence. "
        "Returns normalized ranked news cards with publish time, score/rank, source_chain, "
        "and structured degradation errors when the upstream feed is unavailable."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Max telegraph items to return (default: 20, max: 50).",
            required=False,
            default=20,
        ),
        ToolParameter(
            name="important_only",
            type="boolean",
            description="Whether to keep only high-importance items based on rank/score and legacy CLS flags when present.",
            required=False,
            default=False,
        ),
        ToolParameter(
            name="keyword",
            type="string",
            description="Optional keyword filter over title, brief, content, subjects, and linked stock names.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="last_time",
            type="integer",
            description="Optional published_ts upper bound. CLS v1 uses it as last_time for paging and local filtering.",
            required=False,
            default=0,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description="HTTP timeout budget in seconds (default: 6, max: 15).",
            required=False,
            default=6.0,
        ),
    ],
    handler=_handle_get_cls_telegraph_news,
    category="search",
)


get_xueqiu_hot_news_tool = ToolDefinition(
    name="get_xueqiu_hot_news",
    description=(
        "Fetch 雪球热榜 items via https://orz.ai/api/v1/dailynews/?platform=xueqiu. "
        "Use this to observe retail/social attention, hot themes, and market discussion heat. "
        "Returns normalized ranked hot-news cards with publish time, score/rank, source_chain, "
        "and structured degradation errors when the upstream feed is unavailable."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Max hot-list items to return (default: 20, max: 50).",
            required=False,
            default=20,
        ),
        ToolParameter(
            name="keyword",
            type="string",
            description="Optional keyword filter over title and content.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="min_score",
            type="number",
            description="Optional minimum heat score filter.",
            required=False,
            default=0.0,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description="HTTP timeout budget in seconds (default: 6, max: 15).",
            required=False,
            default=6.0,
        ),
    ],
    handler=_handle_get_xueqiu_hot_news,
    category="search",
)


get_macro_finance_news_tool = ToolDefinition(
    name="get_macro_finance_news",
    description=(
        "Fetch macro-finance news from finance-oriented orz dailynews feeds, currently "
        "sina_finance and eastmoney by default. Use this for nonfarm payrolls, CPI/PPI/PMI, "
        "Fed/interest-rate events, PBOC open-market operations, reverse repos, MLF/LPR, "
        "liquidity and broad market risk-appetite signals. Returns only items matched by "
        "macro keywords, with provider/source_chain and structured degradation errors."
    ),
    parameters=[
        ToolParameter(
            name="limit",
            type="integer",
            description="Max macro-finance items to return (default: 30, max: 50).",
            required=False,
            default=30,
        ),
        ToolParameter(
            name="platforms",
            type="string",
            description="Comma-separated orz dailynews platforms. Defaults to sina_finance,eastmoney.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="keyword",
            type="string",
            description="Optional extra keyword filter after macro filtering.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="include_search_fallback",
            type="boolean",
            description="Whether to use SearchService.search_general_news for fixed macro queries when dailynews coverage is thin.",
            required=False,
            default=True,
        ),
        ToolParameter(
            name="search_days",
            type="integer",
            description="Lookback days for SearchService fallback queries (default: 3, max: 30).",
            required=False,
            default=3,
        ),
        ToolParameter(
            name="timeout_seconds",
            type="number",
            description="HTTP timeout budget in seconds per upstream platform (default: 6, max: 15).",
            required=False,
            default=6.0,
        ),
    ],
    handler=_handle_get_macro_finance_news,
    category="search",
)


search_stock_prompt_intel_tool = ToolDefinition(
    name="search_stock_prompt_intel",
    description=(
        "Search the configured search engines for a user's single-stock question. "
        "Use this when the user asks about one stock's announcements, latest events, regulatory letters, "
        "orders/contracts, rumors, capital-flow news, or price-move context in natural language."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g. '688126'.",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Optional stock name in Chinese. If omitted, the local stock-name index is used.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="user_prompt",
            type="string",
            description="The user's original single-stock question, e.g. '有什么公告，你看看走势'.",
        ),
        ToolParameter(
            name="search_scope",
            type="string",
            description="Optional search hint: auto, announcement, news, price, or risk.",
            required=False,
            default="auto",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="Search freshness window in days (default: 30, max: 365).",
            required=False,
            default=30,
        ),
        ToolParameter(
            name="max_results",
            type="integer",
            description="Max results to return (default: 6, max: 10).",
            required=False,
            default=6,
        ),
    ],
    handler=_handle_search_stock_prompt_intel,
    category="search",
)


score_stock_news_sentiment_tool = ToolDefinition(
    name="score_stock_news_sentiment",
    description=(
        "Score one stock's message/news sentiment from recent news and optional Tushare announcements. "
        "Classifies earnings, orders/contracts, policy tailwinds, buybacks/increases, reductions, "
        "regulatory risks, litigation/defaults, and rumor/abnormal-trading uncertainty."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g. '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Optional stock name in Chinese. If omitted, the local stock-name index is used.",
            required=False,
            default="",
        ),
        ToolParameter(
            name="lookback_hours",
            type="integer",
            description="Lookback window for announcements/news in hours (default: 72).",
            required=False,
            default=72,
        ),
        ToolParameter(
            name="include_announcements",
            type="boolean",
            description="Whether to include Tushare announcement hard events when TUSHARE_TOKEN is configured.",
            required=False,
            default=True,
        ),
    ],
    handler=_handle_score_stock_news_sentiment,
    category="search",
)


# ============================================================
# search_comprehensive_intel
# ============================================================

def _preprocess_intel_with_llm(
    raw_items: List[Dict[str, Any]],
    stock_name: str,
    timeout_seconds: float = 4.0,
) -> Dict[str, Any]:
    """Lightweight LLM pass to produce a fixed-schema intel summary."""
    import json as _json

    if not raw_items:
        return {"items": [], "key_signals": [], "overall_sentiment": "unknown"}

    compact = [
        {
            "dim": item.get("dim", ""),
            "title": (item.get("title") or "")[:120],
            "date": item.get("date") or "",
            "source": item.get("source") or "",
        }
        for item in raw_items
    ]

    prompt = (
        f'你是股票新闻解析助手。对以下关于"{stock_name}"的新闻标题列表，'
        "输出JSON（无其他内容）：\n"
        '{"items":[{"title":"原标题","date":"日期","source":"来源","dim":"维度",'
        '"signal_type":"capital_flow|earnings|risk|announcement|policy|general",'
        '"sentiment":"positive|negative|neutral","one_line":"一句话要点"}],'
        '"key_signals":["最关键信号"],"overall_sentiment":"positive|negative|neutral|mixed"}\n\n'
        f"新闻列表：\n{_json.dumps(compact, ensure_ascii=False)}"
    )

    try:
        from src.agent.llm_adapter import LLMToolAdapter

        resp = LLMToolAdapter().call_text(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=500,
            timeout=max(1.0, float(timeout_seconds or 4.0)),
        )
        text = (resp.content or "").strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
        return _json.loads(text)
    except Exception as exc:
        logger.warning("Intel LLM preprocessing failed: %s", exc)

    return {
        "items": compact,
        "key_signals": [],
        "overall_sentiment": "unknown",
    }


def _handle_search_comprehensive_intel(stock_code: str, stock_name: str) -> dict:
    """Multi-dimensional intelligence search with LLM-preprocessed structured output."""
    service = _get_search_service()

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    intel_results, search_err, search_ms = _run_search_task_with_timeout(
        lambda: service.search_comprehensive_intel(
            stock_code=stock_code,
            stock_name=stock_name,
            max_searches=2,
        ),
        24.0,
        "search_comprehensive_intel",
    )
    if search_err or not isinstance(intel_results, dict):
        status = "timeout" if search_err and "timeout" in str(search_err).lower() else "error"
        return {
            "status": status,
            "stock_code": _canonical_search_code(stock_code),
            "stock_name": stock_name,
            "intel": {"items": [], "key_signals": [], "overall_sentiment": "unknown"},
            "dimensions_searched": [],
            "dimensions_empty": [],
            "source_chain": [{
                "provider": "search_comprehensive_intel",
                "result": status,
                "duration_ms": search_ms,
            }],
            "errors": [str(search_err or "invalid search result")],
        }

    if not intel_results:
        return {"error": "Comprehensive intel search returned no results"}

    # Collect raw items across all dimensions for LLM preprocessing
    raw_items: List[Dict[str, Any]] = []
    for dim_name, response in intel_results.items():
        if response and response.success:
            _persist_news_response(
                stock_code=stock_code,
                stock_name=stock_name,
                dimension=dim_name,
                response=response,
            )
            for r in response.results[:4]:
                raw_items.append({
                    "dim": dim_name,
                    "title": r.title,
                    "date": r.published_date or "",
                    "source": r.source,
                    "snippet": (r.snippet or "")[:80],
                })

    # Lightweight LLM pass → fixed schema; fallback to raw compact list on failure
    intel = _preprocess_intel_with_llm(raw_items, stock_name, timeout_seconds=4.0)

    return {
        "status": "ok" if raw_items else "empty",
        "stock_code": _canonical_search_code(stock_code),
        "stock_name": stock_name,
        "intel": intel,
        "dimensions_searched": [
            dim for dim, resp in intel_results.items() if resp and resp.success
        ],
        "dimensions_empty": [
            dim for dim, resp in intel_results.items() if not (resp and resp.success)
        ],
        "source_chain": [{
            "provider": "search_comprehensive_intel",
            "result": "ok",
            "duration_ms": search_ms,
            "max_searches": 2,
        }],
    }


search_comprehensive_intel_tool = ToolDefinition(
    name="search_comprehensive_intel",
    description="Multi-dimensional intelligence search: latest news, market analysis, "
                "risk checking, earnings outlook, and industry trends for a stock. "
                "Returns a formatted report and structured results.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_comprehensive_intel,
    category="search",
)


ALL_SEARCH_TOOLS = [
    search_stock_news_tool,
    search_openinvest_news_tool,
    get_cls_telegraph_news_tool,
    get_xueqiu_hot_news_tool,
    get_macro_finance_news_tool,
    search_stock_prompt_intel_tool,
    score_stock_news_sentiment_tool,
    search_comprehensive_intel_tool,
]
