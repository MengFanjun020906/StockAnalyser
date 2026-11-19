"""Guard tests for the read-only pricing shadow probe."""
from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent.pricing import shadow


class _Config:
    def __init__(self, enabled: bool, path: str = "", max_codes: int = 60) -> None:
        self.agent_pricing_shadow_enabled = enabled
        self.agent_pricing_shadow_path = path
        self.agent_pricing_shadow_max_codes = max_codes


def _enable(monkeypatch, *, path: str = "", max_codes: int = 60) -> None:
    monkeypatch.setattr(shadow, "get_config", lambda: _Config(True, path, max_codes))


class _Artifact:
    generation_status = "no_plan"
    reason_codes = ["INSUFFICIENT_DAILY_HISTORY"]

    def model_dump(self, mode: str = "json"):
        return {
            "generation_status": "no_plan",
            "data_quality": "invalid",
            "reason_codes": list(self.reason_codes),
            "actual_as_of_session": "2026-11-19",
            "scenarios": [],
            "observation_anchors": [],
            "diagnostics": {},
        }


class _Snapshot:
    daily_bars: list = []


def test_disabled_probe_is_zero_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow, "get_config", lambda: _Config(False))

    class _ExplodingBuilder:
        def __init__(self, *args, **kwargs):
            raise AssertionError("builder must not be constructed when disabled")

    monkeypatch.setattr(shadow, "PricePlanSnapshotBuilder", _ExplodingBuilder)
    target = tmp_path / "shadow.json"

    result = shadow.run_pricing_shadow_probe(
        ["600519", "000001"], as_of=date(2026, 11, 19), output_path=target
    )

    assert result is None
    assert not target.exists()


def test_unsupported_markets_are_recorded_not_skipped(monkeypatch, tmp_path):
    _enable(monkeypatch)

    class _ExplodingBuilder:
        def __init__(self, *args, **kwargs):
            raise AssertionError("builder must not run for unsupported markets")

    monkeypatch.setattr(shadow, "PricePlanSnapshotBuilder", _ExplodingBuilder)
    target = tmp_path / "shadow.json"

    payload = shadow.run_pricing_shadow_probe(
        ["hk00700", "AAPL"], as_of=date(2026, 11, 19), output_path=target
    )

    assert payload is not None
    assert [item["status"] for item in payload["items"]] == ["unsupported", "unsupported"]
    assert [item["market"] for item in payload["items"]] == ["hk", "us"]
    assert payload["summary"]["unsupported"] == 2
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == shadow.SCHEMA_VERSION


def test_single_failure_does_not_stop_the_run(monkeypatch, tmp_path):
    _enable(monkeypatch)

    class _Builder:
        def __init__(self, *args, **kwargs):
            pass

        def build(self, code, *, as_of):
            if code == "000001":
                raise RuntimeError("boom")
            return _Snapshot()

    monkeypatch.setattr(shadow, "PricePlanSnapshotBuilder", _Builder)
    monkeypatch.setattr(
        shadow,
        "generate_trade_plan",
        lambda *args, **kwargs: SimpleNamespace(
            price_plan=_Artifact(), execution_status="no_plan"
        ),
    )
    target = tmp_path / "shadow.json"

    payload = shadow.run_pricing_shadow_probe(
        ["000001", "600519"], as_of=date(2026, 11, 19), output_path=target
    )

    assert payload is not None
    statuses = {item["code"]: item["status"] for item in payload["items"]}
    assert statuses["000001"] == "error"
    assert statuses["600519"] == "no_plan"
    assert payload["summary"]["errors"] == 1
    assert payload["summary"]["cash_no_plan"] == 1
    ok_item = next(item for item in payload["items"] if item["code"] == "600519")
    assert ok_item["comparison"]["atr_14"] == {"cash": None, "qfq": None, "delta_pct": None}
    assert ok_item["comparison"]["scenarios"] == []
    assert ok_item["comparison"]["qfq_price_deviation_pct"] is None


