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
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are an **Intelligence & Sentiment Agent** specialising in A-shares, \
HK, and US equities.

Your task: gather the latest news, announcements, and risk signals for \
the given stock, then produce a structured JSON opinion.

## Workflow
1. Search stock news using the declared 7/14/30-day adaptive window
2. For A-shares, call get_tushare_today_news when same-day market flash \
news is needed; it only returns today's Tushare news window
3. Run comprehensive intel search — this covers latest news, company \
announcements (公司公告), market analysis, risk checks, and earnings outlook
4. Classify positive catalysts and risk alerts
5. Assess overall sentiment

## Data Quality
- Use quality="ok" only when current news and company-announcement evidence
  are both available with identifiable sources.
- Use "partial" when one channel is missing, "stale" when results are outside
  their declared freshness window, and "unavailable" when no source-backed
  event evidence exists.
- List incomplete searches or missing announcement/news channels in
  missing_evidence. Never invent an event, publication time, or source.

## Risk Detection Priorities
- Insider / major shareholder sell-downs (减持)
- Earnings warnings or pre-loss announcements (业绩预亏)
- Regulatory penalties or investigations
- Industry-wide policy headwinds
- Large lock-up expirations (解禁)
- PE valuation anomalies

## Output Format
Return **only** a JSON object:
{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary of news/sentiment/capital-flow findings",
  "risk_alerts": ["list", "of", "detected", "risks"],
  "positive_catalysts": ["list", "of", "catalysts"],
  "sentiment_label": "very_positive|positive|neutral|negative|very_negative",
  "quality": "ok|partial|stale|unavailable",
  "missing_evidence": [],
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
            "3. Output the JSON opinion with source-backed news, catalysts, risks, "
            "quality, and missing_evidence. Capital-flow analysis is owned by the "
            "separate Capital Agent."
        )
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[IntelAgent] failed to parse opinion JSON")
            return None

        # Cache parsed intel so downstream agents (especially RiskAgent) can
        # reuse it instead of re-searching the same evidence.
        quality = str(parsed.get("quality") or "partial").lower()
        if quality not in {"ok", "partial", "stale", "unavailable"}:
            quality = "partial"
        missing_evidence = parsed.get("missing_evidence")
        if not isinstance(missing_evidence, list):
            missing_evidence = []
        parsed["quality"] = quality
        parsed["missing_evidence"] = [str(item) for item in missing_evidence if str(item).strip()]
        ctx.set_data("intel_opinion", parsed)
        ctx.meta.setdefault("dimension_status", {})[self.agent_name] = {
            "quality": quality,
            "missing_evidence": parsed["missing_evidence"],
        }

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
