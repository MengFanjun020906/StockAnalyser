# -*- coding: utf-8 -*-
"""
Contract tests for get_realtime_quote tool output semantics.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.agent.tools.data_tools import _handle_get_realtime_quote


class _DummyRealtimeManager:
    def get_realtime_quote(self, _stock_code: str):
        return UnifiedRealtimeQuote(
            code="002050",
            name="三花智控",
            source=RealtimeSource.TENCENT,
            price=46.92,
            change_pct=4.69,
            change_amount=2.1,
            pre_close=44.82,
        )


def test_get_realtime_quote_labels_non_trading_day_as_latest_available_quote():
    fixed_now = datetime(2026, 5, 3, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    trading_calendar = SimpleNamespace(
        get_market_for_stock=lambda _code: "cn",
        get_market_now=lambda _market: fixed_now,
        is_market_open=lambda _market, _date: False,
        get_effective_trading_date=lambda _market, current_time=None: date(2026, 4, 30),
    )

    with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=_DummyRealtimeManager()), \
         patch("src.core.trading_calendar.get_market_for_stock", trading_calendar.get_market_for_stock), \
         patch("src.core.trading_calendar.get_market_now", trading_calendar.get_market_now), \
         patch("src.core.trading_calendar.is_market_open", trading_calendar.is_market_open), \
         patch("src.core.trading_calendar.get_effective_trading_date", trading_calendar.get_effective_trading_date), \
         patch("src.agent.tools.data_tools._resolve_realtime_market_phase", return_value="closed_non_trading_day"):
        result = _handle_get_realtime_quote("002050")

    assert result["price"] == 46.92
    assert result["change_pct"] == 4.69
    assert result["market"] == "cn"
    assert result["query_date"] == "2026-05-03"
    assert result["is_trading_day"] is False
    assert result["market_session"] == "closed_non_trading_day"
    assert result["is_market_open_now"] is False
    assert result["effective_trading_date"] == "2026-04-30"
    assert result["quote_trade_date"] == "2026-04-30"
    assert result["price_label"] == "最新可用价"
    assert result["change_pct_label"] == "最近交易日涨跌幅"
    assert "查询日市场休市" in result["freshness_note"]
    assert "不代表查询日盘中涨跌" in result["freshness_note"]


def test_get_realtime_quote_infers_a_share_market_from_exchange_suffix():
    seen_codes = []

    def infer_market(code):
        seen_codes.append(code)
        if code == "002050":
            return "cn"
        return None

    fixed_now = datetime(2026, 5, 3, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with patch("src.agent.tools.data_tools._get_fetcher_manager", return_value=_DummyRealtimeManager()), \
         patch("src.core.trading_calendar.get_market_for_stock", side_effect=infer_market), \
         patch("src.core.trading_calendar.get_market_now", return_value=fixed_now), \
         patch("src.core.trading_calendar.is_market_open", return_value=False), \
         patch("src.core.trading_calendar.get_effective_trading_date", return_value=date(2026, 4, 30)), \
         patch("src.agent.tools.data_tools._resolve_realtime_market_phase", return_value="closed_non_trading_day"):
        result = _handle_get_realtime_quote("002050.SZ")

    assert "002050.SZ" in seen_codes
    assert "002050" in seen_codes
    assert result["market"] == "cn"
    assert result["quote_trade_date"] == "2026-04-30"
