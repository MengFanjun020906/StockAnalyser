# -*- coding: utf-8 -*-
"""新闻事件研究地基：证券代码、交易日口径、细分事件类型。

本模块只做三件确定性的事，不参与选股、打分、排序或提示词：

1. 从结构化字段或原文里抽取 6 位证券代码；抽不到时显式标为 ``no_stock``
   （对外文案为“无标的”），不伪造标的。
2. 把发布时间换算成“交易日口径”的 ``trade_date``：交易时段内按当日，
   收盘后或非交易日顺延到下一个交易日；日历不足时返回 ``None`` 并给出原因。
3. 用固定关键词规则给出细分事件类型（财报、业绩预告、减持等），并保留
   命中的规则 id，便于回溯。

所有规则都是可审计的关键词映射，不做语义推断。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.data.stock_mapping import STOCK_NAME_MAP

NO_STOCK_LABEL = "无标的"

SCOPE_SINGLE = "single"
SCOPE_MULTI = "multi"
SCOPE_NO_STOCK = "no_stock"
SCOPE_UNKNOWN = "unknown"

CATEGORY_OTHER = "其他"

MARKET_CLOSE = time(15, 0)

# 规则顺序即优先级；先命中者作为主分类，全部命中项写入 event_categories。
EVENT_CATEGORY_RULES: Tuple[Dict[str, Any], ...] = (
    {
        "category": "业绩预告",
        "rule": "kw.earnings_forecast.v1",
        "keywords": (
            "业绩预告",
            "业绩快报",
            "业绩修正",
            "预增",
            "预减",
            "预亏",
            "预盈",
            "扭亏",
            "首亏",
            "续亏",
        ),
    },
    {
        "category": "财报",
        "rule": "kw.financial_report.v1",
        "keywords": (
            "年报",
            "半年报",
            "季报",
            "一季报",
            "三季报",
            "年度报告",
            "半年度报告",
            "季度报告",
            "财报",
            "营业收入",
            "净利润",
            "毛利率",
            "营收",
            "业绩",
        ),
    },
    {
        "category": "减持",
        "rule": "kw.share_reduction.v1",
        "keywords": ("减持", "拟减持", "减持计划", "大宗减持"),
    },
    {
        "category": "增持回购",
        "rule": "kw.share_increase_buyback.v1",
        "keywords": ("增持", "回购", "股份回购", "拟回购", "增持计划"),
    },
    {
        "category": "质押",
        "rule": "kw.pledge.v1",
        "keywords": ("质押", "解押", "解除质押", "质押式回购"),
    },
    {
        "category": "监管处罚",
        "rule": "kw.regulatory_penalty.v1",
        "keywords": (
            "行政处罚",
            "处罚",
            "立案调查",
            "立案",
            "警示函",
            "监管函",
            "问询函",
            "关注函",
            "监管措施",
            "责令改正",
            "市场禁入",
            "没收",
            "罚款",
            "违规",
        ),
    },
    {
        "category": "中标订单",
        "rule": "kw.order_contract.v1",
        "keywords": (
            "中标",
            "订单",
            "合同",
            "签约",
            "框架协议",
            "采购",
            "供货",
            "定点",
        ),
    },
    {
        "category": "分红",
        "rule": "kw.dividend.v1",
        "keywords": ("分红", "派息", "现金分红", "利润分配", "送转", "转增", "每10股"),
    },
    {
        "category": "诉讼",
        "rule": "kw.litigation.v1",
        "keywords": ("诉讼", "仲裁", "起诉", "被诉", "判决"),
    },
    {
        "category": "停复牌",
        "rule": "kw.suspension_delisting.v1",
        "keywords": ("停牌", "复牌", "暂停上市", "终止上市", "退市", "风险警示", "被ST", "*ST"),
    },
    {
        "category": "重组并购",
        "rule": "kw.merger_acquisition.v1",
        "keywords": ("重大资产重组", "重组", "并购", "收购", "资产注入", "要约收购"),
    },
)

COARSE_EVENT_TYPE_CATEGORY: Dict[str, str] = {
    "业绩验证": "财报",
    "大客户/订单": "中标订单",
}

# 仅保留 A 股常见代码前缀，避免把金额、编号误当证券代码。
_A_SHARE_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
    "920",
)


def normalize_code(value: Any) -> Optional[str]:
    """Normalize ``600519.SH`` / ``sh600519`` / ``600519`` to ``600519``."""

    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if text[:2] in {"SH", "SZ", "BJ"} and text[2:].isdigit():
        text = text[2:]
    if len(text) != 6 or not text.isdigit():
        return None
    if text.startswith(_A_SHARE_PREFIXES) or text[0] in {"4", "8"}:
        return text
    return None


def _iter_codes_in_text(text: str) -> Iterable[str]:
    if not text:
        return []
    found: List[str] = []
    buffer: List[str] = []
    for char in str(text):
        if char.isdigit():
            buffer.append(char)
            continue
        if buffer:
            found.append("".join(buffer))
            buffer = []
    if buffer:
        found.append("".join(buffer))
    return [code for code in (normalize_code(item) for item in found) if code]


def _ref(code: str, name: str = "", source: str = "") -> Dict[str, str]:
    return {"code": code, "name": str(name or ""), "source": source}


def extract_security_refs(
    *,
    text: str = "",
    stocks: Any = None,
    entity_links: Any = None,
    extra_codes: Any = None,
    include_name_map: bool = True,
    name_matcher: Optional[Callable[[str], Iterable[Tuple[str, str]]]] = None,
) -> Dict[str, Any]:
    """Collect security references with provenance.

    Priority: explicit 6-digit codes in text > structured stock entries >
    company entity links > name map. ``stock_scope`` is ``single`` only when
    exactly one distinct code is found.

    ``name_matcher`` 为空时沿用内置 ``STOCK_NAME_MAP`` 的旧行为；传入
    ``AStockNameMatcher.find`` 等可调用对象时才使用扩展名称字典。
    """

    refs: Dict[str, Dict[str, str]] = {}

    def _add(code: Any, name: str = "", source: str = "") -> None:
        normalized = normalize_code(code)
        if not normalized:
            return
        if normalized not in refs:
            refs[normalized] = _ref(normalized, name, source)

    for code in _iter_codes_in_text(text):
        _add(code, "", "text_code")

    for item in stocks if isinstance(stocks, list) else []:
        if isinstance(item, dict):
            _add(
                item.get("code") or item.get("symbol") or item.get("stock_code") or item.get("ts_code"),
                str(item.get("name") or ""),
                "stocks_payload",
            )
        elif isinstance(item, str):
            _add(item, "", "stocks_payload")

    for link in entity_links if isinstance(entity_links, list) else []:
        if not isinstance(link, dict):
            continue
        if str(link.get("entity_type") or "").strip().lower() != "company":
            continue
        _add(
            link.get("symbol") or link.get("code"),
            str(link.get("name") or ""),
            "entity_links",
        )

    for code in extra_codes if isinstance(extra_codes, (list, tuple, set)) else []:
        _add(code, "", "extra_codes")

    if include_name_map and text:
        if name_matcher is not None:
            for code, name in name_matcher(text):
                _add(code, str(name), "name_map")
        else:
            for code, name in STOCK_NAME_MAP.items():
                if len(str(name)) < 3:
                    continue
                if str(name) in text:
                    _add(code, str(name), "name_map")

    codes = sorted(refs)
    if not codes:
        scope = SCOPE_NO_STOCK
    elif len(codes) == 1:
        scope = SCOPE_SINGLE
    else:
        scope = SCOPE_MULTI
    return {
        "refs": [refs[code] for code in codes],
        "codes": codes,
        "scope": scope,
    }


def classify_event_category(*texts: Any, coarse_event_type: str = "") -> Dict[str, Any]:
    """Map free text to one of the fine-grained event categories."""

    haystack = " ".join(str(item or "") for item in texts)
    matched: List[Dict[str, str]] = []
    for rule in EVENT_CATEGORY_RULES:
        for keyword in rule["keywords"]:
            if keyword and keyword in haystack:
                matched.append({"category": rule["category"], "rule": rule["rule"], "keyword": keyword})
                break
    if matched:
        primary = matched[0]
        return {
            "category": primary["category"],
            "rule": primary["rule"],
            "keyword": primary["keyword"],
            "all_categories": [item["category"] for item in matched],
            "source": "keyword",
        }
    mapped = COARSE_EVENT_TYPE_CATEGORY.get(str(coarse_event_type or "").strip())
    if mapped:
        return {
            "category": mapped,
            "rule": f"event_type.{mapped}.v1",
            "keyword": "",
            "all_categories": [mapped],
            "source": "coarse_event_type",
        }
    return {
        "category": CATEGORY_OTHER,
        "rule": "default.other.v1",
        "keyword": "",
        "all_categories": [CATEGORY_OTHER],
        "source": "default",
    }


@dataclass(frozen=True)
class TradingCalendar:
    """Trading sessions derived from local ``stock_daily`` dates."""

    dates: Tuple[date, ...]

    def contains(self, day: date) -> bool:
        return day in self.dates

    def next_after(self, day: date) -> Optional[date]:
        index = bisect.bisect_right(self.dates, day)
        return self.dates[index] if index < len(self.dates) else None

    def on_or_after(self, day: date) -> Optional[date]:
        index = bisect.bisect_left(self.dates, day)
        return self.dates[index] if index < len(self.dates) else None

    def resolve(
        self,
        *,
        published_at: Any = None,
        signal_date: Any = None,
    ) -> Tuple[Optional[date], str]:
        """Return ``(trade_date, rule)`` under the close-time convention."""

        published = _coerce_datetime(published_at)
        if published is not None:
            base = published.date()
            if published.time() <= MARKET_CLOSE:
                if self.contains(base):
                    return base, "pre_close_same_day"
                resolved = self.on_or_after(base)
                return (resolved, "non_trading_day_next") if resolved else (None, "calendar_exhausted")
            resolved = self.next_after(base)
            return (resolved, "post_close_next_day") if resolved else (None, "calendar_exhausted")

        signal = _coerce_date(signal_date)
        if signal is not None:
            if self.contains(signal):
                return signal, "signal_date_same_day"
            resolved = self.on_or_after(signal)
            return (resolved, "signal_date_next") if resolved else (None, "calendar_exhausted")
        return None, "no_date"


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0))
    text = str(value or "").strip()
    if not text:
        return None
    formats = (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    )
    for fmt, width in formats:
        try:
            return datetime.strptime(text[:width], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _coerce_datetime(value)
    return parsed.date() if parsed else None


def load_trading_calendar(dates: Iterable[Any]) -> TradingCalendar:
    normalized = sorted({item for item in (_coerce_date(value) for value in dates) if item})
    return TradingCalendar(tuple(normalized))


def build_raw_foundation_fields(
    raw: Dict[str, Any],
    *,
    calendar: Optional[TradingCalendar] = None,
    name_matcher: Optional[Callable[[str], Iterable[Tuple[str, str]]]] = None,
) -> Dict[str, Any]:
    """Build persisted foundation fields for one raw news episode."""

    subjects = raw.get("subjects") if isinstance(raw.get("subjects"), list) else []
    stocks = raw.get("stocks") if isinstance(raw.get("stocks"), list) else []
    text = " ".join(
        [
            str(raw.get("title") or ""),
            str(raw.get("summary") or ""),
            str(raw.get("content") or ""),
            str(raw.get("normalized_content") or ""),
            " ".join(str(item) for item in subjects),
            " ".join(str(item.get("name") or "") for item in stocks if isinstance(item, dict)),
        ]
    )
    refs = extract_security_refs(text=text, stocks=stocks, name_matcher=name_matcher)
    category = classify_event_category(text)
    trade_date, trade_date_rule = (None, "calendar_unavailable")
    if calendar is not None:
        trade_date, trade_date_rule = calendar.resolve(
            published_at=raw.get("published_at"),
            signal_date=raw.get("signal_date"),
        )
    return {
        "stock_codes": refs["refs"],
        "stock_scope": refs["scope"],
        "event_category": category["category"],
        "event_category_rule": category["rule"],
        "event_categories": category["all_categories"],
        "trade_date": trade_date,
        "trade_date_rule": trade_date_rule,
    }


def build_event_foundation_fields(
    event: Dict[str, Any],
    *,
    raw: Optional[Dict[str, Any]] = None,
    calendar: Optional[TradingCalendar] = None,
) -> Dict[str, Any]:
    """Build persisted foundation fields for one extracted event."""

    raw = raw or {}
    text = " ".join(
        str(event.get(key) or "")
        for key in ("evidence_sentence", "trigger", "subject", "object")
    )
    refs = extract_security_refs(
        text=text,
        entity_links=event.get("entity_links"),
        include_name_map=False,
    )
    if refs["scope"] == SCOPE_NO_STOCK:
        raw_refs = raw.get("stock_codes") if isinstance(raw.get("stock_codes"), list) else []
        fallback: Dict[str, Dict[str, str]] = {}
        for item in raw_refs:
            if isinstance(item, dict):
                normalized = normalize_code(item.get("code"))
                if normalized:
                    fallback[normalized] = _ref(
                        normalized,
                        str(item.get("name") or ""),
                        str(item.get("source") or "raw_episode"),
                    )
            elif isinstance(item, str):
                normalized = normalize_code(item)
                if normalized:
                    fallback[normalized] = _ref(normalized, "", "raw_episode")
        if fallback:
            codes = sorted(fallback)
            refs = {
                "refs": [fallback[code] for code in codes],
                "codes": codes,
                "scope": SCOPE_SINGLE if len(codes) == 1 else SCOPE_MULTI,
            }

    category = classify_event_category(
        text,
        coarse_event_type=str(event.get("event_type") or ""),
    )

    trade_date = _coerce_date(raw.get("trade_date"))
    trade_date_rule = "raw_episode"
    if trade_date is None:
        if calendar is not None:
            trade_date, trade_date_rule = calendar.resolve(
                published_at=event.get("event_time") or raw.get("published_at"),
                signal_date=event.get("signal_date") or raw.get("signal_date"),
            )
        else:
            trade_date_rule = "calendar_unavailable"

    primary = refs["codes"][0] if refs["scope"] == SCOPE_SINGLE else ""
    return {
        "stock_codes": refs["refs"],
        "stock_scope": refs["scope"],
        "primary_stock_code": primary,
        "event_category": category["category"],
        "event_category_rule": category["rule"],
        "trade_date": trade_date,
        "trade_date_rule": trade_date_rule,
    }


def summarize_scope(scope_counts: Dict[str, int]) -> Dict[str, Any]:
    """Return coverage percentages for a ``stock_scope`` distribution."""

    total = sum(int(value or 0) for value in scope_counts.values())
    covered = int(scope_counts.get(SCOPE_SINGLE, 0)) + int(scope_counts.get(SCOPE_MULTI, 0))
    return {
        "total": total,
        "with_stock": covered,
        "no_stock": int(scope_counts.get(SCOPE_NO_STOCK, 0)),
        "unknown": int(scope_counts.get(SCOPE_UNKNOWN, 0)),
        "coverage_pct": round(covered / total * 100, 2) if total else 0.0,
    }


def parse_codes_from_refs(value: Any) -> List[str]:
    """Parse ``stock_codes_json`` payloads into a sorted code list."""

    codes: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            normalized = normalize_code(item.get("code"))
        else:
            normalized = normalize_code(item)
        if normalized:
            codes.add(normalized)
    return sorted(codes)
