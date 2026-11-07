from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from math import sin

import pandas as pd
import pytest
from pydantic import ValidationError

from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.decision import build_account_decision, build_review_artifact
from src.agent.pricing.decision import finalize_account_decision
from src.agent.pricing.params import PRICE_PLAN_PARAMS_V1
from src.agent.pricing.plan import evaluate_price_plan
from src.agent.pricing.report import merge_price_plan_into_report, render_price_plan_markdown
from src.agent.pricing.readiness import PricePlanReadinessGate
from src.agent.pricing.runtime import (
    _account_position_cap_pct,
    _risk_profile,
    generate_trade_plan,
)
from src.agent.pricing.snapshot import PricePlanSnapshotBuilder
from src.agent.pricing.schemas import (
    ChipSnapshot,
    MarketRuleSnapshot,
    PlanLifecycleState,
    PriceBarSnapshot,
    PricePlanArtifact,
    PricePlanSnapshot,
    TradePlanResult,
)
from src.services.signal_lifecycle_service import SignalLifecycleService


def _supporting_review_result(*, scenario_ids: list[str] | None = None):
    from types import SimpleNamespace

    return SimpleNamespace(
        content="定性证据支持执行数学计划。",
        dashboard=None,
        trade_plan=None,
        debate={
            "success": True,
            "judge_decision": {
                "final_action": "open",
                "dimension_assessments": [
                    {
                        "dimension": "technical",
                        "plan_verdict": "support",
                        "summary": "技术确认支持按数学计划等待执行。",
                        "evidence_refs": ["tool:1:analyze_trend"],
                        "scenario_ids": scenario_ids or [],
                        "missing": [],
                    },
                    {
                        "dimension": "capital_flow",
                        "plan_verdict": "support",
                        "summary": "资金证据不阻断数学计划。",
                        "evidence_refs": ["tool:2:get_capital_flow"],
                        "scenario_ids": scenario_ids or [],
                        "missing": [],
                    },
                    {
                        "dimension": "fundamental",
                        "plan_verdict": "support",
                        "summary": "基本面证据不阻断数学计划。",
                        "evidence_refs": ["tool:3:get_tushare_financial_indicators"],
                        "scenario_ids": scenario_ids or [],
                        "missing": [],
                    },
                    {
                        "dimension": "news_event",
                        "plan_verdict": "support",
                        "summary": "消息面未发现阻断计划的事件。",
                        "evidence_refs": ["tool:5:search_stock_news"],
                        "scenario_ids": scenario_ids or [],
                        "missing": [],
                    },
                ],
            },
        },
        provider="test",
        model="test",
    )


def _supporting_review_tool_calls() -> list[dict[str, object]]:
    return [
        {"tool": "analyze_trend", "success": True, "result_preview": "bullish"},
        {"tool": "get_capital_flow", "success": True, "result_preview": "inflow"},
        {
            "tool": "get_tushare_financial_indicators",
            "success": True,
            "result_preview": "profitable",
        },
        {"tool": "get_tushare_forecast", "success": True, "result_preview": "stable"},
        {"tool": "search_stock_news", "success": True, "result_preview": "neutral"},
        {
            "tool": "get_tushare_announcements",
            "success": True,
            "result_preview": "no material event",
        },
    ]


def _snapshot(*, include_chip: bool = True) -> PricePlanSnapshot:
    start = date(2023, 1, 1)
    bars = []
    for index in range(720):
        trend = Decimal("18") + Decimal(index) * Decimal("0.018")
        wave = Decimal(str(round(sin(index / 6) * 0.32, 6)))
        close = trend + wave
        open_price = close - Decimal("0.05")
        bars.append(
            PriceBarSnapshot(
                trade_date=start + timedelta(days=index),
                open=open_price,
                high=close + Decimal("0.24"),
                low=close - Decimal("0.28"),
                close=close,
                volume=Decimal(1_000_000 + index * 1_000),
                amount=(close * Decimal(1_000_000 + index * 1_000)),
            )
        )
    latest = bars[-1]
    chip = None
    if include_chip:
        chip = ChipSnapshot(
            trade_date=latest.trade_date,
            avg_cost=latest.close - Decimal("0.22"),
            cost_70_low=latest.close - Decimal("0.48"),
            cost_70_high=latest.close + Decimal("0.12"),
            cost_90_low=latest.close - Decimal("0.75"),
            cost_90_high=latest.close + Decimal("0.60"),
            source="test-chip",
            source_ref="chip:test:720",
        )
    return PricePlanSnapshot(
        symbol="600519",
        market="cn",
        requested_as_of=latest.trade_date,
        actual_as_of_session=latest.trade_date,
        daily_bars=bars,
        weekly_bars=[bars[index] for index in range(26, len(bars), 7)][-100:],
        last_cash_price=latest.close,
        price_basis="cash_verified",
        price_scale_verified=True,
        history_source="test",
        market_rules=MarketRuleSnapshot(
            snapshot_id="rules-cn-a-v1",
            venue="sse",
            board="main",
            trade_date=latest.trade_date,
            tick_size=Decimal("0.01"),
            buy_lot_size=100,
            price_limit_ratio=Decimal("0.10"),
            previous_close=bars[-2].close,
            limit_up=(bars[-2].close * Decimal("1.10")).quantize(Decimal("0.01")),
            limit_down=(bars[-2].close * Decimal("0.90")).quantize(Decimal("0.01")),
            t_plus_one=True,
            st_status_verified=True,
            listing_status_verified=True,
            instrument_type="common_stock",
            instrument_type_verified=True,
        ),
        chip=chip,
    )


