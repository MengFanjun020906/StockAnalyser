# -*- coding: utf-8 -*-
"""Deterministic news and announcement event scoring for Agent tools."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List


POSITIVE_EVENT_RULES: List[Dict[str, Any]] = [
    {
        "event_type": "contract_order",
        "direction": "positive",
        "severity": "high",
        "weight": 16,
        "keywords": ("重大合同", "大订单", "中标", "签订合同", "采购订单", "订单增长", "定点通知"),
    },
    {
        "event_type": "earnings_positive",
        "direction": "positive",
        "severity": "high",
        "weight": 14,
        "keywords": ("业绩预增", "扭亏", "净利润增长", "利润大增", "同比增长", "超预期"),
    },
    {
        "event_type": "buyback_increase",
        "direction": "positive",
        "severity": "medium",
        "weight": 9,
        "keywords": ("回购", "增持", "员工持股", "股权激励"),
    },
    {
        "event_type": "policy_tailwind",
        "direction": "positive",
        "severity": "medium",
        "weight": 8,
        "keywords": ("政策支持", "补贴", "试点", "产业规划", "设备更新", "国产替代"),
    },
    {
        "event_type": "partnership_product",
        "direction": "positive",
        "severity": "medium",
        "weight": 7,
        "keywords": ("战略合作", "新品发布", "量产", "产能扩张", "通过认证", "客户导入"),
    },
]

NEGATIVE_EVENT_RULES: List[Dict[str, Any]] = [
    {
        "event_type": "reduction_unlock",
        "direction": "negative",
        "severity": "high",
        "weight": -16,
        "keywords": ("减持", "拟减持", "清仓式减持", "限售股解禁", "大比例解禁"),
    },
    {
        "event_type": "regulatory_risk",
        "direction": "negative",
        "severity": "high",
        "weight": -18,
        "keywords": ("立案", "处罚", "行政处罚", "监管函", "问询函", "警示函", "调查"),
    },
    {
        "event_type": "earnings_negative",
        "direction": "negative",
        "severity": "high",
        "weight": -16,
        "keywords": ("业绩预减", "亏损", "净利润下降", "同比下降", "商誉减值", "计提减值"),
    },
    {
        "event_type": "litigation_default",
        "direction": "negative",
        "severity": "high",
        "weight": -14,
        "keywords": ("诉讼", "违约", "债务逾期", "冻结", "被执行", "退市风险"),
    },
]

UNCERTAIN_EVENT_RULES: List[Dict[str, Any]] = [
    {
        "event_type": "rumor_high_heat",
        "direction": "uncertain",
        "severity": "medium",
        "weight": -2,
        "keywords": ("传闻", "网传", "市场传言", "未证实", "澄清公告", "异动公告"),
    },
    {
        "event_type": "price_abnormal",
        "direction": "uncertain",
        "severity": "medium",
        "weight": -1,
        "keywords": ("股价异动", "交易异常波动", "风险提示", "关注函"),
    },
]

ALL_EVENT_RULES = POSITIVE_EVENT_RULES + NEGATIVE_EVENT_RULES + UNCERTAIN_EVENT_RULES


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def dedupe_news_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate news by URL when available, otherwise by normalized title/source/date."""
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        title = _normalize_text(item.get("title"))
        if not title:
            continue
        url = _normalize_text(item.get("url"))
        source = _normalize_text(item.get("source"))
        published = _normalize_text(item.get("published_date") or item.get("published_at"))
        key_raw = url or f"{title}|{source}|{published}"
        key = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def classify_news_item(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Classify one news/announcement item into deterministic event labels."""
    title = _normalize_text(item.get("title"))
    snippet = _normalize_text(item.get("snippet") or item.get("summary"))
    text = f"{title} {snippet}"
    events: List[Dict[str, Any]] = []
    for rule in ALL_EVENT_RULES:
        hit_terms = [kw for kw in rule["keywords"] if kw in text]
        if not hit_terms:
            continue
        events.append({
            "event_type": rule["event_type"],
            "direction": rule["direction"],
            "severity": rule["severity"],
            "weight": rule["weight"],
            "hit_terms": hit_terms[:4],
            "title": title[:180],
            "snippet": snippet[:220],
            "source": _normalize_text(item.get("source")),
            "url": _normalize_text(item.get("url")),
            "published_at": _normalize_text(item.get("published_date") or item.get("published_at")),
            "confidence": 0.82 if rule["direction"] in {"positive", "negative"} else 0.62,
        })
    return events


def score_news_items(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Score a stock's message/news state from deduped news and announcement items."""
    deduped = dedupe_news_items(items)
    events: List[Dict[str, Any]] = []
    for item in deduped:
        events.extend(classify_news_item(item))

    positive = [event for event in events if event["direction"] == "positive"]
    negative = [event for event in events if event["direction"] == "negative"]
    uncertain = [event for event in events if event["direction"] == "uncertain"]
    raw_score = 50 + min(8, len(deduped)) + sum(float(event["weight"]) for event in events)
    score = int(max(0, min(100, round(raw_score))))
    if score >= 70:
        state = "positive"
    elif score >= 58:
        state = "slightly_positive"
    elif score <= 35:
        state = "negative"
    elif score <= 45:
        state = "slightly_negative"
    else:
        state = "neutral"

    risk_flags = []
    if negative:
        risk_flags.append("存在负面公告/监管/减持类事件，不能按普通热度处理。")
    if uncertain:
        risk_flags.append("存在传闻、异动或未证实信息，需要后续确认。")

    tags = []
    for event in events:
        tag = str(event.get("event_type") or "")
        if tag and tag not in tags:
            tags.append(tag)

    if positive and not negative:
        summary = "消息面偏正，主要由公司级正面事件驱动。"
    elif positive and negative:
        summary = "消息面多空混合，正面催化与负面风险并存。"
    elif negative:
        summary = "消息面偏负，存在需要反方审查的风险事件。"
    elif uncertain:
        summary = "消息面不确定，热度或异动尚未形成硬催化。"
    elif deduped:
        summary = "有相关新闻，但未识别到明确公司级硬事件。"
    else:
        summary = "未检索到可用消息。"

    return {
        "message_score": score,
        "message_state": state,
        "news_count": len(deduped),
        "positive_count": len(positive),
        "negative_count": len(negative),
        "uncertain_count": len(uncertain),
        "event_tags": tags,
        "events": events[:12],
        "risk_flags": risk_flags,
        "summary": summary,
    }
