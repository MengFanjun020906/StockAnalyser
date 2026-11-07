# -*- coding: utf-8 -*-
"""FundamentalAgent - valuation and financial-quality specialist."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class FundamentalAgent(BaseAgent):
    """Gather company, valuation, financial-statement and quality evidence."""

    agent_name = "fundamental"
    max_steps = 5
    tool_names = [
        "get_stock_business_context",
        "get_tushare_daily_basic",
        "get_tushare_financial_indicators",
        "get_tushare_financial_statements",
        "get_tushare_forecast",
        "search_stock_fundamentals",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are a **Fundamental Analysis Agent** specialising in A-shares, Hong Kong
stocks, and US equities.

Your task is to build the fundamental dimension of a stock analysis.  Gather
company context, valuation, profitability, balance-sheet and cash-flow
evidence.  Produce a dimension signal only; a downstream Decision Agent owns
the final recommendation.

## Required evidence
1. Call get_stock_business_context for company, industry and business context.
2. Call get_tushare_daily_basic when an A-share valuation snapshot is needed.
3. Call get_tushare_financial_indicators for profitability, leverage and
   operating-quality trends.
4. Call get_tushare_financial_statements for income, balance-sheet and cash
   flow corroboration when supported.
5. If structured valuation or financial coverage is missing, call
   search_stock_fundamentals for source-linked supplementary materials. Keep
   this evidence partial and never copy an unsourced number from a snippet.

## Data integrity
- Never fabricate revenue, profit, ROE, PE/PB, debt, or cash-flow numbers.
- Keep report period, announcement date, source_chain and status attached to
  the evidence.  A missing or stale report is not evidence of good or bad
  fundamentals.
- quality is "ok" only when business context, valuation evidence and at least
  one financial-quality source are available; otherwise use "partial",
  "stale", or "unavailable".
- When fundamental data is unavailable, do not upgrade a conclusion to a
  medium/long-term strong-hold thesis.

## Output
Return only a JSON object:
{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence evidence-bound summary",
  "quality": "ok|partial|stale|unavailable",
  "valuation_assessment": "undervalued|fair|overvalued|not_available",
  "financial_health": "strong|normal|weak|not_available",
  "key_metrics": {},
  "risk_alerts": [],
  "missing_evidence": []
}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        stock = f"{ctx.stock_code} ({ctx.stock_name})" if ctx.stock_name else ctx.stock_code
        return (
            f"Build the fundamental evidence for stock **{stock}**.\n"
            "Use the available tools, preserve periods and source status, and "
            "return the required JSON contract."
        )

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[FundamentalAgent] failed to parse fundamental JSON")
            return None

        parsed = _normalise_dimension_payload(parsed)
        ctx.set_data("fundamental_opinion", parsed)
        ctx.meta.setdefault("dimension_status", {})[self.agent_name] = {
            "quality": parsed["quality"],
            "missing_evidence": parsed["missing_evidence"],
        }
        for alert in parsed["risk_alerts"]:
            ctx.add_risk_flag(category="fundamental", description=alert)

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed["signal"],
            confidence=_to_confidence(parsed.get("confidence")),
            reasoning=parsed["reasoning"],
            raw_data=parsed,
        )


def _normalise_dimension_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(payload)
    result["signal"] = result.get("signal") or "hold"
    result["reasoning"] = str(result.get("reasoning") or "")
    quality = str(result.get("quality") or "unavailable").lower()
    result["quality"] = quality if quality in {"ok", "partial", "stale", "unavailable"} else "unavailable"
    result["risk_alerts"] = _as_string_list(result.get("risk_alerts"))
    result["missing_evidence"] = _as_string_list(result.get("missing_evidence"))
    result.setdefault("valuation_assessment", "not_available")
    result.setdefault("financial_health", "not_available")
    result.setdefault("key_metrics", {})
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