def test_price_plan_is_deterministic_and_scenarios_are_self_contained() -> None:
    snapshot = _snapshot()

    first = evaluate_price_plan(snapshot, PRICE_PLAN_PARAMS_V1)
    second = evaluate_price_plan(snapshot, PRICE_PLAN_PARAMS_V1)

    assert first.model_dump(mode="json", exclude={"created_at"}) == second.model_dump(
        mode="json", exclude={"created_at"}
    )
    assert first.generation_status in {"generated", "observation"}
    assert first.plan_id == second.plan_id
    assert first.data_fingerprint == second.data_fingerprint
    assert first.scenarios
    assert all(scenario.entry_low <= scenario.entry_high for scenario in first.scenarios)
    assert all(scenario.scenario_fingerprint for scenario in first.scenarios)
    formal = [scenario for scenario in first.scenarios if scenario.role != "observation"]
    assert all(scenario.stop_loss is not None for scenario in formal)
    assert all(scenario.target_1 is not None for scenario in formal)
    assert all(scenario.reference_net_rr_1 is not None for scenario in formal)


def test_persisted_v1_anchor_without_source_priority_remains_readable() -> None:
    payload = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1).model_dump(mode="json")
    assert payload["observation_anchors"]
    payload["observation_anchors"][0].pop("source_priority", None)

    restored = PricePlanArtifact.model_validate(payload)

    assert restored.observation_anchors[0].source_priority == 1000


def test_critical_snapshot_failure_returns_explicit_no_plan() -> None:
    snapshot = _snapshot().model_copy(
        update={"price_scale_verified": False, "price_basis": "unverified"}
    )

    artifact = evaluate_price_plan(snapshot, PRICE_PLAN_PARAMS_V1)

    assert artifact.generation_status == "no_plan"
    assert artifact.scenarios == []
    assert "PRICE_SCALE_UNVERIFIED" in artifact.reason_codes


def test_v1_rejects_non_common_stock_instrument() -> None:
    artifact = evaluate_price_plan(
        _snapshot().model_copy(update={"symbol": "510300"}),
        PRICE_PLAN_PARAMS_V1,
    )

    assert artifact.generation_status == "no_plan"
    assert artifact.reason_codes == ["UNSUPPORTED_INSTRUMENT"]


def test_calendar_fallback_never_counts_weekend_as_trading_session(monkeypatch) -> None:
    from src.agent.pricing.plan import _add_weekday_sessions

    monkeypatch.setattr("src.agent.pricing.plan.is_market_open", lambda *_args: True)

    assert _add_weekday_sessions(date(2026, 8, 14), 1) == date(2026, 8, 17)


def test_new_price_plan_contract_rejects_unknown_fields() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError):
        PricePlanSnapshot.model_validate(
            {**snapshot.model_dump(), "markdown_entry_zone": "19.6-20.5"}
        )


def test_snapshot_builder_truncates_as_of_and_does_not_use_future_chip() -> None:
    frame = pd.DataFrame(
        [
            {
                "date": value,
                "open": 10.0 + index * 0.01,
                "high": 10.3 + index * 0.01,
                "low": 9.8 + index * 0.01,
                "close": 10.1 + index * 0.01,
                "volume": 1_000_000,
                "amount": (10.1 + index * 0.01) * 1_000_000,
            }
            for index, value in enumerate(pd.bdate_range("2023-01-02", periods=730))
        ]
    )
    as_of = frame.iloc[-11]["date"].date()

    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "test-history"),
        chip_loader=lambda symbol, target: {
            "status": "ok",
            "date": (target + timedelta(days=1)).isoformat(),
            "avg_cost": 15.0,
            "source": "future-chip",
        },
    ).build("600519", as_of=as_of)

    assert snapshot.actual_as_of_session == as_of
    assert snapshot.daily_bars[-1].trade_date == as_of
    assert all(bar.trade_date <= as_of for bar in snapshot.daily_bars)
    assert all(bar.trade_date <= as_of for bar in snapshot.weekly_bars)
    assert snapshot.price_scale_verified is True
    assert snapshot.chip is None
    assert "chip_distribution" in snapshot.missing_fields


def test_snapshot_builder_excludes_current_unclosed_daily_bar(monkeypatch) -> None:
    frame = pd.DataFrame(
        [
            {
                "date": "2026-08-11",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
            {
                "date": "2026-08-12",
                "open": 10.5,
                "high": 11.5,
                "low": 10,
                "close": 11,
                "volume": 100,
                "amount": 1100,
            },
        ]
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.get_market_now",
        lambda *_args, **_kwargs: datetime(2026, 8, 12, 10, 0),
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.get_effective_trading_date",
        lambda *_args, **_kwargs: date(2026, 8, 11),
    )

    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "test-history"),
        chip_loader=lambda *_args: None,
    ).build("600519", as_of=date(2026, 8, 12))

    assert snapshot.requested_as_of == date(2026, 8, 12)
    assert snapshot.actual_as_of_session == date(2026, 8, 11)
    assert snapshot.daily_bars[-1].trade_date == date(2026, 8, 11)


