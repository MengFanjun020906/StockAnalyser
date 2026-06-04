# -*- coding: utf-8 -*-
"""Theme-catalyst thesis desk expert (主题催化席)."""

from __future__ import annotations

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
    "get_stock_business_context",
    "get_stockapi_hot_sectors",
    "get_stockapi_hot_sector_leaders",
    "get_stockapi_hot_money_activity",
    "get_capital_flow",
    "get_realtime_quote",
    "get_volume_analysis",
    "analyze_price_structure",
    "search_stock_news",
    "score_stock_news_sentiment",
)

_ELIGIBLE_KINDS = {"news", "sector", "capital", "limit"}
_ELIGIBLE_SOURCES = {"news_theme_daily", "sector_theme", "hot_rank", "limit_up_pool"}


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

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Eligibility: news/theme source, news/sector flag, or catalyst clue."""

        primary: List[FeatureRow] = []
        fallback_candidates: List[FeatureRow] = []

        for row in rows:
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

            fs = row.fact_sheet
            if fs is not None and fs.sector_strength == "strong":
                fallback_candidates.append(row)

        if primary:
            return primary
        return fallback_candidates[: self._fallback_supplement_n]
