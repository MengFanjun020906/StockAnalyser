# -*- coding: utf-8 -*-
"""Early-turn / low-base breakout expert (低位启动专家)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.base import BaseExpert, LLMCallable
from src.agent.candidate_experts_v2.prompts.early_turn import build_early_turn_system_prompt
from src.agent.candidate_experts_v2.tools_manifest import (
    load_manifest,
    validate_manifest,
)
from src.agent.candidate_experts_v2.experts.capital_flow import _registry_lookup


EARLY_TURN_TOOLS: tuple[str, ...] = (
    "get_realtime_quote",
    "analyze_trend",
    "calculate_ma",
    "get_volume_analysis",
    "analyze_price_structure",
    "get_capital_flow",
    "get_stock_info",
)


class EarlyTurnExpert(BaseExpert):
    """Low-base / early-turn dimension expert."""

    expert_name = "early_turn_expert"
    dimension = "early_turn"

    def __init__(
        self,
        *,
        tool_registry: Dict[str, Any],
        tool_decls: Sequence[Dict[str, Any]],
        llm: LLMCallable,
        max_llm_rounds: int = 3,
        max_tool_calls: int = 6,
        prompt_variables: Optional[Mapping[str, Any]] = None,
    ) -> None:
        manifest = load_manifest("early_turn")
        _registry_has_tools = any(
            _registry_lookup(tool_registry, name) is not None
            for name in EARLY_TURN_TOOLS
        )
        if _registry_has_tools:
            validate_manifest(
                manifest,
                whitelist=EARLY_TURN_TOOLS,
                tool_registry=tool_registry,
            )
        system_prompt = build_early_turn_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=EARLY_TURN_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            freshness="intraday",
        )