def test_snapshot_builder_uses_current_daily_bar_only_after_close(monkeypatch) -> None:
    frame = pd.DataFrame(
        [
            {
                "date": "2026-08-12",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
            {
                "date": "2026-08-13",
                "open": 10.5,
                "high": 11.5,
                "low": 10,
                "close": 11,
                "volume": 100,
                "amount": 1100,
            },
        ]
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.get_market_now",
        lambda *_args, **_kwargs: datetime(2026, 8, 13, 18, 0),
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.get_effective_trading_date",
        lambda *_args, **_kwargs: date(2026, 8, 13),
    )

    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "test-history"),
        chip_loader=lambda *_args: None,
    ).build("600519", as_of=date(2026, 8, 13))

    assert snapshot.requested_as_of == date(2026, 8, 13)
    assert snapshot.actual_as_of_session == date(2026, 8, 13)
    assert snapshot.daily_bars[-1].trade_date == date(2026, 8, 13)


def test_live_provider_lag_reuses_current_verified_identity_for_previous_bar(monkeypatch) -> None:
    frame = pd.DataFrame(
        [
            {
                "date": "2026-08-12",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1050,
            },
        ]
    )
    metadata_calls: list[date] = []

    def metadata(_symbol: str, *, trade_date: date):
        metadata_calls.append(trade_date)
        if trade_date != date(2026, 8, 13):
            return None
        return {
            "st_status": False,
            "st_status_verified": True,
            "listing_status": "listed",
            "listing_status_verified": True,
            "instrument_type": "common_stock",
            "instrument_type_verified": True,
            "metadata_observed_on": date(2026, 8, 13),
        }

    monkeypatch.setattr(
        "src.agent.pricing.snapshot.resolve_completed_cn_as_of",
        lambda *_args, **_kwargs: date(2026, 8, 13),
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.current_cn_common_stock_metadata",
        metadata,
    )
    snapshot = PricePlanSnapshotBuilder(
        history_loader=lambda *args, **kwargs: (frame, "lagging-provider"),
        chip_loader=lambda *_args: None,
    ).build("600330", as_of=date(2026, 8, 13))

    assert snapshot.actual_as_of_session == date(2026, 8, 12)
    assert snapshot.market_rules.trade_date == date(2026, 8, 12)
    assert snapshot.market_rules.instrument_type_verified is True
    assert metadata_calls == [date(2026, 8, 12), date(2026, 8, 13)]


def test_runtime_readiness_uses_last_completed_session(tmp_path, monkeypatch) -> None:
    from src.storage import DatabaseManager

    base = _snapshot()
    completed_session = base.actual_as_of_session
    requested_date = completed_session + timedelta(days=1)
    snapshot = base.model_copy(update={"requested_as_of": requested_date})
    checked_dates: list[date] = []

    def current_metadata(_symbol: str, *, trade_date: date):
        checked_dates.append(trade_date)
        if trade_date != completed_session:
            return None
        return {
            "st_status": False,
            "st_status_verified": True,
            "listing_status": "listed",
            "listing_status_verified": True,
            "instrument_type": "common_stock",
            "instrument_type_verified": True,
            "metadata_observed_on": completed_session,
        }

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-runtime-as-of.db'}")
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.resolve_completed_cn_as_of",
        lambda *_args, **_kwargs: completed_session,
    )
    monkeypatch.setattr(
        "src.agent.pricing.snapshot.PricePlanSnapshotBuilder.build",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        "src.agent.pricing.readiness.current_cn_common_stock_metadata",
        current_metadata,
    )
    try:
        result = generate_trade_plan(
            "600519",
            as_of=requested_date,
            db_manager=db,
            persist=False,
        )

        assert checked_dates == [completed_session]
        assert result.execution_status != "system_unready"
        assert result.price_plan.requested_as_of == requested_date
        assert result.price_plan.actual_as_of_session == completed_session
        assert result.price_plan.scenarios
        assert all(
            scenario.valid_from > completed_session
            for scenario in result.price_plan.scenarios
        )
    finally:
        DatabaseManager.reset_instance()


def test_after_close_plan_uses_current_close_for_next_session(tmp_path, monkeypatch) -> None:
    from src.storage import DatabaseManager

    snapshot = _snapshot()
    completed_session = snapshot.actual_as_of_session

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-runtime-after-close.db'}")
    monkeypatch.setattr(
        "src.agent.pricing.readiness.current_cn_common_stock_metadata",
        lambda _symbol, *, trade_date: {
            "st_status": False,
            "st_status_verified": trade_date == completed_session,
            "listing_status": "listed",
            "listing_status_verified": trade_date == completed_session,
            "instrument_type": "common_stock",
            "instrument_type_verified": trade_date == completed_session,
            "metadata_observed_on": completed_session,
        },
    )
    try:
        result = generate_trade_plan(
            "600519",
            as_of=completed_session,
            snapshot=snapshot,
            db_manager=db,
            persist=False,
        )

        assert result.execution_status != "system_unready"
        assert result.price_plan.requested_as_of == completed_session
        assert result.price_plan.actual_as_of_session == completed_session
        assert result.price_plan.scenarios
        assert all(
            scenario.valid_from > completed_session
            for scenario in result.price_plan.scenarios
        )
    finally:
        DatabaseManager.reset_instance()


def test_price_plan_artifact_persistence_is_idempotent(tmp_path) -> None:
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-plan.db'}")
    try:
        artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
        repo = PricePlanRepository(db)

        first = repo.save_artifact(artifact)
        second = repo.save_artifact(artifact.model_copy(update={"trace_ref": "trace-second"}))
        loaded = repo.get_artifact(artifact.plan_id)

        assert first.plan_id == second.plan_id == artifact.plan_id
        assert first.revision == second.revision == 1
        assert loaded == artifact
        states = repo.list_lifecycle_states(artifact.plan_id)
        assert {item.scenario_id for item in states} == {
            item.scenario_id for item in artifact.scenarios
        }
        assert all(item.state == "waiting_touch" for item in states)
        assert repo.list_artifacts_by_trace("trace-second")[0].plan_id == artifact.plan_id
    finally:
        DatabaseManager.reset_instance()


