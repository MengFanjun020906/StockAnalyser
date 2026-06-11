# -*- coding: utf-8 -*-
"""
IntelAgent — news & intelligence gathering specialist.

Responsible for:
- Searching latest stock news and announcements
- Running comprehensive intelligence search
- Detecting risk events (reduce holdings, earnings warnings, regulatory)
- Summarising sentiment and catalysts
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class IntelAgent(BaseAgent):
    agent_name = "intel"
    max_steps = 4
    tool_names = [
        "get_tushare_today_news",
        "search_stock_news",
        "search_comprehensive_intel",
        "get_stock_info",
        "get_capital_flow",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are an **Intelligence & Sentiment Agent** specialising in A-shares, \
HK, and US equities.

Your task: gather the latest news, announcements, and risk signals for \
the given stock, then produce a structured JSON opinion.

## Workflow
1. Search latest stock news (earnings, announcements, insider activity)
2. For A-shares, call get_tushare_today_news when same-day market flash \
news is needed; it only returns today's Tushare news window
3. Run comprehensive intel search — this covers latest news, company \
announcements (公司公告), market analysis, risk checks, and earnings outlook
4. For A-share stocks, call get_capital_flow to obtain capital-flow data with \
explicit main-force vs all-order semantics and include it in your analysis
5. Classify positive catalysts and risk alerts
6. Assess overall sentiment

## Risk Detection Priorities
- Insider / major shareholder sell-downs (减持)
- Earnings warnings or pre-loss announcements (业绩预亏)
- Regulatory penalties or investigations
- Industry-wide policy headwinds
- Large lock-up expirations (解禁)
- PE valuation anomalies
- Sustained main-force capital outflow (主力持续净流出)

## Capital Flow Interpretation (A-shares only)
- main_net_inflow > 0: bullish signal (主力净流入)
- main_net_inflow < 0: bearish signal (主力净流出)
- main_inflow_5d / main_inflow_10d: medium-term 主力 accumulation or distribution trend
- net_inflow / net_inflow_5d / net_inflow_10d: Tushare net_mf_amount all-order active-flow口径. Never describe these fields as 主力资金.
- amount_unit is CNY; raw_amount_unit is 10k CNY. Use the returned definitions when explaining exact capital-flow numbers.

## Output Format
Return **only** a JSON object:
{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary of news/sentiment/capital-flow findings",
  "risk_alerts": ["list", "of", "detected", "risks"],
  "positive_catalysts": ["list", "of", "catalysts"],
  "sentiment_label": "very_positive|positive|neutral|negative|very_negative",
  "capital_flow_signal": "inflow|outflow|neutral|not_available",
  "key_news": [
    {"title": "...", "impact": "positive|negative|neutral"}
  ]
}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Gather intelligence and assess sentiment for stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"
        parts.append(
            "Steps:\n"
            "1. For A-shares, call get_tushare_today_news if same-day flash news is relevant.\n"
            "2. Call search_comprehensive_intel to get latest news, company announcements "
            "(公司公告), risk events, and earnings outlook.\n"
            "3. Call get_capital_flow to obtain capital flow data (A-share only; skip for HK/US). "
            "Use main_net_inflow/main_inflow_5d/main_inflow_10d for 主力口径; "
            "net_inflow* is all-order net_mf_amount and must not be called 主力资金.\n"
            "4. Output the JSON opinion including capital_flow_signal."
        )
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[IntelAgent] failed to parse opinion JSON")
            return None

        # Cache parsed intel so downstream agents (especially RiskAgent) can
        # reuse it instead of re-searching the same evidence.
        ctx.set_data("intel_opinion", parsed)

        # Propagate risk alerts to context
        for alert in parsed.get("risk_alerts", []):
            if isinstance(alert, str) and alert:
                ctx.add_risk_flag(category="intel", description=alert)

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed.get("signal", "hold"),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=parsed.get("reasoning", ""),
            raw_data=parsed,
        )
