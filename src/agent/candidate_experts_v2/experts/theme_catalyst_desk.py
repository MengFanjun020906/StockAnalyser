# -*- coding: utf-8 -*-
"""Theme-catalyst thesis desk expert (主题催化席)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.base import LLMCallable, _registry_lookup
from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.prompts.theme_catalyst_desk import (
    build_theme_catalyst_desk_system_prompt,
)
from src.agent.candidate_experts_v2.schemas import FeatureRow
from src.agent.candidate_experts_v2.tools_manifest import load_manifest, validate_manifest


THEME_CATALYST_DESK_TOOLS: tuple[str, ...] = (
    "get_eastmoney_cjzc_daily",
    "get_cls_telegraph_news",
    "get_xueqiu_hot_news",
    "get_macro_finance_news",
    "get_stock_business_context",
    "get_stock_disclosure_events",
    "get_tushare_announcements",
    "get_stockapi_hot_sectors",
    "get_stockapi_sector_constituents",
    "get_stockapi_sector_flow_history",
    "get_board_capital_flow",
    "get_tushare_moneyflow_ind_dc",
    "get_tushare_moneyflow_ind_ths",
    "get_tushare_moneyflow_cnt_ths",
    "get_stockapi_hot_sector_leaders",
    "get_stockapi_hot_money_activity",
    "get_stockapi_limit_up_pool",
    "get_stockapi_popularity_rank",
    "get_tushare_moneyflow_mkt_dc",
    "get_capital_flow",
    "get_realtime_quote",
    "get_volume_analysis",
    "get_tushare_stk_factor",
    "analyze_price_structure",
    "get_symbol_regime_probability",
    "search_stock_news",
    "search_openinvest_news",
    "search_comprehensive_intel",
    "score_stock_news_sentiment",
)

_ELIGIBLE_KINDS = {"news", "sector"}
_ELIGIBLE_SOURCES = {"news_theme_daily", "sector_theme"}
_RAW_TEXT_KEYS = {
    "raw",
    "raw_text",
    "raw_html",
    "raw_content",
    "content",
    "content_body",
    "ContentBody",
    "article_body",
    "body",
    "text",
    "html",
    "full_text",
    "original",
    "paragraphs",
}
_SUMMARY_KEYS = {
    "status",
    "code",
    "name",
    "date",
    "target_date",
    "as_of",
    "source",
    "source_chain",
    "article_fetch_status",
    "title",
    "summary",
    "evidence_section",
    "matched_keywords",
    "high_impact_terms",
    "theme",
    "themes",
    "industry",
    "boards",
    "business_summary",
    "company_events",
    "announcements",
    "items",
    "results",
    "news",
    "events",
    "data",
    "error",
    "reason",
    "url",
    "published_at",
    "published_date",
    "publish_time",
}
_EVIDENCE_FOCUS = [
    "product_export",
    "domestic_substitution_policy",
    "business_fit",
    "funding_validation",
    "company_negative",
    "unrelated_noise",
]
_TECH_CHAIN_TERMS = {
    "ai",
    "aigc",
    "人工智能",
    "算力",
    "服务器",
    "数据中心",
    "液冷",
    "芯片",
    "半导体",
    "晶圆",
    "先进封装",
    "封测",
    "存储",
    "dram",
    "nand",
    "hbm",
    "mlcc",
    "被动元件",
    "电子元件",
    "陶瓷电容",
    "电容",
    "电感",
    "pcb",
    "ccl",
    "光模块",
    "cpo",
    "光通信",
    "光芯片",
    "光器件",
    "连接器",
    "消费电子",
    "机器人",
    "智能驾驶",
    "车规",
    "国产替代",
    "信创",
}


class ThemeCatalystDeskExpert(BaseDeskExpert):
    """主题催化席 — validates news/theme catalysts against business fit and money flow."""

    expert_name = "theme_catalyst_desk"
    dimension = "theme_catalyst"

    def __init__(
        self,
        *,
        tool_registry: Dict[str, Any],
        tool_decls: Sequence[Dict[str, Any]],
        llm: LLMCallable,
        max_llm_rounds: int = 5,
        max_tool_calls: int = 10,
        prompt_variables: Optional[Mapping[str, Any]] = None,
        fallback_supplement_n: int = 10,
    ) -> None:
        manifest = load_manifest("theme_catalyst_desk")
        if any(_registry_lookup(tool_registry, n) is not None for n in THEME_CATALYST_DESK_TOOLS):
            validate_manifest(
                manifest,
                whitelist=THEME_CATALYST_DESK_TOOLS,
                tool_registry=tool_registry,
            )
        system_prompt = build_theme_catalyst_desk_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=THEME_CATALYST_DESK_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            fallback_supplement_n=fallback_supplement_n,
        )

    def _tool_result_for_model(self, tool_name: str, result_payload: Any) -> Any:
        return _compact_theme_tool_result(tool_name, result_payload)

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Eligibility: AI/tech-chain news/theme candidates only."""

        primary: List[FeatureRow] = []

        for row in rows:
            if not _is_ai_tech_chain_row(row):
                continue
            sources = set(str(item) for item in (row.recall_sources or []) if item)
            flag_kinds = {flag.kind for flag in row.flags}
            flag_detectors = " ".join(str(flag.detector or "") for flag in row.flags)
            flag_summaries = " ".join(str(flag.summary or "") for flag in row.flags)

            if sources & _ELIGIBLE_SOURCES:
                primary.append(row)
                continue
            if flag_kinds & _ELIGIBLE_KINDS:
                primary.append(row)
                continue
            if "news_theme_daily" in flag_detectors or "主题" in flag_summaries or "催化" in flag_summaries:
                primary.append(row)
                continue

        return primary

    def _ineligible_row_reason(self, row: FeatureRow) -> str:
        tech_note = "" if _is_ai_tech_chain_row(row) else "未识别为 AI/科技产业链候选，跳过新闻席位"
        sources = set(str(item) for item in (row.recall_sources or []) if item)
        flag_kinds = {flag.kind for flag in row.flags}
        flag_detectors = " ".join(str(flag.detector or "") for flag in row.flags)
        flag_summaries = " ".join(str(flag.summary or "") for flag in row.flags)
        source_note = "" if sources & _ELIGIBLE_SOURCES else "召回来源未命中 news_theme_daily/sector_theme"
        flag_note = "" if flag_kinds & _ELIGIBLE_KINDS else "flags 未命中 news/sector"
        clue_note = (
            ""
            if ("news_theme_daily" in flag_detectors or "主题" in flag_summaries or "催化" in flag_summaries)
            else "detector/summary 未出现主题或催化线索"
        )
        parts = [item for item in (tech_note, source_note, flag_note, clue_note) if item]
        return "主题催化席未入席：" + "；".join(parts) + "。"