def test_readiness_rejects_historical_date_without_verified_market_rules(
    tmp_path,
    monkeypatch,
) -> None:
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-readiness.db'}")
    monkeypatch.setattr(
        "src.agent.pricing.readiness.current_cn_common_stock_metadata",
        lambda *_args, **_kwargs: None,
    )
    try:
        result = PricePlanReadinessGate(db).evaluate(
            symbol="600519",
            as_of=date(2024, 1, 2),
        )

        assert result.ready is False
        assert result.checks["market_rules"] is False
        assert result.reason_codes == ("SYSTEM_NOT_READY",)
    finally:
        DatabaseManager.reset_instance()


def test_readiness_accepts_exact_verified_market_rule_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-readiness-persisted.db'}")
    monkeypatch.setattr(
        "src.agent.pricing.readiness.current_cn_common_stock_metadata",
        lambda *_args, **_kwargs: None,
    )
    rules = _snapshot().market_rules.model_copy(
        update={"symbol": "600519", "trade_date": date(2024, 1, 2)}
    )
    try:
        PricePlanRepository(db).save_market_rules(rules)

        result = PricePlanReadinessGate(db).evaluate(
            symbol="600519",
            as_of=date(2024, 1, 2),
        )

        assert result.ready is True
        assert result.checks["market_rules"] is True
    finally:
        DatabaseManager.reset_instance()


def test_report_renderer_uses_structured_scenarios_and_is_idempotent() -> None:
    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    result = TradePlanResult(
        price_plan=artifact,
        execution_status="review_pending",
        reason_codes=["REVIEW_PENDING"],
    )

    rendered = render_price_plan_markdown(result)
    once = merge_price_plan_into_report("## 原报告\n\n只保留定性意见。", result)
    twice = merge_price_plan_into_report(once, result)

    assert "## 确定性价格计划" in rendered
    assert "唯一有效来源" in rendered
    assert "入场区间" in rendered
    if any(item.role != "observation" for item in artifact.scenarios):
        assert "止损价" in rendered
        assert "第一目标" in rendered
    else:
        assert "仅供观察，不创建买入信号" in rendered
    assert twice == once
    assert twice.count("deterministic-price-plan:start") == 1


def test_report_renderer_explains_no_plan_without_guessing_prices() -> None:
    artifact = evaluate_price_plan(
        _snapshot().model_copy(
            update={"price_scale_verified": False, "price_basis": "unverified"}
        ),
        PRICE_PLAN_PARAMS_V1,
    )
    rendered = render_price_plan_markdown(
        TradePlanResult(
            price_plan=artifact,
            execution_status="no_plan",
            reason_codes=artifact.reason_codes,
        )
    )

    assert "暂不可生成价格计划" in rendered
    assert "现金价格尺度无法验证" in rendered
    assert "不会回退到大模型猜测价格" in rendered


def test_review_and_account_decision_are_price_less_and_idempotent(tmp_path) -> None:
    from types import SimpleNamespace

    from src.storage import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(f"sqlite:///{tmp_path / 'price-review.db'}")
    try:
        artifact = PricePlanRepository(db).save_artifact(
            evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
        )
        result = SimpleNamespace(
            dashboard={"decision_type": "buy"},
            debate=None,
            provider="test-provider",
            model="test-model",
        )
        review = build_review_artifact(
            artifact,
            result=result,
            tool_calls=[
                {"tool": "analyze_trend", "success": True, "result_preview": "bullish"},
                {"tool": "search_stock_news", "success": True, "result_preview": "neutral"},
            ],
        )
        assert review is not None
        assert "price" not in review.validated_output
        repo = PricePlanRepository(db)
        first_review = repo.save_review(review)
        second_review = repo.save_review(review)
        states = repo.list_lifecycle_states(artifact.plan_id)
        decision = build_account_decision(
            artifact,
            review=first_review,
            lifecycle_states=states,
            risk_profile="balanced",
        )
        first_decision = repo.save_account_decision(decision)
        second_decision = repo.save_account_decision(decision)

        assert first_review.review_id == second_review.review_id
        assert first_decision.decision_id == second_decision.decision_id
        assert first_decision.risk_profile == "balanced"
        assert first_decision.execution_status in {"observe_only", "waiting_confirmation"}
    finally:
        DatabaseManager.reset_instance()


def test_negated_entry_advice_cannot_become_support() -> None:
    from types import SimpleNamespace

    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    review = build_review_artifact(
        artifact,
        result=SimpleNamespace(
            dashboard={"operation_advice": "场外观望，不开新仓"},
            debate=None,
            provider="test-provider",
            model="test-model",
        ),
        tool_calls=[
            {"tool": "analyze_trend", "success": True, "result_preview": "bullish"},
            {"tool": "search_stock_news", "success": True, "result_preview": "neutral"},
        ],
    )

    assert review is not None
    assert review.validated_output["action"] == "wait"
    assert review.conclusion == "insufficient"


