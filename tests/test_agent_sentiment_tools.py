# -*- coding: utf-8 -*-
"""Tests for market sentiment and heat-candidate Agent tools."""

from unittest.mock import MagicMock, patch

from src.agent.factory import get_tool_registry
from src.agent.tools.sentiment_tools import (
    _handle_get_market_sentiment_snapshot,
    _handle_get_sentiment_heat_candidates,
    _handle_scan_global_risk_events,
)
from src.agent.tools.market_tools import _handle_discover_watchlist_candidates
from src.search_service import SearchResponse, SearchResult


def test_sentiment_tools_are_registered():
    registry = get_tool_registry()

    assert "get_market_sentiment_snapshot" in registry
    assert "get_sentiment_heat_candidates" in registry
    assert "scan_global_risk_events" in registry


def test_market_sentiment_snapshot_degrades_when_sources_fail():
    with patch("src.agent.tools.sentiment_tools._fetch_limit_up_pool", return_value={"status": "error", "error": "quota"}), \
         patch("src.agent.tools.sentiment_tools._fetch_popularity_rank", return_value={"status": "error", "error": "quota"}), \
         patch("src.agent.tools.sentiment_tools._fetch_market_indices", return_value={"status": "error", "error": "offline"}), \
         patch("src.agent.tools.sentiment_tools._handle_scan_global_risk_events", return_value={"status": "failed", "highest_severity": "unknown"}):
        payload = _handle_get_market_sentiment_snapshot(region="cn", include_global_risk=True, limit=10)

    assert payload["status"] == "limited"
    assert payload["data_quality"] == "limited"
    assert payload["risk_appetite"] == "neutral"
    assert any(item["status"] == "error" for item in payload["source_chain"])


def test_sentiment_heat_candidates_merges_popularity_and_limit_pool():
    popularity = {
        "status": "ok",
        "items": [
            {"code": "600001", "name": "热榜一", "rank": 1, "reason": "AI 关注度上升"},
            {"code": "600002", "name": "热榜二", "rank": 2},
        ],
    }
    limit_pool = {
        "status": "ok",
        "items": [
            {"code": "600001", "name": "热榜一", "limit_up_streak": 2, "stock_reason": "涨停原因"},
            {"code": "600003", "name": "涨停三", "limit_up_streak": 1},
        ],
    }
    with patch("src.agent.tools.sentiment_tools._fetch_popularity_rank", return_value=popularity), \
         patch("src.agent.tools.sentiment_tools._fetch_limit_up_pool", return_value=limit_pool):
        payload = _handle_get_sentiment_heat_candidates(market="cn", limit=5)

    assert payload["status"] == "ok"
    assert payload["candidate_source"] == "sentiment_heat"
    assert [item["code"] for item in payload["candidates"]] == ["600001", "600002", "600003"]
    merged = payload["candidates"][0]
    assert "sentiment_heat:hot_rank" in merged["recall_sources"]
    assert "sentiment_heat:limit_up" in merged["recall_sources"]
    assert merged["reason_dimensions"][0]["dimension"] == "sentiment"


def test_discover_watchlist_candidates_supports_sentiment_heat_source():
    with patch(
        "src.agent.tools.sentiment_tools._handle_get_sentiment_heat_candidates",
        return_value={
            "status": "ok",
            "candidate_source": "sentiment_heat",
            "candidates": [{"code": "600001", "name": "热榜一", "source": "sentiment_heat:hot_rank"}],
            "source_chain": [{"provider": "mock", "status": "ok", "count": 1}],
            "data_quality": "sufficient",
        },
    ):
        payload = _handle_discover_watchlist_candidates(candidate_source="sentiment_heat", limit=3)

    assert payload["status"] == "ok"
    assert payload["candidate_source"] == "sentiment_heat"
    assert payload["candidates"][0]["code"] == "600001"
    assert payload["discovery_steps"][0]["source"] == "sentiment_heat"


def test_global_risk_scan_uses_search_results_and_scores_severity():
    service = MagicMock()
    service.search_general_news.return_value = SearchResponse(
        query="risk",
        provider="mock",
        success=True,
        results=[
            SearchResult(
                title="Oil shipping disruption after missile attack",
                snippet="Energy supply and shipping routes face disruption.",
                url="https://example.com/a",
                source="example",
                published_date="2026-07-17",
            )
        ],
    )

    with patch("src.agent.tools.sentiment_tools._build_search_service", return_value=service):
        payload = _handle_scan_global_risk_events(region="global", lookback_hours=24, limit=5)

    assert payload["status"] == "ok"
    assert payload["highest_severity"] in {"high", "critical"}
    assert payload["events"][0]["event_type"] in {"geopolitical", "energy_transport", "macro_risk"}
    assert payload["source_chain"][0]["provider"] == "mock"
