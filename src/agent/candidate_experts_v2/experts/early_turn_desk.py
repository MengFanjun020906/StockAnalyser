# -*- coding: utf-8 -*-
"""Early-turn thesis desk expert (低位启动席)."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.experts.base import LLMCallable
from src.agent.candidate_experts_v2.experts.base import _registry_lookup
from src.agent.candidate_experts_v2.prompts.early_turn_desk import (
    build_early_turn_desk_system_prompt,
)
from src.agent.candidate_experts_v2.schemas import FeatureRow
from src.agent.candidate_experts_v2.tools_manifest import load_manifest, validate_manifest


EARLY_TURN_DESK_TOOLS: tuple[str, ...] = (
    "get_realtime_quote",
    "analyze_trend",
    "analyze_price_structure",
    "analyze_pattern",
    "calculate_ma",
    "get_volume_analysis",
    "get_capital_flow",
    "get_chip_distribution",
    "get_stock_info",
)

# FactSheet flag kinds that mark a row as early-turn eligible
_ELIGIBLE_KINDS = {"position", "pattern", "capital"}
_ELIGIBLE_SOURCES = {"low_base_structure", "alphasift", "sequoia"}
# range_pct_120 ceiling (None = data missing → eligible via fallback)
_LOW_BASE_RANGE_PCT_MAX = 0.45


class EarlyTurnDeskExpert(BaseDeskExpert):
    """低位启动席 — identifies low-base stocks with an early turn signal."""

    expert_name = "early_turn_desk"
    dimension = "early_turn"

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
        low_base_range_pct_max: float = _LOW_BASE_RANGE_PCT_MAX,
    ) -> None:
        manifest = load_manifest("early_turn_desk")
        if any(_registry_lookup(tool_registry, n) is not None for n in EARLY_TURN_DESK_TOOLS):
            validate_manifest(manifest, whitelist=EARLY_TURN_DESK_TOOLS, tool_registry=tool_registry)
        system_prompt = build_early_turn_desk_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=EARLY_TURN_DESK_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            fallback_supplement_n=fallback_supplement_n,
        )
        self._low_base_range_pct_max = low_base_range_pct_max

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Eligibility: low-base position OR low-base-type source flag OR fallback."""
        primary: List[FeatureRow] = []
        fallback_candidates: List[FeatureRow] = []

        for row in rows:
            fs = row.fact_sheet
            # Criterion 1: position low by FactSheet range
            if fs is not None and fs.range_pct_120 is not None:
                if fs.range_pct_120 <= self._low_base_range_pct_max:
                    primary.append(row)
                    continue

            # Criterion 2: has low-base source or position/pattern/capital flag
            sources = set(row.recall_sources)
            if sources & _ELIGIBLE_SOURCES:
                primary.append(row)
                continue
            flag_kinds = {f.kind for f in row.flags}
            if flag_kinds & _ELIGIBLE_KINDS:
                primary.append(row)
                continue

            # Criterion 3: range_pct_120 missing → data gap, keep as fallback
            if fs is None or fs.range_pct_120 is None:
                fallback_candidates.append(row)

        if primary:
            return primary

        # Nothing passed primary filters → use fallback supplement
        return fallback_candidates[: self._fallback_supplement_n]
