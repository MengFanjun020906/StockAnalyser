# -*- coding: utf-8 -*-
"""Tests for A-share market regime detection."""

from __future__ import annotations

import json
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
from src.agent.tools import market_tools
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


def test_high_volatility_uptrend_keeps_directional_regime():
    bars = _bars(220, drift=0.35, vol=0.5)
    widened = []
    for idx, bar in enumerate(bars):
        if idx < len(bars) - 30:
            widened.append(bar)
            continue
        widened.append(
            MarketBar(
                date=bar.date,
                open=bar.open,
                high=bar.close + 10.0,
                low=max(1.0, bar.close - 10.0),
                close=bar.close,
                volume=bar.volume * 1.5,
                amount=bar.amount,
            )
        )

    state = detect_market_regime(widened)

    assert state.volatility_bucket in {VolatilityBucket.HIGH_VOL, VolatilityBucket.EXTREME}
    assert state.regime == MarketRegime.TRENDING_UP
    assert any("波动不是开仓否决项" in item for item in state.strategy_hints)


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


def test_market_regime_timeout_defaults_favor_auxiliary_accuracy():
    from src.config import Config
    from src.core.config_registry import get_field_definition

    cfg = Config()

    assert cfg.agent_regime_component_timeout_seconds == 25.0
    assert cfg.agent_sector_rankings_timeout_seconds == 10.0
    assert cfg.agent_tushare_tool_timeout_seconds == 20.0
    assert get_field_definition("AGENT_REGIME_COMPONENT_TIMEOUT_SECONDS")["default_value"] == "25.0"
    assert get_field_definition("AGENT_SECTOR_RANKINGS_TIMEOUT_SECONDS")["default_value"] == "10.0"
    assert get_field_definition("AGENT_TUSHARE_TOOL_TIMEOUT_SECONDS")["default_value"] == "20.0"


def test_detect_market_regime_uses_full_auxiliary_component_timeout():
    from src.agent.tools import market_tools

    calls = []

    history_rows = [
        {
            "date": str(row.date),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
        }
        for row in _bars(180)
    ]

    def fake_get_timeout(attr_name: str, default: float) -> float:
        if attr_name == "agent_regime_component_timeout_seconds":
            return 25.0
        return default

    def fake_run_with_timeout(task, timeout_seconds: float, task_name: str):
        calls.append((task_name, timeout_seconds))
        if task_name == "market_history":
            return (history_rows, "unit_test"), None, 1
        if task_name == "market_indices":
            return {"status": "ok", "indices": []}, None, 1
        if task_name == "sector_rankings":
            return {"status": "empty", "top_sectors": [], "bottom_sectors": []}, None, 1
        return {"status": "ok", "source_chain": []}, None, 1

    with patch("src.agent.tools.market_tools._get_agent_timeout_attr", side_effect=fake_get_timeout), \
         patch("src.agent.tools.market_tools._run_with_timeout", side_effect=fake_run_with_timeout), \
         patch("src.storage.get_db", side_effect=RuntimeError("db unavailable")):
        result = market_tools._handle_detect_market_regime(persist=False)

    assert result["status"] == "ok"
    timeouts_by_name = {name: timeout for name, timeout in calls}
    assert timeouts_by_name["market_history"] == 25.0
    assert timeouts_by_name["market_indices"] == 25.0
    assert timeouts_by_name["sector_rankings"] == 25.0
    assert timeouts_by_name["market_flow"] == 25.0
    assert timeouts_by_name["northbound"] == 25.0
    assert timeouts_by_name["margin"] == 25.0


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

    class FakeTushareHttpClient:
        def query(self, *args, **kwargs):
            return pd.DataFrame(rows)

    with patch("data_provider.tushare_client.get_tushare_token", return_value="token"), \
         patch("data_provider.tushare_client.build_tushare_http_client", return_value=FakeTushareHttpClient()) as build_client, \
         patch("src.services.history_loader.load_history_df") as fallback_loader:
        history, source = market_tools._load_market_history("000300", 1)

    assert source == "tushare:index_daily"
    assert history[0]["date"] == "2026-05-15"
    assert history[0]["close"] == 101
    build_client.assert_called_once_with(timeout=20.0)
    fallback_loader.assert_not_called()


