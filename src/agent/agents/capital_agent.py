# -*- coding: utf-8 -*-
"""CapitalAgent - capital-flow and market-participation specialist.

This agent owns capital-flow evidence for a single-stock run.  It does not
make a final trading decision; it produces a dimension-level opinion and
explicitly records when an instrument or provider cannot supply the data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class CapitalAgent(BaseAgent):
    """Gather stock, board and market capital-flow evidence."""

    agent_name = "capital"
    max_steps = 5
    tool_names = [
        "get_capital_flow",
        "get_chip_distribution",
        "get_board_capital_flow",
        "get_market_capital_flow",
        "get_tushare_moneyflow_mkt_dc",
        "get_northbound_capital_flow",
        "get_margin_trading_summary",
        "get_realtime_quote",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are a **Capital Flow Agent** specialising in A-shares, Hong Kong stocks,
and US equities.

Your task is to build the capital-flow dimension of a stock analysis.  You
produce evidence and a dimension signal only; a downstream Decision Agent
will combine it with technical, intelligence, and fundamental opinions.

## Required evidence
1. Call get_capital_flow for an A-share individual stock.  Respect its
   selected_flow_source, amount_unit, and main/net flow definitions.
2. Call get_chip_distribution when available and report its status.
3. Add board/market context with get_board_capital_flow and
   get_market_capital_flow when available.
4. For A-shares, use northbound and margin data only when the tool returns a
   supported result.  Skip them for HK/US stocks and mark the omission.

## Data integrity
- Never call all-order net flow "main-force" flow.  Preserve source and unit.
- Do not infer a numeric flow, trend, or confidence when the source is empty,
  stale, unsupported, or failed.
- quality is "ok" only when the stock-level flow and at least one supporting
  capital/chip signal are available; otherwise use "partial", "stale", or
  "unavailable".

## Output
Return only a JSON object:
{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence evidence-bound summary",
  "quality": "ok|partial|stale|unavailable",
  "capital_flow_signal": "inflow|outflow|neutral|not_available",
  "capital_metrics": {},
  "risk_alerts": [],
  "missing_evidence": []
}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        stock = f"{ctx.stock_code} ({ctx.stock_name})" if ctx.stock_name else ctx.stock_code
        return (
            f"Build the capital-flow evidence for stock **{stock}**.\n"
            "Use the available tools, distinguish supported from unavailable data, "
            "and return the required JSON contract."
        )

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[CapitalAgent] failed to parse capital JSON")
            return None

        parsed = _normalise_dimension_payload(parsed, default_quality="unavailable")
        ctx.set_data("capital_opinion", parsed)
        ctx.meta.setdefault("dimension_status", {})[self.agent_name] = {
            "quality": parsed["quality"],
            "missing_evidence": parsed["missing_evidence"],
        }
        for alert in parsed["risk_alerts"]:
            ctx.add_risk_flag(category="capital", description=alert)

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed["signal"],
            confidence=_to_confidence(parsed.get("confidence")),
            reasoning=parsed["reasoning"],
            raw_data=parsed,
        )


def _normalise_dimension_payload(payload: Dict[str, Any], default_quality: str) -> Dict[str, Any]:
    """Keep downstream contracts stable when the model omits optional keys."""
    result = dict(payload)
    result["signal"] = result.get("signal") or "hold"
    result["reasoning"] = str(result.get("reasoning") or "")
    quality = str(result.get("quality") or default_quality).lower()
    result["quality"] = quality if quality in {"ok", "partial", "stale", "unavailable"} else default_quality
    result["risk_alerts"] = _as_string_list(result.get("risk_alerts"))
    result["missing_evidence"] = _as_string_list(result.get("missing_evidence"))
    result.setdefault("capital_flow_signal", "not_available")
    result.setdefault("capital_metrics", {})
    return result


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _to_confidence(value: Any) -> float:
    try:
        return float(value if value is not None else 0.3)
    except (TypeError, ValueError):
        return 0.3