def test_failed_judge_is_not_rendered_as_missing_dimension_evidence() -> None:
    from types import SimpleNamespace

    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    result = SimpleNamespace(
        content="等待数学计划。",
        dashboard={},
        trade_plan=None,
        debate={
            "enabled": True,
            "success": False,
            "error": "llm_json_parse_failed:judge_decision",
            "judge_decision": {},
        },
        provider="test-provider",
        model="test-model",
    )
    review = build_review_artifact(
        artifact,
        result=result,
        tool_calls=[
            {"tool": "analyze_trend", "success": True, "result_preview": '{"status":"ok"}'},
            {"tool": "get_capital_flow", "success": True, "result_preview": '{"status":"ok"}'},
        ],
    )

    assert review is not None
    assert review.review_status == "failed"
    assert all(item.review_status == "failed" for item in review.dimension_reviews)
    rendered = merge_price_plan_into_report(
        result.content,
        TradePlanResult(
            price_plan=artifact,
            review=review,
            execution_status="observe_only",
            reason_codes=[],
        ),
    )
    assert "模型证据评审：评审失败" in rendered
    assert "Judge 结构化评审失败" in rendered
    assert "已取得该维度证据，但模型未给出" not in rendered
    assert "四维影响：技术面评审失败；资金面评审失败" in rendered
    assert "四维影响：技术面证据不足" not in rendered


def test_llm_execution_prices_fail_review_and_are_removed_from_report() -> None:
    from types import SimpleNamespace

    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    model_content = "## 交易计划\n\n- 理想入场区间：777.77 - 788.88 元\n- 定性结论：等待回踩。"
    review = build_review_artifact(
        artifact,
        result=SimpleNamespace(
            content=model_content,
            dashboard={
                "decision_type": "buy",
                "dashboard": {"battle_plan": {"sniper_points": {"ideal_buy": 777.77}}},
            },
            debate=None,
            trade_plan=None,
            provider="test-provider",
            model="test-model",
        ),
        tool_calls=[
            {"tool": "analyze_trend", "success": True},
            {"tool": "search_stock_news", "success": True},
        ],
    )

    assert review is not None
    assert review.review_status == "failed"
    assert review.conclusion == "insufficient"
    assert review.validated_output["price_fields_present"] is True
    rendered = merge_price_plan_into_report(
        model_content,
        TradePlanResult(
            price_plan=artifact,
            review=review,
            execution_status="observe_only",
            reason_codes=["LLM_PRICE_AUTHORITY_VIOLATION"],
        ),
    )
    assert "777.77" not in rendered
    assert "788.88" not in rendered
    assert "模型越权输出价格" in rendered
    assert "定性结论：等待回踩" in rendered


def test_llm_price_violation_does_not_erase_verified_dimension_verdicts() -> None:
    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    result = _supporting_review_result()
    result.content = "等待确认；模型曾越权给出理想入场区间：777.77 - 788.88 元。"

    review = build_review_artifact(
        artifact,
        result=result,
        tool_calls=_supporting_review_tool_calls(),
    )

    assert review is not None
    assert review.review_status == "completed"
    assert review.conclusion == "support"
    assert all(
        "MODEL_PLAN_VERDICT" in item.reason_codes
        for item in review.dimension_reviews
    )
    rendered = merge_price_plan_into_report(
        result.content,
        TradePlanResult(
            price_plan=artifact,
            review=review,
            execution_status="observe_only",
            reason_codes=[],
        ),
    )
    assert "777.77" not in rendered
    assert "788.88" not in rendered
    assert "合规的非价格维度意见继续按固定规则评审" in rendered


def test_unverified_dimension_support_cannot_raise_scenario_permission() -> None:
    result = _supporting_review_result()
    result.debate["judge_decision"]["dimension_assessments"][0]["evidence_refs"] = [
        "tool:999:analyze_trend"
    ]
    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)

    review = build_review_artifact(
        artifact,
        result=result,
        tool_calls=_supporting_review_tool_calls(),
    )

    assert review is not None
    technical = next(
        item for item in review.dimension_reviews if item.dimension == "technical"
    )
    assert technical.verdict == "insufficient"
    assert "MODEL_PLAN_VERDICT" not in technical.reason_codes
    assert all(item.final_effect == "observe_only" for item in review.scenario_impacts)


def test_authoritative_math_block_is_not_misclassified_as_llm_price_output() -> None:
    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    result = _supporting_review_result()
    result.content = render_price_plan_markdown(
        TradePlanResult(
            price_plan=artifact,
            execution_status="observe_only",
            reason_codes=[],
        )
    )

    review = build_review_artifact(
        artifact,
        result=result,
        tool_calls=_supporting_review_tool_calls(),
    )

    assert review is not None
    assert review.validated_output["price_fields_present"] is False


