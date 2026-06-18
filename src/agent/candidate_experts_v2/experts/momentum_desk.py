# -*- coding: utf-8 -*-
"""Trend-continuation thesis desk expert (趋势/形态延续席)."""

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
    "get_tushare_stk_factor",
    "analyze_price_structure",
    "get_volume_analysis",
    "get_capital_flow",
    "get_tushare_moneyflow_mkt_dc",
    "get_market_capital_flow",
    "get_northbound_capital_flow",
    "get_margin_trading_summary",
    "get_tushare_moneyflow_dc",
    "get_tushare_moneyflow_ths",
    "get_tushare_limit_list_d",
    "get_tushare_limit_step",
    "get_stockapi_limit_up_pool",
    "get_tushare_dragon_tiger_list",
    "get_tushare_dragon_tiger_inst",
    "get_tushare_hot_rank",
    "get_stockapi_popularity_rank",
    "get_sector_rankings",
    "get_board_capital_flow",
    "get_tushare_moneyflow_ind_dc",
    "get_tushare_moneyflow_ind_ths",
    "get_tushare_moneyflow_cnt_ths",
    "get_symbol_regime_probability",
    "get_stock_info",
)

# FeatureFlag/source kinds that signal trend-continuation eligibility.
_ELIGIBLE_KINDS = {"limit", "capital", "pattern"}
_ELIGIBLE_SOURCES = {"sequoia", "alphasift", "limit_up_pool", "capital_flow_anomaly"}


class MomentumDeskExpert(BaseDeskExpert):
    """趋势/形态延续席 — trend continuation and real capital-momentum plays."""

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
        """Eligibility: strategy/limit/capital source OR bullish trend OR high volume."""
        primary: List[FeatureRow] = []
        fallback_candidates: List[FeatureRow] = []

        for row in rows:
            fs = row.fact_sheet
            flag_kinds = {f.kind for f in row.flags}
            sources = set(row.recall_sources)

            # Criterion 1: main momentum recall sources.
            if sources & _ELIGIBLE_SOURCES:
                primary.append(row)
                continue

            # Criterion 2: has limit/capital/pattern flag.
            if flag_kinds & _ELIGIBLE_KINDS:
                primary.append(row)
                continue

            # Criterion 3: FactSheet shows bullish trend.
            if fs is not None and fs.trend_state == "bullish":
                primary.append(row)
                continue

            # Criterion 4: high volume ratio or strong 5d gain.
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

    def _ineligible_row_reason(self, row: FeatureRow) -> str:
        fs = row.fact_sheet
        sources = set(row.recall_sources)
        flag_kinds = {f.kind for f in row.flags}
        if not (sources & _ELIGIBLE_SOURCES):
            source_note = "召回来源未命中 sequoia/alphasift/limit_up_pool/capital_flow_anomaly"
        else:
            source_note = ""
        if not (flag_kinds & _ELIGIBLE_KINDS):
            flag_note = "flags 未命中 limit/capital/pattern"
        else:
            flag_note = ""
        trend_note = ""
        volume_note = ""
        if fs is None:
            trend_note = "缺少 FactSheet，无法确认 bullish 趋势"
            volume_note = "缺少 FactSheet，无法确认量比或 5 日涨幅"
        else:
            if fs.trend_state != "bullish":
                trend_note = f"趋势状态为 {fs.trend_state or 'unknown'}，不是 bullish"
            vr = fs.volume_ratio
            gain = fs.gain_5d
            if not ((vr is not None and vr >= 1.5) or (gain is not None and gain >= 5.0)):
                volume_note = f"量比 {vr if vr is not None else '缺失'}、5日涨幅 {gain if gain is not None else '缺失'} 未达动量阈值"
        parts = [item for item in (source_note, flag_note, trend_note, volume_note) if item]
        return "动量席未入席：" + "；".join(parts) + "。"