def _is_ai_tech_chain_row(row: FeatureRow) -> bool:
    """Return True only for rows with explicit AI/tech-chain textual evidence."""

    texts: List[str] = [row.name or "", row.code or ""]
    for source in row.recall_sources or []:
        texts.append(str(source or ""))
    fs = row.fact_sheet
    if fs is not None:
        texts.extend([
            fs.sector_name or "",
            " ".join(str(item) for item in (fs.warnings or [])),
            " ".join(str(item) for item in (fs.hard_risk_flags or [])),
        ])
    seed_fact = getattr(row, "seed_fact", None)
    if seed_fact is not None:
        try:
            texts.append(seed_fact.model_dump_json())
        except Exception:
            texts.append(str(seed_fact))
    for flag in row.flags:
        texts.extend([
            flag.detector or "",
            flag.kind or "",
            flag.summary or "",
            str(flag.metrics or ""),
        ])
    haystack = " ".join(texts).lower()
    return any(_term_matches(haystack, term) for term in _TECH_CHAIN_TERMS)


def _term_matches(haystack: str, term: str) -> bool:
    lowered = term.lower()
    if re.fullmatch(r"[a-z0-9]+", lowered):
        return re.search(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", haystack) is not None
    return lowered in haystack


def _compact_theme_tool_result(tool_name: str, payload: Any) -> Dict[str, Any]:
    omitted: List[str] = []
    compact = _compact_theme_value(payload, depth=0, omitted=omitted)
    return {
        "context_policy": "theme_catalyst_summary_card",
        "tool": tool_name,
        "evidence_focus": _EVIDENCE_FOCUS,
        "summary_contract": (
            "Summarize each item into product-category export, domestic-substitution policy, "
            "business fit, funding validation, negative company event, or unrelated noise. "
            "Do not reason from long raw article text."
        ),
        "result": compact,
        "omitted_raw_fields": sorted(set(omitted))[:20],
    }


def _compact_theme_value(value: Any, *, depth: int, omitted: List[str]) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _truncate_theme_text(value)
    if isinstance(value, list):
        limit = 6 if depth <= 2 else 4
        compact_items = [
            _compact_theme_value(item, depth=depth + 1, omitted=omitted)
            for item in value[:limit]
        ]
        if len(value) > limit:
            compact_items.append({"omitted_count": len(value) - limit})
        return compact_items
    if isinstance(value, Mapping):
        compact: Dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str in _RAW_TEXT_KEYS or key_str.lower() in _RAW_TEXT_KEYS:
                omitted.append(key_str)
                continue
            if depth >= 2 and key_str not in _SUMMARY_KEYS:
                if _looks_like_verbose_text(item):
                    omitted.append(key_str)
                    continue
            compact[key_str] = _compact_theme_value(item, depth=depth + 1, omitted=omitted)
            if len(compact) >= 32:
                omitted.append("__extra_keys__")
                break
        return compact
    return _truncate_theme_text(str(value))


def _looks_like_verbose_text(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > 500
    if isinstance(value, list):
        return len(value) > 12
    if isinstance(value, Mapping):
        return len(value) > 24
    return False


def _truncate_theme_text(text: str, limit: int = 360) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "...[truncated]"