def test_market_history_skips_stock_fallback_for_supported_index():
    from src.agent.tools import market_tools

    with patch("src.agent.tools.market_tools._load_index_history_from_tushare", return_value=([], "tushare:index_daily_failed")), \
         patch("src.services.history_loader.load_history_df", return_value=(None, "db_cache_miss")) as fallback_loader, \
         patch("src.agent.tools.market_tools._load_index_history_from_baostock", return_value=([], "baostock:index_daily_failed")):
        history, source = market_tools._load_market_history("000300", 260)

    assert history == []
    assert source == "tushare:index_daily_failed;db_cache_miss;baostock:index_daily_failed"
    fallback_loader.assert_called_once_with("000300", days=260, fallback_to_network=True)


def test_market_history_ignores_short_cache_and_uses_baostock_fallback():
    short_rows = [{"date": "2026-07-14", "open": 1, "high": 1, "low": 1, "close": 1}]
    fallback_rows = [
        {
            "date": str(date(2025, 1, 1) + timedelta(days=idx)),
            "open": 100 + idx,
            "high": 101 + idx,
            "low": 99 + idx,
            "close": 100.5 + idx,
            "volume": 1000,
            "amount": 10000,
        }
        for idx in range(260)
    ]

    with patch(
        "src.agent.tools.market_tools._load_market_history_cache_only",
        return_value=(short_rows, "short-cache"),
    ), patch(
        "src.agent.tools.market_tools._load_index_history_from_tushare",
        return_value=(short_rows, "short-tushare"),
    ), patch(
        "src.services.history_loader.load_history_df",
        return_value=(None, "none"),
    ), patch(
        "src.agent.tools.market_tools._load_index_history_from_baostock",
        return_value=(fallback_rows, "baostock:index_daily"),
    ) as baostock_loader:
        history, source = market_tools._load_market_history("000300", 1090, cache_first=True)

    assert len(history) == 260
    assert source == "baostock:index_daily"
    baostock_loader.assert_called_once_with("000300", 1090)


def test_market_history_uses_local_index_cache_when_tushare_fails(tmp_path):
    from src.agent.tools import market_tools

    cache_path = tmp_path / "000300.SH.json"
    cache_path.write_text(
        json.dumps({
            "ts_code": "000300.SH",
            "source": "tushare:index_daily",
            "rows": [
                {
                    "date": "2026-05-14",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 1000,
                    "amount": 10000,
                },
                {
                    "date": "2026-05-15",
                    "open": 101,
                    "high": 103,
                    "low": 100,
                    "close": 102,
                    "volume": 1200,
                    "amount": 12000,
                },
            ],
        }),
        encoding="utf-8",
    )

    class FailingTushareHttpClient:
        def query(self, *args, **kwargs):
            raise RuntimeError("gateway unavailable")

    with patch("data_provider.tushare_client.get_tushare_token", return_value="token"), \
         patch("data_provider.tushare_client.build_tushare_http_client", return_value=FailingTushareHttpClient()), \
         patch("src.agent.tools.market_tools._index_history_cache_path", return_value=cache_path), \
         patch("src.services.history_loader.load_history_df") as fallback_loader:
        history, source = market_tools._load_market_history("000300", 1)

    assert source == "tushare:index_daily_cache:after_http_error"
    assert len(history) == 1
    assert history[0]["date"] == "2026-05-15"
    assert history[0]["close"] == 102
    fallback_loader.assert_not_called()


def test_regime_forward_probability_uses_cache_first_market_history():
    seen_cache_first = []
    history_rows = [
        {
            "date": str(row.date),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
        }
        for row in _bars(420)
    ]

    def fake_load_market_history(_index_code, _lookback_days, *, cache_first=False):
        seen_cache_first.append(cache_first)
        return history_rows, "unit_test"

    with patch("src.agent.tools.market_tools._load_market_history", side_effect=fake_load_market_history):
        result = market_tools._handle_get_regime_forward_probability(
            lookback_days=260,
            windows=[7, 30],
        )

    assert result["status"] in {"ok", "insufficient_data"}
    assert seen_cache_first == [True]


def test_detect_market_regime_tool_returns_structured_timeout_diagnostics():
    with patch("src.agent.tools.market_tools._run_with_timeout", return_value=(None, "market_history timeout", 10)), \
         patch("src.storage.get_db", side_effect=RuntimeError("db unavailable")):
        result = _handle_detect_market_regime(persist=True)

    assert result["status"] == "insufficient_data"
    assert result["history_source"] == "timeout"
    assert any("market_history timeout" in item for item in result["data_errors"])
    assert result["component_diagnostics"]["market_history"]["status"] == "timeout"
