# -*- coding: utf-8 -*-
"""Reversal-structure thesis desk expert (结构反转席, code key early_turn_desk)."""

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
    "get_tushare_stk_factor",
    "get_volume_analysis",
    "get_capital_flow",
    "get_tushare_moneyflow_mkt_dc",
    "get_chip_distribution",
    "get_stock_info",
)

# FactSheet flag/source kinds that mark a row as having explicit turn evidence.
_TURN_EVIDENCE_KINDS = {"pattern", "capital"}
_TURN_EVIDENCE_SOURCES = {"low_base_structure"}
# range_pct_120 ceiling (None = data missing → eligible via fallback)
_LOW_BASE_RANGE_PCT_MAX = 0.45


class EarlyTurnDeskExpert(BaseDeskExpert):
    """结构反转席 — low range only participates with explicit turn evidence."""

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
        """Eligibility: range_pct_120 low AND explicit turn evidence.

        Low position alone no longer qualifies.  The code key stays
        early_turn_desk for compatibility, while the business role is
        reversal_structure_desk.
        """
        primary: List[FeatureRow] = []

        for row in rows:
            fs = row.fact_sheet
            if fs is None or fs.range_pct_120 is None:
                continue
            if fs.range_pct_120 > self._low_base_range_pct_max:
                continue

            sources = set(row.recall_sources)
            flag_kinds = {f.kind for f in row.flags}
            has_turn_evidence = bool(
                sources & _TURN_EVIDENCE_SOURCES
                or flag_kinds & _TURN_EVIDENCE_KINDS
            )
            if has_turn_evidence:
                primary.append(row)

        return primary

    def _ineligible_row_reason(self, row: FeatureRow) -> str:
        fs = row.fact_sheet
        if fs is None or fs.range_pct_120 is None:
            return "低位启动席需要 range_pct_120 位置分位，当前事实包缺少该字段。"
        if fs.range_pct_120 > self._low_base_range_pct_max:
            return (
                "低位启动席只看低位反转，当前 range_pct_120="
                f"{fs.range_pct_120:.3f} 高于阈值 {self._low_base_range_pct_max:.3f}。"
            )
        sources = set(row.recall_sources)
        flag_kinds = {f.kind for f in row.flags}
        if not (sources & _TURN_EVIDENCE_SOURCES or flag_kinds & _TURN_EVIDENCE_KINDS):
            return "低位启动席要求低位且有反转证据，当前缺少 pattern/capital 或 low_base_structure 证据。"
        return "低位启动席过滤条件未命中。"