def test_qfq_conversion_uses_adj_factor_ratio():
    from src.agent.pricing.schemas import (
        MarketRuleSnapshot,
        PriceBarSnapshot,
        PricePlanSnapshot,
    )

    rules = MarketRuleSnapshot(
        snapshot_id="rules-1",
        venue="SSE",
        board="main",
        trade_date=date(2026, 11, 19),
        tick_size=Decimal("0.01"),
        t_plus_one=True,
    )
    bars = [
        PriceBarSnapshot(
            trade_date=date(2026, 11, 18),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("100"),
            amount=Decimal("1000"),
        ),
        PriceBarSnapshot(
            trade_date=date(2026, 11, 19),
            open=Decimal("20"),
            high=Decimal("21"),
            low=Decimal("19"),
            close=Decimal("20"),
            volume=Decimal("100"),
            amount=Decimal("2000"),
        ),
    ]
    snapshot = PricePlanSnapshot(
        symbol="600519",
        requested_as_of=date(2026, 11, 19),
        actual_as_of_session=date(2026, 11, 19),
        daily_bars=bars,
        weekly_bars=bars,
        last_cash_price=Decimal("20"),
        price_basis="cash",
        price_scale_verified=True,
        history_source="test",
        market_rules=rules,
    )

    derived, status = shadow.build_qfq_snapshot(
        snapshot,
        {date(2026, 11, 18): Decimal("1"), date(2026, 11, 19): Decimal("2")},
    )

    assert status["status"] == "ok"
    assert derived is not None
    assert derived.price_basis == "qfq"
    assert derived.daily_bars[0].close == Decimal("5.000000")
    assert derived.daily_bars[0].volume == Decimal("200")
    assert derived.daily_bars[1].close == Decimal("20.000000")
    assert derived.daily_bars[1].volume == Decimal("100")


def test_hook_keeps_report_identical_when_enabled(monkeypatch):
    from src.agent import stock_selection

    ctx = stock_selection.SelectionRunContext(run_id="run-2", user_message="x", market="cn")
    ctx.set_stage(
        "candidate_screening",
        {
            "status": "ok",
            "summary": {},
            "full": {
                "shortlist": [
                    {"code": "600519", "screening_result": "deep_dive"},
                    {"code": "000001", "screening_result": "monitor"},
                ]
            },
        },
    )
    ctx.set_stage(
        "single_stock_deep_dive",
        {
            "status": "ok",
            "summary": {"targets": ["600519"]},
            "full": {"targets": ["600519"], "results": [{"code": "600519"}]},
        },
    )
    ctx.set_stage(
        "portfolio_allocation",
        {
            "status": "ok",
            "summary": {},
            "full": {"positions_plan": [{"code": "600519", "action": "conditional_open"}]},
        },
    )

    before_context = copy.deepcopy(ctx.to_dict(include_full=True))
    before_report = json.dumps(
        stock_selection._build_final_report_json(ctx), ensure_ascii=False, sort_keys=True
    )

    monkeypatch.setattr("src.config.get_config", lambda: _Config(True))
    captured = {}
    monkeypatch.setattr(
        "src.agent.pricing.shadow.run_pricing_shadow_probe",
        lambda codes, **kwargs: captured.update({"codes": list(codes), **kwargs}),
    )

    stock_selection._run_pricing_shadow_if_enabled(ctx)

    after_context = ctx.to_dict(include_full=True)
    after_report = json.dumps(
        stock_selection._build_final_report_json(ctx), ensure_ascii=False, sort_keys=True
    )

    assert captured["codes"] == ["600519", "000001"]
    assert after_context == before_context
    assert after_report == before_report


def test_hook_swallows_probe_errors(monkeypatch):
    from src.agent import stock_selection

    monkeypatch.setattr("src.config.get_config", lambda: _Config(True))
    monkeypatch.setattr(
        stock_selection,
        "_pricing_shadow_codes",
        lambda ctx: ["600519"],
    )
    monkeypatch.setattr(
        "src.agent.pricing.shadow.run_pricing_shadow_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe boom")),
    )

    ctx = stock_selection.SelectionRunContext(run_id="run-3", user_message="x")
    stock_selection._run_pricing_shadow_if_enabled(ctx)  # must not raise
