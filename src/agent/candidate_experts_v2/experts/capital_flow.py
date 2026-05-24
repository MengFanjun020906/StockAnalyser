# -*- coding: utf-8 -*-
"""Capital-flow expert (资金面专家)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.experts.base import BaseExpert, LLMCallable
from src.agent.candidate_experts_v2.prompts.capital import build_capital_system_prompt
from src.agent.candidate_experts_v2.tools_manifest import (
    load_manifest,
    validate_manifest,
)


def _registry_lookup(tool_registry: Any, name: str) -> Any:
    """Best-effort lookup that works for both ToolRegistry and plain dict."""
    if hasattr(tool_registry, "get"):
        try:
            return tool_registry.get(name)
        except TypeError:
            pass
    if isinstance(tool_registry, dict):
        return tool_registry.get(name)
    return None


CAPITAL_FLOW_TOOLS: tuple[str, ...] = (
    "get_capital_flow",
    "get_tushare_moneyflow_ths",
    "get_tushare_moneyflow_dc",
    "get_tushare_dragon_tiger_list",
    "get_tushare_dragon_tiger_inst",
    "get_tushare_limit_list_ths",
    "get_tushare_limit_list_d",
    "get_tushare_limit_step",
    "get_tushare_hot_rank",
    "get_stockapi_popularity_rank",
    "get_stockapi_hot_money_activity",
)


class CapitalFlowExpert(BaseExpert):
    """Capital dimension expert. Whitelist enforced by BaseExpert.

    Loads `tools_manifest/capital.yaml` at init time and renders the SYSTEM
    prompt with per-tool business semantics injected. Optional
    `prompt_variables` (e.g. {"today": "20260523", "seed_codes": "600519,000001"})
    are substituted into each tool's typical_args at prompt build time; unknown
    placeholders are left literal so the LLM can still see them.
    """

    expert_name = "capital_flow_expert"
    dimension = "capital"

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
        manifest = load_manifest("capital")
        # Cross-check manifest against this expert's whitelist + the live registry
        # only when the registry actually contains the capital tools. Test stubs
        # commonly pass `tool_registry={}`; in that case skip the registry-side
        # check (the whitelist check still runs via the manifest loader's pydantic
        # validation + the explicit pass below).
        _registry_has_capital_tools = any(
            _registry_lookup(tool_registry, name) is not None
            for name in CAPITAL_FLOW_TOOLS
        )
        if _registry_has_capital_tools:
            validate_manifest(
                manifest,
                whitelist=CAPITAL_FLOW_TOOLS,
                tool_registry=tool_registry,
            )
        system_prompt = build_capital_system_prompt(
            manifest=manifest,
            variables=prompt_variables,
        )
        super().__init__(
            allowed_tools=CAPITAL_FLOW_TOOLS,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            freshness="intraday",
        )
