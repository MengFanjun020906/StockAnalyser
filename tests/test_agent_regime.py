# -*- coding: utf-8 -*-
"""Tests for A-share market regime detection."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from src.agent.regime import (
    MarketBar,
    MarketRegime,
    SentimentComponents,
    VolatilityBucket,
    WyckoffPhase,
    detect_market_regime,
)
from src.agent.tools.market_tools import _handle_detect_market_regime, detect_market_regime_tool
from src.storage import DatabaseManager


def _bars(count: int = 180, *, start: float = 100.0, drift: float = 0.3, vol: float = 1.0) -> list[MarketBar]:
    rows: list[MarketBar] = []
    current = start
    base_date = date(2025, 1, 1)
    for idx in range(count):
        current += drift
        spread = vol * (1.0 + (idx % 7) / 10.0)
        rows.append(
            MarketBar(
                date=base_date + timedelta(days=idx),
                open=current - drift / 2,
                high=current + spread,
                low=max(1.0, current - spread),
                close=current,
                volume=1_000_000 + idx * 1000,
                amount=100_000_000 + idx * 100_000,
            )
        )
    return rows


def test_volatility_bucket_uses_empirical_cdf_and_damping():
    bars = _bars(150, drift=0.1, vol=0.4) + _bars(30, start=115, drift=-0.2, vol=8.0)

    state = detect_market_regime(
        bars,
        previous_bucket=VolatilityBucket.LOW.value,
        previous_regime=MarketRegime.RANGE_BOUND.value,
        confirmation_bars=3,
    )

    assert state.raw_volatility_bucket == VolatilityBucket.EXTREME
    assert state.volatility_bucket == VolatilityBucket.NORMAL
    assert state.volatility_percentile is not None
    assert any("阻尼" in item for item in state.evidence)


def test_regime_confirmation_requires_consecutive_bars_before_switching():
    bars = _bars(180, drift=0.5, vol=1.0)

    state = detect_market_regime(
        bars,
        previous_regime=MarketRegime.RANGE_BOUND.value,
        pending_regime=None,
        pending_count=0,
        confirmation_bars=3,
    )

    assert state.confirmation["state"] == "pending"
    assert state.confirmation["pending_regime"] == MarketRegime.TRENDING_UP.value
    assert state.regime == MarketRegime.RANGE_BOUND


def test_sentiment_and_wyckoff_are_structured():
    bars = _bars(180, drift=0.35, vol=1.0)
    sentiment = SentimentComponents(
        margin_balance_change=0.5,
        market_breadth=0.8,
        fear_greed_index=80,
        northbound_flow_z=0.7,
        market_flow_z=0.6,
    )

    state = detect_market_regime(bars, sentiment=sentiment)

    assert state.sentiment_score is not None
    assert state.sentiment_state.value in {"greed", "extreme_greed"}
    assert state.wyckoff_phase in {WyckoffPhase.MARKUP, WyckoffPhase.RANGE}
    assert state.data_quality in {"sufficient", "limited"}


def test_market_regime_state_persists_to_sqlite():
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    payload = detect_market_regime(_bars(180)).to_dict()

    db.save_market_regime_state("cn", payload)
    loaded = db.get_market_regime_state("cn")

    assert loaded is not None
    assert loaded["market"] == "cn"
    assert loaded["payload"]["regime"] == payload["regime"]
    DatabaseManager.reset_instance()


def test_detect_market_regime_tool_registered():
    assert detect_market_regime_tool.name == "detect_market_regime"
    assert detect_market_regime_tool.category == "market"


def test_default_tool_registry_contains_detect_market_regime():
    from src.agent import factory

    factory._TOOL_REGISTRY = None
    registry = factory.get_tool_registry()

    assert "detect_market_regime" in registry.list_names()
    factory._TOOL_REGISTRY = None


def test_detect_market_regime_tool_uses_persisted_state_and_context():
    history_rows = [
        {
            "date": bar.date.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        for bar in _bars(180, drift=0.4, vol=1.2)
    ]

    class FakeDB:
        def __init__(self):
            self.saved = None

        def get_market_regime_state(self, market):
            return {
                "regime": "range_bound",
                "volatility_bucket": "normal",
                "pending_regime": "trending_up",
                "pending_count": 2,
                "payload": {"volatility_bucket": "normal"},
            }

        def save_market_regime_state(self, market, payload):
            self.saved = (market, payload)

    fake_db = FakeDB()

    with patch("src.agent.tools.market_tools._load_market_history", return_value=(history_rows, "unit")), \
         patch("src.agent.tools.market_tools._handle_get_market_indices", return_value={"indices": [{"change_pct": 1.0}]}), \
         patch("src.agent.tools.market_tools._handle_get_sector_rankings", return_value={"top_sectors": [], "bottom_sectors": []}), \
         patch("src.agent.tools.data_tools._handle_get_market_capital_flow", return_value={"market_flow": {"main_net_inflow": 100.0}}), \
         patch("src.agent.tools.data_tools._handle_get_northbound_capital_flow", return_value={"history": [{"net_inflow": 10.0}]}), \
         patch("src.agent.tools.data_tools._handle_get_margin_trading_summary", return_value={"sse": [{"margin_balance": 100.0}, {"margin_balance": 110.0}]}), \
         patch("src.storage.get_db", return_value=fake_db):
        result = _handle_detect_market_regime(persist=True)

    assert result["status"] == "ok"
    assert result["history_source"] == "unit"
    assert result["persisted"] is True
    assert fake_db.saved is not None


def test_market_history_prefers_tushare_index_daily_fast_path():
    rows = [
        {
            "ts_code": "000300.SH",
            "trade_date": "20260515",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "vol": 1000,
            "amount": 10000,
        }
    ]

    import pandas as pd
    from src.agent.tools import market_tools

    with patch("data_provider.tushare_client.get_tushare_token", return_value="token"), \
         patch("data_provider.tushare_client.query_tushare_api", return_value=pd.DataFrame(rows)), \
         patch("src.services.history_loader.load_history_df") as fallback_loader:
        history, source = market_tools._load_market_history("000300", 260)

    assert source == "tushare:index_daily"
    assert history[0]["date"] == "2026-05-15"
    assert history[0]["close"] == 101
    fallback_loader.assert_not_called()


def test_detect_market_regime_tool_returns_structured_timeout_diagnostics():
    with patch("src.agent.tools.market_tools._run_with_timeout", return_value=(None, "market_history timeout", 10)), \
         patch("src.storage.get_db", side_effect=RuntimeError("db unavailable")):
        result = _handle_detect_market_regime(persist=True)

    assert result["status"] == "insufficient_data"
    assert result["history_source"] == "timeout"
    assert any("market_history timeout" in item for item in result["data_errors"])
    assert result["component_diagnostics"]["market_history"]["status"] == "timeout"
