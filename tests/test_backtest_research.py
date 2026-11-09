# -*- coding: utf-8 -*-
"""P3 research vertical slice tests with a fake LLM (no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from src.backtest.calendar import CnTradingCalendar
from src.backtest.entities import DailyBar
from src.backtest.research.dataset import build_research_dataset, resolve_research_universe
from src.backtest.research.orchestrator import ResearchOrchestrator
from src.backtest.research.market import TechnicalSnapshot, build_research_plan
from src.backtest.research.sequoia import SequoiaDailySource
from src.backtest.rules import MarketRule
from src.backtest.runner import EngineIdentity, RunnerOptions, run_experiment
from src.backtest.schemas import (
    ResearchUniverseMode,
    ResearchConfirmationMode,
    CorporateActionPolicy,
    ExperimentConfig,
    Granularity,
    ResearchConfig,
    RiskProfile,
    SourcePipeline,
)
from src.backtest.snapshot import SnapshotBuilder


CAL = CnTradingCalendar()
START = date(2025, 8, 1)
DECISION_START = date(2026, 1, 5)
DECISION_END = date(2026, 3, 31)


@dataclass
class FakeResponse:
    content: str
    model: str = "fake/research-model"
    usage: dict | None = None
    provider: str = "fake"
    finish_reason: str = "stop"


class FakeAdapter:
    is_available = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_text(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        system = messages[0]["content"]
        if "选股研究 Agent" in system:
            body = {
                "selected": [
                    {"symbol": "600519", "rank": 1, "confidence": 0.9, "thesis": "as-of trend"}
                ],
                "no_trade_reason": None,
            }
            return FakeResponse(content=json.dumps(body), usage={"total_tokens": 10})
        body = {"decision": "accept", "confidence": 0.9, "reason": "as-of plan acceptable"}
        return FakeResponse(content=json.dumps(body), usage={"total_tokens": 5})


def _bars() -> list[DailyBar]:
    sessions = CAL.sessions(START, DECISION_END)
    result = []
    price = Decimal("10")
    for index, session in enumerate(sessions):
        price = price * Decimal("1.001")
        result.append(
            DailyBar(
                symbol="600519",
                session=session,
                open=price * Decimal("0.999"),
                high=price * Decimal("1.02"),
                low=price * Decimal("0.98"),
                close=price,
                volume=Decimal("1000000"),
                amount=Decimal("0"),
                source="test",
            )
        )
    return result


def _config(max_calls: int = 10) -> ExperimentConfig:
    return ExperimentConfig(
        mode="historical_agent_research",
        source_pipeline=SourcePipeline.RESEARCH,
        start_date=DECISION_START,
        end_date=DECISION_END,
        initial_cash=Decimal("100000"),
        granularity=Granularity.DAILY,
        price_basis="daily_declared_unadjusted",
        risk_profile=RiskProfile.BALANCED,
        corporate_action_policy=CorporateActionPolicy.ASSUME_NONE,
        fee_policy_ref="cn-a-share-zero-cost.v1",
        research=ResearchConfig(
            universe=["600519"],
            selection_top_n=1,
            max_confirmations_per_decision=1,
            max_llm_calls=max_calls,
            min_history_bars=60,
            confirmation_mode="llm",
        ),
    )


def _snapshot(config: ExperimentConfig):
    builder = SnapshotBuilder(
        run_id="bt-research-test",
        config=config,
        plans=[],
        calendar=CAL,
        daily_bars=_bars(),
        rules=[],
        symbols_override=["600519"],
    )
    return builder.build()


class StaticBuilder:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def build(self):
        return self.snapshot


def test_p3_selection_confirmation_generates_plans_without_recorded_plans(tmp_path):
    config = _config()
    snapshot = _snapshot(config)
    adapter = FakeAdapter()
    result = run_experiment(
        run_id="bt-research-test",
        config=config,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CAL,
            engine_identity=EngineIdentity("test", "abc", False),
            snapshot_builder=StaticBuilder(snapshot),
            research_llm_adapter=adapter,
        ),
    )
    assert result.status == "partial"
    assert result.plans
    assert result.summary["research"]["labels"]
    assert result.summary["research"]["llm_calls"] >= 2
    assert result.summary["research"]["decision_dates"]
    assert result.result_dir is not None
    assert (result.result_dir / "research_cache.sqlite").exists()
    assert len(adapter.calls) >= 2
    # The fake adapter saw only as-of payloads; no future decision dates.
    for call in adapter.calls:
        payload = json.loads(call["messages"][1]["content"])
        assert payload.get("as_of", "9999-12-31") <= "2026-03-31"


def test_p3_budget_is_enforced_and_marks_budget_exhausted(tmp_path):
    config = _config(max_calls=1)
    snapshot = _snapshot(config)
    adapter = FakeAdapter()
    result = run_experiment(
        run_id="bt-research-budget",
        config=config,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CAL,
            engine_identity=EngineIdentity("test", "abc", False),
            snapshot_builder=StaticBuilder(snapshot),
            research_llm_adapter=adapter,
        ),
    )
    assert result.status == "partial"
    assert result.summary["research"]["budget_exhausted"] is True
    assert len(adapter.calls) <= 1


def test_p3_precheck_has_no_no_included_plans(tmp_path):
    config = _config()
    snapshot = _snapshot(config)
    orchestrator = ResearchOrchestrator(
        config=config,
        data=snapshot.data,
        calendar=CAL,
        cache_path=None,
        llm_adapter=FakeAdapter(),
    )
    try:
        blockers, warnings, diagnostics = orchestrator.research_precheck(snapshot)
    finally:
        orchestrator.close()
    assert "no_included_plans" not in blockers
    assert diagnostics["research"]["estimated_llm_calls"] > 0
    assert "survivor_bias" in diagnostics["research"]["labels"]
    assert diagnostics["research"]["universe"] == ["600519"]
def test_p3_auto_universe_resolves_full_market_sequoia_and_rebases_cash_prices():
    source = SequoiaDailySource()
    if not source.available:
        return
    config = _config()
    config.research.universe_mode = ResearchUniverseMode.AUTO_LOCAL_COVERAGE
    config.research.universe = []
    config.research.auto_universe_limit = 20
    config.research.min_history_bars = 40
    symbols, source_label = resolve_research_universe(config, db_manager=None)
    assert source_label == "sequoia_local_coverage"
    assert len(symbols) > 5
    assert "000300.SH" not in symbols
    dataset = build_research_dataset(
        config,
        calendar=CAL,
        universe=["600519"],
        db_manager=None,
    )
    assert dataset.universe_source == "sequoia_local_coverage"
    assert "research_data_source:sequoia" in dataset.snapshot.quality_flags
    assert "research_price_basis_rebased" in dataset.snapshot.quality_flags
    bars = dataset.snapshot.data.daily_bars("600519", DECISION_END)
    assert bars
    # 600519 in the raw Sequoia DB is ~11k (hfq); cash scale is ~1.4k.
    assert Decimal("500") < bars[-1].close < Decimal("5000")


def test_p3_auto_universe_runner_uses_override_and_labels(tmp_path):
    config = _config()
    config.research.universe_mode = ResearchUniverseMode.AUTO_LOCAL_COVERAGE
    config.research.universe = []
    snapshot = _snapshot(config)
    adapter = FakeAdapter()
    result = run_experiment(
        run_id="bt-research-auto",
        config=config,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CAL,
            engine_identity=EngineIdentity("test", "abc", False),
            snapshot_builder=StaticBuilder(snapshot),
            research_llm_adapter=adapter,
            research_universe=["600519"],
        ),
    )
    assert result.status == "partial"
    assert "local_coverage_universe" in result.summary["research"]["labels"]
    assert result.summary["research"]["universe_source"] == "override"
    assert result.summary["research"]["universe"] == ["600519"]

def test_p3_auto_accept_skips_confirmation_llm(tmp_path):
    config = _config()
    config.research.confirmation_mode = ResearchConfirmationMode.AUTO_ACCEPT
    snapshot = _snapshot(config)
    adapter = FakeAdapter()
    result = run_experiment(
        run_id="bt-research-auto-accept",
        config=config,
        options=RunnerOptions(
            runs_root=tmp_path,
            calendar=CAL,
            engine_identity=EngineIdentity("test", "abc", False),
            snapshot_builder=StaticBuilder(snapshot),
            research_llm_adapter=adapter,
        ),
    )
    assert result.status == "partial"
    assert result.summary["research"]["confirmation_mode"] == "auto_accept"
    assert "auto_accept_selection" in result.summary["research"]["labels"]
    assert result.summary["research"]["llm_calls"] == len(result.summary["research"]["decision_dates"])
    assert all(
        not str(call.get("stage", "")).startswith("confirmation:")
        for call in result.summary["research"]["call_records"]
    )
    assert result.plans

def test_research_plan_uses_adaptive_breakout_and_15_day_validity():
    snapshot = TechnicalSnapshot(
        symbol="600519",
        as_of=date(2026, 1, 5),
        close=Decimal("10.90"),
        prev_close=Decimal("10.80"),
        return_5d=0.01,
        return_20d=0.06,
        return_60d=0.12,
        ma20=Decimal("10.50"),
        ma60=Decimal("10.20"),
        atr14=Decimal("0.30"),
        atr_pct=0.0275,
        high20=Decimal("11.00"),
        low20=Decimal("9.80"),
        volume_ratio=1.1,
        score=0.2,
        bars_count=80,
    )
    rule = MarketRule(
        symbol="600519",
        session=date(2026, 1, 6),
        venue="XSHG",
        board="main",
    )
    draft = build_research_plan(
        config=_config(),
        snapshot=snapshot,
        rule=rule,
        available_at=datetime.combine(date(2026, 1, 5), datetime.min.time()),
        valid_from=date(2026, 1, 6),
        rank=1,
    )
    assert draft is not None
    assert draft.plan.order_valid_trading_days == 15
    assert "research_entry_breakout" in draft.plan.quality_flags
    assert draft.plan.entry_high >= snapshot.high20

