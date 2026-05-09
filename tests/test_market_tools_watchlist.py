from unittest.mock import patch

import pandas as pd

from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
from src.agent.tools import market_tools
from src.agent.tools.market_tools import _handle_discover_watchlist_candidates


def _write_daily_db(path, rows):
    import sqlite3

    with sqlite3.connect(path) as conn:
        pd.DataFrame(rows).to_sql("stock_daily", conn, if_exists="replace", index=False)


def _bars_for_turtle(symbol: str):
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i in range(22):
        close = 10.0 + i * 0.1
        high = close + 0.2
        open_price = close - 0.05
        if i == 21:
            close = 14.0
            high = 14.2
            open_price = 13.0
        rows.append({
            "symbol": symbol,
            "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": open_price,
            "high": high,
            "low": close - 0.3,
            "close": close,
            "volume": 1_000_000 + i,
            "turnover": 150_000_000 if i == 21 else 20_000_000,
        })
    return rows


def test_discover_watchlist_candidates_uses_seed_symbols_directly():
    result = _handle_discover_watchlist_candidates(
        seed_symbols=["600519", "600519", "300750"],
        limit=5,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["candidates"]] == ["600519", "300750"]
    assert "get_realtime_quote" in result["next_required_tools"]


def test_discover_watchlist_candidates_falls_back_when_sector_lookup_empty():
    with patch.dict("os.environ", {"SEQUOIA_CANDIDATE_DB_PATH": "/tmp/not-exists-sequoia.db"}), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=[],
    ):
        result = _handle_discover_watchlist_candidates(limit=3)

    assert result["status"] == "partial"
    assert result["fallback_used"] is True
    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["source"] == "fallback_seed_pool"


def test_sequoia_provider_returns_structured_strategy_candidates(tmp_path):
    db_path = tmp_path / "sequoia.db"
    _write_daily_db(db_path, _bars_for_turtle("600001"))

    provider = SequoiaCandidateProvider(str(db_path))
    result = provider.discover(limit=5, strategy_names=["turtle_trade"])

    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["code"] == "600001"
    assert candidate["source"] == "sequoia:turtle_trade"
    assert candidate["matched_strategies"] == ["turtle_trade"]
    assert "breakout" in candidate["strategy_tags"]
    assert candidate["signal_score"] > 0


def test_discover_watchlist_candidates_auto_merges_sequoia_and_sector(tmp_path):
    db_path = tmp_path / "sequoia.db"
    _write_daily_db(db_path, _bars_for_turtle("600001"))

    with patch.dict("os.environ", {"SEQUOIA_CANDIDATE_DB_PATH": str(db_path)}), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=["半导体"],
    ), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=[
            {
                "code": "600002",
                "name": "板块候选",
                "source": "akshare:industry:半导体",
                "reason": "来自强势板块。",
                "change_pct": 5.0,
            }
        ],
    ) as fetch_sector:
        result = _handle_discover_watchlist_candidates(
            candidate_source="auto",
            strategy_names=["turtle_trade"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "multi_recall"
    assert result["discovery_steps"][0]["source"] == "sequoia"
    assert any(step["source"] == "sector_constituents" for step in result["discovery_steps"])
    assert fetch_sector.called
    assert {item["code"] for item in result["candidates"]} == {"600001", "600002"}


def test_discover_watchlist_candidates_merges_duplicate_multi_recall():
    result = market_tools._merge_and_score_candidates(
        [
            {
                "code": "600001",
                "name": "测试股票",
                "source": "sequoia:turtle_trade",
                "signal_score": 78,
                "matched_strategies": ["turtle_trade"],
                "strategy_tags": ["breakout"],
                "reason": "海龟突破。",
            },
            {
                "code": "600001",
                "name": "测试股票",
                "source": "akshare:industry:半导体",
                "change_pct": 6.0,
                "reason": "来自强势板块。",
            },
        ],
        limit=5,
    )

    assert len(result) == 1
    candidate = result[0]
    assert candidate["source"] == "multi_recall"
    assert candidate["recall_sources"] == ["sequoia:turtle_trade", "akshare:industry:半导体"]
    assert candidate["signal_score"] > 78
    assert candidate["matched_strategies"] == ["turtle_trade"]


def test_discover_watchlist_candidates_falls_back_when_sequoia_db_missing():
    with patch.dict("os.environ", {"SEQUOIA_CANDIDATE_DB_PATH": "/tmp/not-exists-sequoia.db"}), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=[],
    ):
        result = _handle_discover_watchlist_candidates(limit=2)

    assert result["status"] == "partial"
    assert result["candidate_source"] == "fallback"
    assert result["fallback_used"] is True
    assert result["discovery_steps"][0]["source"] == "sequoia"
    assert result["discovery_steps"][0]["status"] == "unavailable"
    assert result["candidates"][0]["source"] == "fallback_seed_pool"
