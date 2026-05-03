from unittest.mock import patch

from src.agent.tools.market_tools import _handle_discover_watchlist_candidates


def test_discover_watchlist_candidates_uses_seed_symbols_directly():
    result = _handle_discover_watchlist_candidates(
        seed_symbols=["600519", "600519", "300750"],
        limit=5,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["candidates"]] == ["600519", "300750"]
    assert "get_realtime_quote" in result["next_required_tools"]


def test_discover_watchlist_candidates_falls_back_when_sector_lookup_empty():
    with patch("src.agent.tools.market_tools._top_sector_names", return_value=[]):
        result = _handle_discover_watchlist_candidates(limit=3)

    assert result["status"] == "partial"
    assert result["fallback_used"] is True
    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["source"] == "fallback_seed_pool"
