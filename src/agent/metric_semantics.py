# -*- coding: utf-8 -*-
"""Compact metric semantics injected into model-facing tool fact cards.

The goal is not to explain every field. Only high-risk financial metrics whose
names are easy to misread get a short semantic guardrail in the LLM context.
Longer source documentation belongs in docs and traces, not in every prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping


@dataclass(frozen=True)
class MetricSemanticSpec:
    """One tool-level semantic contract for compact model context."""

    ref: str
    risk_level: str
    fields: Mapping[str, str]


_SEMANTIC_REGISTRY: Dict[str, MetricSemanticSpec] = {
    "get_capital_flow": MetricSemanticSpec(
        ref="capital_flow.v1",
        risk_level="P0",
        fields={
            "main_net_inflow": "主力口径=(buy_lg_amount+buy_elg_amount-sell_lg_amount-sell_elg_amount)*10000, CNY",
            "main_inflow_5d": "5日主力口径累计, CNY",
            "main_inflow_10d": "10日主力口径累计, CNY",
            "net_inflow": "Tushare net_mf_amount全口径主动净流入*10000, CNY; 不等于主力资金",
            "net_inflow_5d": "5日全口径主动净流入累计, CNY; 不等于主力资金",
            "net_inflow_10d": "10日全口径主动净流入累计, CNY; 不等于主力资金",
        },
    ),
    "get_chip_distribution": MetricSemanticSpec(
        ref="chip_distribution.v1",
        risk_level="P0",
        fields={
            "profit_ratio": "筹码获利比例，必须来自 get_chip_distribution 数据源；缺失时不能估算",
            "avg_cost": "筹码平均成本，必须来自 get_chip_distribution 数据源；缺失时不能估算",
            "cost_90": "90%筹码成本区间，必须来自 get_chip_distribution 数据源；缺失时不能估算",
            "winner_rate": "筹码获利比例同义字段，必须来自数据源；缺失时不能估算",
        },
    ),
}


def apply_metric_semantics(tool_name: str, compact_payload: Any) -> Any:
    """Attach minimal semantics for high-risk fields present in a compact payload."""

    if not isinstance(compact_payload, dict):
        return compact_payload
    spec = _SEMANTIC_REGISTRY.get(tool_name)
    if spec is None:
        return compact_payload

    visible_fields = {
        field: text
        for field, text in spec.fields.items()
        if field in compact_payload and compact_payload.get(field) not in (None, "", [], {})
    }
    if not visible_fields:
        return compact_payload

    out = dict(compact_payload)
    out["semantic_ref"] = spec.ref
    out["semantic_risk_level"] = spec.risk_level
    out["field_semantics"] = visible_fields
    return out


def registered_metric_semantics() -> Dict[str, MetricSemanticSpec]:
    """Return registered semantic specs for tests and future diagnostics."""

    return dict(_SEMANTIC_REGISTRY)
