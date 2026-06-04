# -*- coding: utf-8 -*-
"""Momentum thesis desk expert (动量席 — trend continuation + limit-up)."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.experts.base import LLMCallable
from src.agent.candidate_experts_v2.experts.base import _registry_lookup
from src.agent.candidate_experts_v2.prompts.momentum_desk import (
    build_momentum_desk_system_prompt,
)
from src.agent.candidate_experts_v2.schemas import FeatureRow
from src.agent.candidate_experts_v2.tools_manifest import load_manifest, validate_manifest


MOMENTUM_DESK_TOOLS: tuple[str, ...] = (
    "get_realtime_quote",
    "analyze_trend",
    "analyze_price_structure",
    "get_volume_analysis",
    "get_capital_flow",
    "get_tushare_moneyflow_dc",
    "get_tushare_limit_list_d",
    "get_tushare_limit_step",
    "get_tushare_dragon_tiger_list",
    "get_tushare_dragon_tiger_inst",
    "get_tushare_hot_rank",
    "get_sector_rankings",
    "get_stock_info",
)

# FeatureFlag kinds that signal momentum eligibility
_ELIGIBLE_KINDS = {"limit", "capital", "pattern"}


class MomentumDeskExpert(BaseDeskExpert):
    """动量席 — trend continuation and real capital-momentum (limit-up) plays."""

    expert_name = "momentum_desk"
    dimension = "momentum"

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
        manifest = load_manifest("momentum_desk")
        if any(_registry_lookup(tool_registry, n) is not None for n in MOMENTUM_DESK_TOOLS):
            validate_manifest(manifest, whitelist=MOMENTUM_DESK_TOOLS, tool_registry=tool_registry)
        system_prompt = build_momentum_desk_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=MOMENTUM_DESK_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            fallback_supplement_n=fallback_supplement_n,
        )

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Eligibility: limit/capital/pattern flag OR bullish trend OR high volume."""
        primary: List[FeatureRow] = []
        fallback_candidates: List[FeatureRow] = []

        for row in rows:
            fs = row.fact_sheet
            flag_kinds = {f.kind for f in row.flags}

            # Criterion 1: has limit/capital/pattern flag
            if flag_kinds & _ELIGIBLE_KINDS:
                primary.append(row)
                continue

            # Criterion 2: FactSheet shows bullish trend
            if fs is not None and fs.trend_state == "bullish":
                primary.append(row)
                continue

            # Criterion 3: high volume ratio or strong 5d gain
            if fs is not None:
                vr = fs.volume_ratio
                gain = fs.gain_5d
                if (vr is not None and vr >= 1.5) or (gain is not None and gain >= 5.0):
                    primary.append(row)
                    continue

            fallback_candidates.append(row)

        if primary:
            return primary

        return fallback_candidates[: self._fallback_supplement_n]
