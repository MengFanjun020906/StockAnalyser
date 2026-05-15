# -*- coding: utf-8 -*-
"""Deterministic expert opinion builders for watchlist scans."""

from src.agent.multi_expert.experts.watchlist import (
    build_capital_expert_opinion,
    build_candidate_expert_opinion,
    build_fundamental_expert_opinion,
    build_market_regime_expert_opinion,
    build_news_sentiment_expert_opinion,
    build_portfolio_risk_expert_opinion,
    build_technical_expert_opinion,
)

__all__ = [
    "build_capital_expert_opinion",
    "build_candidate_expert_opinion",
    "build_fundamental_expert_opinion",
    "build_market_regime_expert_opinion",
    "build_news_sentiment_expert_opinion",
    "build_portfolio_risk_expert_opinion",
    "build_technical_expert_opinion",
]