def test_review_connects_dimension_verdicts_to_each_math_scenario() -> None:
    from types import SimpleNamespace

    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    result = SimpleNamespace(
        content="## 最终结论\n\n暂不入场。",
        dashboard=None,
        trade_plan=None,
        provider="test-provider",
        model="test-model",
        debate={
            "success": True,
            "primary_thesis": {"action": "open"},
            "opposing_thesis": {"action": "reject"},
            "judge_decision": {
                "final_action": "reject",
                "dimension_assessments": [
                    {
                        "dimension": "technical",
                        "plan_verdict": "support",
                        "summary": "技术证据支持主计划。",
                        "evidence_refs": ["tool:1:analyze_trend"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "capital_flow",
                        "plan_verdict": "support",
                        "summary": "资金证据支持主计划。",
                        "evidence_refs": ["tool:2:get_capital_flow"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "fundamental",
                        "plan_verdict": "support",
                        "summary": "基本面证据不阻断主计划。",
                        "evidence_refs": ["tool:3:get_tushare_financial_indicators"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "news_event",
                        "plan_verdict": "support",
                        "summary": "消息面证据不阻断主计划。",
                        "evidence_refs": ["tool:5:search_stock_news"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "technical",
                        "plan_verdict": "opposing",
                        "summary": "周线趋势与日线二卖均不支持当前入场。",
                        "evidence": ["日线二卖", "周线趋势相反"],
                        "evidence_refs": ["tool:1:analyze_trend"],
                        "missing": [],
                    },
                    {
                        "dimension": "capital_flow",
                        "plan_verdict": "support",
                        "summary": "近期主力资金保持净流入。",
                        "evidence": ["五日主力净流入"],
                        "evidence_refs": ["tool:2:get_capital_flow"],
                        "missing": [],
                    },
                    {
                        "dimension": "fundamental",
                        "plan_verdict": "hard_veto",
                        "summary": "连续亏损构成股票级硬约束。",
                        "evidence": ["业绩预告首亏"],
                        "evidence_refs": ["tool:3:get_tushare_forecast"],
                        "missing": [],
                    },
                    {
                        "dimension": "news_event",
                        "plan_verdict": "insufficient",
                        "summary": "没有确认新增催化。",
                        "evidence": [],
                        "missing": ["最新公告"],
                    },
                ],
            },
        },
    )
    tool_calls = [
        {"tool": "analyze_trend", "success": True, "result_preview": "bullish"},
        {"tool": "get_capital_flow", "success": True, "result_preview": "inflow"},
        {"tool": "get_tushare_forecast", "success": True, "result_preview": "loss"},
        {"tool": "search_stock_fundamentals", "success": True, "result_preview": "loss"},
        {"tool": "search_stock_news", "success": True, "result_preview": "{}"},
    ]

    review = build_review_artifact(artifact, result=result, tool_calls=tool_calls)

    assert review is not None
    reviews = {item.dimension: item for item in review.dimension_reviews}
    assert reviews["technical"].verdict == "opposing"
    assert reviews["capital"].verdict == "support"
    assert reviews["fundamental"].verdict == "hard_veto"
    assert reviews["fundamental"].execution_effect == "veto"
    assert reviews["intel"].verdict == "insufficient"
    assert len(review.scenario_impacts) == len(artifact.scenarios)
    assert {item.scenario_id for item in review.scenario_impacts} == {
        item.scenario_id for item in artifact.scenarios
    }
    assert all(item.final_effect == "veto" for item in review.scenario_impacts)

    rendered = render_price_plan_markdown(
        TradePlanResult(
            price_plan=artifact,
            review=review,
            execution_status="observe_only",
            reason_codes=artifact.reason_codes,
        )
    )
    assert "价格形成与四维评审" in rendered
    assert "价格依据" in rendered
    assert "周线趋势与日线二卖均不支持当前入场" in rendered
    assert "近期主力资金保持净流入" in rendered
    assert "数学模型先生成区间，四维评审只改变执行权限" in rendered


def test_scenario_specific_opposition_does_not_downgrade_other_scenarios() -> None:
    from types import SimpleNamespace

    base = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    first = base.scenarios[0].model_copy(
        update={
            "role": "primary",
            "stop_loss": base.scenarios[0].entry_low - Decimal("1"),
            "target_1": base.scenarios[0].entry_high + Decimal("3"),
            "target_2": base.scenarios[0].entry_high + Decimal("5"),
            "reference_net_rr_1": Decimal("2"),
            "reference_net_risk": Decimal("1"),
            "reference_net_reward_1": Decimal("2"),
            "confirmation_rules": [{"rule_type": "confirm", "params": {}, "rule_version": "v1"}],
            "invalidation_rules": [{"rule_type": "stop", "params": {}, "rule_version": "v1"}],
        }
    )
    second = first.model_copy(
        update={
            "scenario_id": "scenario-secondary-isolated",
            "role": "secondary",
            "base_rank": 2,
        }
    )
    artifact = base.model_copy(
        update={"generation_status": "generated", "scenarios": [first, second], "reason_codes": []}
    )
    result = SimpleNamespace(
        content="等待数学计划。",
        dashboard=None,
        trade_plan=None,
        provider="test",
        model="test",
        debate={
            "success": True,
            "judge_decision": {
                "final_action": "wait",
                "dimension_assessments": [
                    {
                        "dimension": "technical",
                        "plan_verdict": "support",
                        "summary": "技术证据支持主计划。",
                        "evidence_refs": ["tool:1:analyze_trend"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "capital_flow",
                        "plan_verdict": "support",
                        "summary": "资金证据支持主计划。",
                        "evidence_refs": ["tool:2:get_capital_flow"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "fundamental",
                        "plan_verdict": "support",
                        "summary": "基本面证据不阻断主计划。",
                        "evidence_refs": ["tool:3:get_tushare_financial_indicators"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "news_event",
                        "plan_verdict": "support",
                        "summary": "消息面证据不阻断主计划。",
                        "evidence_refs": ["tool:5:search_stock_news"],
                        "scenario_ids": [],
                    },
                    {
                        "dimension": "technical",
                        "plan_verdict": "opposing",
                        "summary": "次场景技术结构不支持。",
                        "evidence_refs": ["tool:1:analyze_trend"],
                        "scenario_ids": [second.scenario_id],
                    },
                ],
            },
        },
    )

    review = build_review_artifact(
        artifact,
        result=result,
        tool_calls=_supporting_review_tool_calls(),
    )

    assert review is not None
    impacts = {item.scenario_id: item.final_effect for item in review.scenario_impacts}
    assert impacts[first.scenario_id] == "keep"
    assert impacts[second.scenario_id] == "observe_only"


def test_unconfirmed_right_edge_swing_is_observation_only() -> None:
    snapshot = _snapshot(include_chip=False)
    right_edge = len(snapshot.daily_bars) - 1
    snapshot = snapshot.model_copy(
        update={
            "structure_evidence": {
                "smc": {
                    "latest_swings": [
                        {
                            "index": right_edge,
                            "price": float(snapshot.daily_bars[-1].low),
                            "type": "low",
                            "label": "right-edge",
                            "time": snapshot.daily_bars[-1].trade_date.isoformat(),
                        }
                    ]
                },
                "chan": {"latest_centers": []},
            }
        }
    )

    artifact = evaluate_price_plan(snapshot, PRICE_PLAN_PARAMS_V1)
    anchor = next(
        item
        for item in artifact.observation_anchors
        if item.source_ref.endswith(":right-edge:719")
    )
    assert anchor.anchor_status == "observation"
    assert "STRUCTURE_UNCONFIRMED" in anchor.reason_codes


def test_signal_lifecycle_honors_account_selected_scenario() -> None:
    artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    eligible = [item for item in artifact.scenarios if item.role != "observation"]
    if len(eligible) < 2:
        pytest.skip("fixture produced fewer than two selectable scenarios")

    selected = eligible[-1]
    resolved = SignalLifecycleService._select_price_plan_scenario(
        artifact,
        selected_scenario_id=selected.scenario_id,
    )

    assert resolved is not None
    assert resolved.scenario_id == selected.scenario_id


def test_account_decision_uses_only_deterministic_position_sizing_cap() -> None:
    from types import SimpleNamespace

    base_artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    observation = base_artifact.scenarios[0]
    formal = observation.model_copy(
        update={
            "role": "primary",
            "stop_loss": observation.entry_low - Decimal("1"),
            "target_1": observation.entry_high + Decimal("2"),
            "target_2": observation.entry_high + Decimal("3"),
            "reference_net_rr_1": Decimal("2"),
            "reference_net_risk": Decimal("1"),
            "reference_net_reward_1": Decimal("2"),
            "confirmation_rules": [
                {"rule_type": "closed_bar_reversal", "params": {}, "rule_version": "v1"}
            ],
            "invalidation_rules": [
                {"rule_type": "close_at_or_below_stop", "params": {}, "rule_version": "v1"}
            ],
        }
    )
    artifact = base_artifact.model_copy(
        update={
            "generation_status": "generated",
            "scenarios": [formal],
            "reason_codes": [],
        }
    )
    formal_scenarios = [formal]
    review = build_review_artifact(
        artifact,
        result=_supporting_review_result(),
        tool_calls=_supporting_review_tool_calls(),
    )
    assert review is not None
    confirmed_states = [
        PlanLifecycleState(
            plan_id=artifact.plan_id,
            scenario_id=scenario.scenario_id,
            state_version=2,
            state="confirmed",
            confirmed_at=datetime.combine(scenario.valid_from, datetime.min.time()),
            last_evaluated_as_of=scenario.valid_from,
            last_event_id=f"event-confirmed-{scenario.scenario_id}",
        )
        for scenario in formal_scenarios
    ]

    decision = build_account_decision(
        artifact,
        review=review,
        lifecycle_states=confirmed_states,
        risk_profile="balanced",
        position_sizing={
            "status": "ok",
            "target_position_cap_pct": 20,
            "position_tier": "S",
            "market_coefficient": 1.0,
        },
        account_position_cap_pct=Decimal("12"),
    )

    assert decision.target_position_pct == Decimal("12.000000")
    assert decision.position_sizing_ref == "position_sizing.v1:S:1.0"
    assert decision.reason_codes == ["RISK_GATE_PENDING"]

    passed = finalize_account_decision(
        decision,
        risk_gate_result={"status": "passed", "allowed_action": "open", "checks": []},
    )
    blocked = finalize_account_decision(
        decision,
        risk_gate_result={"status": "blocked", "allowed_action": "wait", "checks": []},
    )
    assert passed.execution_status == "actionable"
    assert passed.reason_codes == []
    assert passed.risk_gate_ref.startswith("risk-gate:")
    assert blocked.execution_status == "risk_blocked"
    assert blocked.reason_codes == ["RISK_GATE_BLOCKED"]


def test_account_cost_policy_recomputes_net_rr_and_lot_quantity() -> None:
    from types import SimpleNamespace

    base_artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    observation = base_artifact.scenarios[0]
    formal = observation.model_copy(
        update={
            "role": "primary",
            "stop_loss": observation.entry_low - Decimal("1"),
            "target_1": observation.entry_high + Decimal("3"),
            "target_2": observation.entry_high + Decimal("5"),
            "reference_net_rr_1": Decimal("2"),
            "reference_net_risk": Decimal("1"),
            "reference_net_reward_1": Decimal("2"),
            "confirmation_rules": [
                {"rule_type": "closed_bar_reversal", "params": {}, "rule_version": "v1"}
            ],
            "invalidation_rules": [
                {"rule_type": "close_at_or_below_stop", "params": {}, "rule_version": "v1"}
            ],
        }
    )
    artifact = base_artifact.model_copy(
        update={"generation_status": "generated", "scenarios": [formal], "reason_codes": []}
    )
    review = build_review_artifact(
        artifact,
        result=_supporting_review_result(),
        tool_calls=_supporting_review_tool_calls(),
    )
    state = PlanLifecycleState(
        plan_id=artifact.plan_id,
        scenario_id=formal.scenario_id,
        state_version=2,
        state="confirmed",
        confirmed_at=datetime.combine(formal.valid_from, datetime.min.time()),
        last_evaluated_as_of=formal.valid_from,
        last_event_id="event-confirmed",
    )

    decision = build_account_decision(
        artifact,
        review=review,
        lifecycle_states=[state],
        risk_profile="aggressive",
        position_sizing={"status": "ok", "target_position_cap_pct": 20},
        account_execution={
            "total_equity": 100_000,
            "available_cash": 50_000,
            "buy_commission_bps": 3,
            "sell_commission_bps": 3,
            "minimum_commission": 5,
            "transfer_fee_bps": 0.1,
            "stamp_duty_bps": 5,
            "cost_policy_version": "cn-account-cost.v1",
        },
        lot_size=100,
    )

    assert decision.account_context_absent is False
    assert decision.account_net_rr_1 is not None
    assert decision.planned_quantity is not None
    assert decision.planned_quantity % 100 == 0
    assert decision.account_cost_policy_ref == "cn-account-cost.v1"


def test_explicit_account_with_missing_cost_policy_is_risk_blocked() -> None:
    from types import SimpleNamespace

    base_artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    observation = base_artifact.scenarios[0]
    formal = observation.model_copy(
        update={
            "role": "primary",
            "stop_loss": observation.entry_low - Decimal("1"),
            "target_1": observation.entry_high + Decimal("3"),
            "target_2": observation.entry_high + Decimal("5"),
            "reference_net_rr_1": Decimal("2"),
            "reference_net_risk": Decimal("1"),
            "reference_net_reward_1": Decimal("2"),
            "confirmation_rules": [{"rule_type": "confirm", "params": {}, "rule_version": "v1"}],
            "invalidation_rules": [{"rule_type": "stop", "params": {}, "rule_version": "v1"}],
        }
    )
    artifact = base_artifact.model_copy(
        update={"generation_status": "generated", "scenarios": [formal], "reason_codes": []}
    )
    review = build_review_artifact(
        artifact,
        result=_supporting_review_result(),
        tool_calls=_supporting_review_tool_calls(),
    )
    state = PlanLifecycleState(
        plan_id=artifact.plan_id,
        scenario_id=formal.scenario_id,
        state_version=2,
        state="confirmed",
        confirmed_at=datetime.combine(formal.valid_from, datetime.min.time()),
        last_evaluated_as_of=formal.valid_from,
        last_event_id="event-confirmed",
    )

    decision = build_account_decision(
        artifact,
        review=review,
        lifecycle_states=[state],
        risk_profile="aggressive",
        position_sizing={"status": "ok", "target_position_cap_pct": 20},
        account_execution={"total_equity": 100_000, "available_cash": 50_000},
    )

    assert decision.execution_status == "risk_blocked"
    assert decision.reason_codes == ["ACCOUNT_COST_POLICY_UNAVAILABLE"]


def test_report_renders_review_and_account_execution_decision() -> None:
    from types import SimpleNamespace

    base_artifact = evaluate_price_plan(_snapshot(), PRICE_PLAN_PARAMS_V1)
    observation = base_artifact.scenarios[0]
    formal = observation.model_copy(
        update={
            "role": "primary",
            "stop_loss": observation.entry_low - Decimal("1"),
            "target_1": observation.entry_high + Decimal("2"),
            "target_2": observation.entry_high + Decimal("3"),
            "reference_net_rr_1": Decimal("2"),
            "reference_net_risk": Decimal("1"),
            "reference_net_reward_1": Decimal("2"),
            "confirmation_rules": [
                {"rule_type": "closed_bar_reversal", "params": {}, "rule_version": "v1"}
            ],
            "invalidation_rules": [
                {"rule_type": "close_at_or_below_stop", "params": {}, "rule_version": "v1"}
            ],
        }
    )
    artifact = base_artifact.model_copy(
        update={"generation_status": "generated", "scenarios": [formal], "reason_codes": []}
    )
    review = build_review_artifact(
        artifact,
        result=_supporting_review_result(),
        tool_calls=_supporting_review_tool_calls(),
    )
    assert review is not None
    decision = build_account_decision(
        artifact,
        review=review,
        lifecycle_states=[],
        risk_profile="conservative",
        position_sizing={
            "status": "ok",
            "target_position_cap_pct": 20,
            "position_tier": "S",
            "market_coefficient": 1,
        },
    )
    result = TradePlanResult(
        price_plan=artifact,
        review=review,
        account_decision=decision,
        execution_status=decision.execution_status,
        reason_codes=decision.reason_codes,
    )

    rendered = render_price_plan_markdown(result)

    assert "### 执行判决" in rendered
    assert "风险偏好：稳健型" in rendered
    assert "等待技术确认" in rendered
    assert decision.selected_scenario_id in rendered


def test_plain_context_keeps_risk_profile_separate_from_position_cap() -> None:
    context = {
        "risk_preference": "aggressive",
        "max_single_position_pct": 12.5,
    }

    assert _risk_profile(context) == "aggressive"
    assert _account_position_cap_pct(context) == Decimal("12.5")
