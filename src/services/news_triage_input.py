# -*- coding: utf-8 -*-
"""Bridge no-stock triage scoring into real-time search / deep-dive news input.

本模块只做两件事：

1. 用 ``news_no_stock_triage.triage_news_item`` 对实时搜索结果现算分类与分数，
   按 ``priority`` / ``score`` 稳定降序排列，0 分沉底；不删除任何条目。
2. 把落库的 ``no_stock_triage_json`` 中 ``priority>0`` 的新闻聚成一段
   “当日外盘/宏观环境”摘要。

它不新增任何分类规则，也不参与评分公式、阈值、gate、prompt 判定文字或点位计算。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import text

from src.services.news_no_stock_triage import TriageResult, triage_news_item

logger = logging.getLogger(__name__)

TRIAGE_BRIEF_SCAN_LIMIT = 40
TRIAGE_BRIEF_TITLES_PER_CHANNEL = 3
TRIAGE_SUMMARY_TITLE_LIMIT = 5
TRIAGE_REPORT_TITLE_LIMIT = 8

_CLS_DAILY_PREFIX_RE = re.compile(r"^财联社\s*\d{1,2}月\d{1,2}日电[，,]?\s*")
_TRAILING_PUNCT_RE = re.compile(r"[。！？!?；;，,\s]+$")


def _normalize_brief_title(title: Any) -> str:
    """去掉财联社电头与尾部标点，用于同日重复快讯去重。"""

    clean = _CLS_DAILY_PREFIX_RE.sub("", str(title or "").strip())
    return _TRAILING_PUNCT_RE.sub("", clean).strip()


def _triage_payload(item: Mapping[str, Any]) -> Dict[str, Any]:
    """把一条搜索结果映射成 triage 需要的标题 / 摘要 / 正文。"""

    return {
        "title": str(item.get("title") or ""),
        "summary": str(item.get("snippet") or item.get("summary") or item.get("brief") or ""),
        "content": str(item.get("content") or ""),
    }


def _sort_key(item: Mapping[str, Any]) -> tuple:
    triage = item.get("news_triage") if isinstance(item.get("news_triage"), dict) else {}
    return (
        int(triage.get("priority") or 0),
        int(triage.get("score") or 0),
        len(triage.get("mapped_sw_l1") or ()),
    )


def annotate_and_sort_items(
    items: Sequence[Mapping[str, Any]],
    *,
    enabled: bool,
    name_matcher: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """给搜索结果附加 triage 字段并稳定排序；关闭时原样返回。"""

    rows = list(items or [])
    if not enabled or not rows:
        return rows

    annotated: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            annotated.append(item)
            continue
        triage: TriageResult = triage_news_item(_triage_payload(item), name_matcher=name_matcher)
        enriched = dict(item)
        enriched["news_triage"] = triage.to_dict()
        annotated.append(enriched)

    # sort 是稳定排序：同分条目保持 provider 原始顺序。
    annotated.sort(key=_sort_key, reverse=True)
    return annotated


def _triage_of(item: Mapping[str, Any]) -> Dict[str, Any]:
    value = item.get("news_triage")
    return value if isinstance(value, dict) else {}


def build_triage_summary(items: Sequence[Mapping[str, Any]], *, limit: int = TRIAGE_SUMMARY_TITLE_LIMIT) -> str:
    """一行式排序摘要，供工具输出与证据卡读取。"""

    parts: List[str] = []
    for item in list(items or [])[:limit]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        triage = _triage_of(item)
        channel = str(triage.get("channel") or "other")
        score = int(triage.get("score") or 0)
        parts.append(f"{title}（{channel}/{score}）")
    return "；".join(parts)


def build_news_report(items: Sequence[Mapping[str, Any]], *, limit: int = TRIAGE_REPORT_TITLE_LIMIT) -> str:
    """多行新闻报告，写入 ``search_comprehensive_intel`` 的 ``report`` 字段。

    该字段是既有下游（证据卡 / 消息面摘要）本来就在读取的输入槽位；
    这里只填内容，不改任何读取逻辑或判定公式。
    """

    lines: List[str] = []
    for index, item in enumerate(list(items or [])[:limit], start=1):
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        triage = _triage_of(item)
        channel = str(triage.get("channel") or "other")
        score = int(triage.get("score") or 0)
        direction = int(triage.get("direction") or 0)
        tag = "利多" if direction > 0 else ("利空" if direction < 0 else "中性")
        meta = "，".join(
            str(value).strip()
            for value in (item.get("source"), item.get("date"), item.get("published_date"))
            if str(value or "").strip()
        )
        suffix = f"（{meta}）" if meta else ""
        lines.append(f"{index}. [{channel}/{score}/{tag}] {title}{suffix}")
    return "\n".join(lines)


def build_market_env_brief(
    *,
    signal_date: Optional[str] = None,
    limit: int = TRIAGE_BRIEF_SCAN_LIMIT,
) -> str:
    """按 ``signal_date`` 聚合库中 ``priority>0`` 的 no_stock 新闻。

    日期口径用 ``signal_date``（自然日），不用 ``trade_date``：盘后新闻的
    ``trade_date`` 会滚到下一交易日，会漏掉“当日”消息。库或列不存在时返回空串。
    """

    try:
        from src.storage import DatabaseManager

        with DatabaseManager.get_instance().get_session() as session:
            target_date = signal_date or _latest_priority_signal_date(session)
            if not target_date:
                return ""
            rows = session.execute(
                text(
                    """
                    SELECT title, no_stock_triage_json
                    FROM raw_news_episodes
                    WHERE no_stock_triage_json IS NOT NULL
                      AND signal_date = :signal_date
                      AND json_extract(no_stock_triage_json, '$.priority') > 0
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                {"signal_date": target_date, "limit": max(1, int(limit or TRIAGE_BRIEF_SCAN_LIMIT))},
            ).fetchall()
    except Exception:
        logger.debug("no_stock triage market brief unavailable", exc_info=True)
        return ""

    if not rows:
        return ""

    by_channel: Dict[str, List[str]] = {}
    counts: Dict[str, int] = {}
    seen: set[str] = set()
    for title, triage_json in rows:
        clean = _normalize_brief_title(title)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        triage = _load_triage(triage_json)
        channel = str(triage.get("channel") or "other")
        counts[channel] = counts.get(channel, 0) + 1
        titles = by_channel.setdefault(channel, [])
        if len(titles) < TRIAGE_BRIEF_TITLES_PER_CHANNEL:
            titles.append(clean)

    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    lines = [f"当日外盘/宏观环境（{target_date}，no_stock 高优先级 {total} 条）："]
    for channel, count in ordered:
        titles = by_channel.get(channel) or []
        if not titles:
            continue
        lines.append(f"- {channel}({count})：" + "；".join(titles))
    lines.append("说明：仅用于新闻排序与背景参考，不参与评分、阈值和点位。")
    return "\n".join(lines)


def _latest_priority_signal_date(session: Any) -> str:
    row = session.execute(
        text(
            """
            SELECT MAX(signal_date)
            FROM raw_news_episodes
            WHERE no_stock_triage_json IS NOT NULL
              AND signal_date IS NOT NULL
              AND json_extract(no_stock_triage_json, '$.priority') > 0
            """
        )
    ).fetchone()
    value = row[0] if row else None
    return str(value)[:10] if value else ""


def _load_triage(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
