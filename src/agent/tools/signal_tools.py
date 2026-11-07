# -*- coding: utf-8 -*-
"""Deprecated compatibility surface for the former LLM price-plan tool."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter
def signal_trade_plan_instructions() -> str:
    """Return the shared sidecar policy for single-stock decision stages."""
    return """
## 确定性价格权限

- 新请求的结构化交易计划由确定性价格引擎创建，模型不提交价格计划。
- 不得自行填写或修改入场价、买点区间、止损价和目标价。只能给出定性证据与风险意见。
""".strip()


def _handle_submit_trade_plan(
    symbol: str,
    action: str,
    entry_zone_low: float,
    entry_zone_high: float,
    entry_zones: Optional[List[Dict[str, Any]]] = None,
    current_action: Optional[str] = None,
    stop_loss_price: Optional[float] = None,
    invalidation_conditions: Optional[List[str]] = None,
    take_profit_price: Optional[float] = None,
    target_position_pct: Optional[float] = None,
    execution_window: Optional[str] = None,
) -> dict:
    """Reject legacy calls so an explicitly registered tool cannot restore price authority."""
    return {
        "status": "failed",
        "error": "submit_trade_plan is disabled; deterministic_price_plan is the only price authority",
    }


submit_trade_plan_tool = ToolDefinition(
    name="submit_trade_plan",
    description=(
        "Deprecated compatibility tool for historical traces. Do not call it for new analyses; "
        "the deterministic price engine owns all entry, stop-loss and target prices."
    ),
    parameters=[
        ToolParameter(name="symbol", type="string", description="Single stock symbol being analyzed."),
        ToolParameter(name="action", type="string", description="Trigger action after price enters the plan zone.", enum=["open", "add"]),
        ToolParameter(name="current_action", type="string", description="Current recommendation before the price condition is met.", enum=["open", "add", "wait", "reject"], required=False),
        ToolParameter(name="entry_zone_low", type="number", description="Lower bound of the evidence-backed entry zone."),
        ToolParameter(name="entry_zone_high", type="number", description="Upper bound of the evidence-backed entry zone."),
        ToolParameter(
            name="entry_zones",
            type="array",
            description="All disjoint evidence-backed zones in priority order; keep ideal and secondary zones separate.",
            required=False,
            items={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Zone label such as ideal or secondary."},
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["label", "low", "high"],
            },
        ),
        ToolParameter(name="stop_loss_price", type="number", description="Evidence-backed stop-loss price.", required=False),
        ToolParameter(name="invalidation_conditions", type="array", description="Concrete conditions that invalidate the plan.", required=False),
        ToolParameter(name="take_profit_price", type="number", description="Optional take-profit reference.", required=False),
        ToolParameter(name="target_position_pct", type="number", description="Optional target position percentage from 0 to 100.", required=False),
        ToolParameter(name="execution_window", type="string", description="Optional human-readable execution window.", required=False),
    ],
    handler=_handle_submit_trade_plan,
    category="action",
)


# Deliberately empty: this module remains import-compatible for old integrations,
# but the former LLM-authored price tool must never be bulk-registered.
ALL_SIGNAL_TOOLS: List[ToolDefinition] = []


__all__ = ["ALL_SIGNAL_TOOLS", "signal_trade_plan_instructions", "submit_trade_plan_tool"]
