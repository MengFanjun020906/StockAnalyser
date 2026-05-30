# -*- coding: utf-8 -*-
"""Quality-repair thesis desk expert (质量修复席)."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.experts.base import LLMCallable
from src.agent.candidate_experts_v2.experts.base import _registry_lookup
from src.agent.candidate_experts_v2.prompts.quality_repair_desk import (
    build_quality_repair_desk_system_prompt,
)
from src.agent.candidate_experts_v2.schemas import FeatureRow
from src.agent.candidate_experts_v2.tools_manifest import load_manifest, validate_manifest


QUALITY_REPAIR_DESK_TOOLS: tuple[str, ...] = (
    "get_stock_info",
    "get_tushare_daily_basic",
    "get_tushare_financial_indicators",
    "get_tushare_financial_statements",
    "get_tushare_forecast",
    "get_tushare_express",
    "get_tushare_dividend",
    "analyze_trend",
    "analyze_price_structure",
)

# FeatureFlag kinds that signal quality-repair eligibility
_ELIGIBLE_KINDS = {"fundamental"}


class QualityRepairDeskExpert(BaseDeskExpert):
    """质量修复席 — finds stocks with improving fundamentals at undemanding prices."""

    expert_name = "quality_repair_desk"
    dimension = "quality"

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
        manifest = load_manifest("quality_repair_desk")
        if any(_registry_lookup(tool_registry, n) is not None for n in QUALITY_REPAIR_DESK_TOOLS):
            validate_manifest(
                manifest, whitelist=QUALITY_REPAIR_DESK_TOOLS, tool_registry=tool_registry
            )
        system_prompt = build_quality_repair_desk_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=QUALITY_REPAIR_DESK_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            fallback_supplement_n=fallback_supplement_n,
        )

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Eligibility: fundamental flag OR low-valuation signal from FactSheet."""
        primary: List[FeatureRow] = []
        fallback_candidates: List[FeatureRow] = []

        for row in rows:
            fs = row.fact_sheet
            flag_kinds = {f.kind for f in row.flags}
            sources = set(row.recall_sources)

            # Criterion 1: has fundamental flag
            if flag_kinds & _ELIGIBLE_KINDS:
                primary.append(row)
                continue

            # Criterion 2: source is fundamental_snapshot
            if "fundamental_snapshot" in sources:
                primary.append(row)
                continue

            # Criterion 3: FactSheet shows low position (potential value play)
            if fs is not None and fs.range_pct_120 is not None and fs.range_pct_120 <= 0.35:
                primary.append(row)
                continue

            fallback_candidates.append(row)

        if primary:
            return primary

        return fallback_candidates[: self._fallback_supplement_n]
