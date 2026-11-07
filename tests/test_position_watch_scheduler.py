# -*- coding: utf-8 -*-
"""Deterministic holding-watch scheduler tests."""

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.services.position_watch_scheduler import PositionWatchSchedulerService


def _position():
    return {
        "account_id": 1,
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "market": "cn",
        "quantity": 100,
        "average_cost": 1500,
        "holding_days": 3,
    }


def _chart(*, score=12, warning_level=0, evaluable=True):
    return {
        "status": "ok",
        "bars": [{"date": "2026-08-10", "close": 1510}],
        "data_window": {"end": "2026-08-10"},
        "composite": {
            "composite_verdict": {
                "score": score,
                "verdict_status": "evaluable" if evaluable else "not_evaluable",
                "exit_warning": {"level": warning_level, "items": []},
            }
        },
    }


def test_level_three_warning_reduces_without_l1_agent():
    market_data = MagicMock()
    market_data.load_chart.return_value = _chart(warning_level=3)
    service = PositionWatchSchedulerService(
        repository=MagicMock(),
        portfolio_service=MagicMock(),
        market_data=market_data,
        l1_runner=MagicMock(),
    )

    result = service._evaluate_l0(_position(), previous=None, as_of=date(2026, 8, 10))

    assert result["action"] == "reduce"
    assert result["risk_level"] == "high"
    assert result["l1_required"] is False


def test_score_drop_routes_holding_to_l1_review():
    market_data = MagicMock()
    market_data.load_chart.return_value = _chart(score=9)
    service = PositionWatchSchedulerService(
        repository=MagicMock(),
        portfolio_service=MagicMock(),
        market_data=market_data,
        l1_runner=MagicMock(),
    )

    result = service._evaluate_l0(
        _position(),
        previous={"latest_score": 15, "stop_price": None},
        as_of=date(2026, 8, 10),
    )

    assert result["action"] == "hold"
    assert result["risk_level"] == "medium"
    assert result["l1_required"] is True
    assert "composite_score_drop_ge_5" in result["reasons"]


def test_us_next_run_is_dst_aware():
    market_now = datetime(2026, 8, 10, 15, 0, tzinfo=ZoneInfo("America/New_York"))

    next_run = PositionWatchSchedulerService._next_run_utc("us", market_now)

    assert next_run == datetime(2026, 8, 10, 20, 30)


def test_l1_reduce_recommendation_is_projected_through_risk_gate():
    service = PositionWatchSchedulerService(
        repository=MagicMock(),
        portfolio_service=MagicMock(),
        market_data=MagicMock(),
        l1_runner=MagicMock(),
    )
    evaluation = {
        "action": "hold",
        "risk_level": "medium",
        "latest_close": 1510,
        "stop_price": 1400,
        "reasons": ["exit_warning_level_2"],
        "risk_gate": {},
    }

    result = service._apply_l1_review(
        _position(),
        evaluation=evaluation,
        l1={"status": "ok", "dashboard": {"operation_advice": {"has_position": "建议减仓"}}},
    )

    assert result["action"] == "reduce"
    assert result["risk_level"] == "high"
    assert result["needs_review"] is False
