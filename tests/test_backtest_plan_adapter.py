# -*- coding: utf-8 -*-
"""Structured-artifact adapter and explicit selection-link tests."""

from __future__ import annotations

from datetime import datetime

from src.backtest.plan_adapter import PlanAdapter
from src.backtest.schemas import SelectionLink, SourcePipeline
from tests.backtest_helpers import CALENDAR, make_artifact


def _adapter(config, artifact=None, decision=None, **kwargs):
    artifact = artifact or make_artifact()[0]
    decision = decision if decision is not None else make_artifact()[1]
    return PlanAdapter(
        config=config,
        calendar=CALENDAR,
        artifacts=[artifact],
        decisions={artifact.plan_id: decision},
        trace_links={artifact.plan_id: ["trace-1"]},
        **kwargs,
    )


def test_actionable_artifact_is_included_for_single_stock_pipeline():
    from tests.backtest_helpers import config as make_config

    config = make_config(source_pipeline=SourcePipeline.SINGLE_STOCK)
    result = _adapter(config).adapt()
    assert result.included_count == 1
    assert result.plans[0].source_pipeline is SourcePipeline.SINGLE_STOCK
    assert result.plans[0].requires_confirmation is False
    assert result.plans[0].valid_from.isoformat() == "2026-01-05"


def test_selection_confirmed_requires_explicit_parent_link():
    from tests.backtest_helpers import config as make_config

    config = make_config(source_pipeline=SourcePipeline.SELECTION_CONFIRMED)
    result = _adapter(config).adapt()
    assert result.included_count == 0
    assert result.excluded_count == 1
    assert result.inputs[0].exclusion_reason == "missing_selection_link"

    link = SelectionLink(
        selection_trace_ref="selection-1",
        confirmation_trace_ref="trace-1",
        symbol="600519",
    )
    config = make_config(
        source_pipeline=SourcePipeline.SELECTION_CONFIRMED,
        selection_links=[link],
    )
    result = _adapter(config).adapt()
    assert result.included_count == 1
    assert result.plans[0].source_pipeline is SourcePipeline.SELECTION_CONFIRMED
    assert result.plans[0].selection_trace_ref == "selection-1"
    assert result.plans[0].link_quality == "explicit"


def test_single_stock_pipeline_excludes_selection_linked_plan():
    from tests.backtest_helpers import config as make_config

    link = SelectionLink(
        selection_trace_ref="selection-1",
        confirmation_trace_ref="trace-1",
        symbol="600519",
    )
    config = make_config(source_pipeline=SourcePipeline.SINGLE_STOCK, selection_links=[link])
    result = _adapter(config).adapt()
    assert result.included_count == 0
    assert result.inputs[0].exclusion_reason == "belongs_to_selection_pipeline"


def test_missing_account_decision_is_excluded_not_silently_dropped():
    from tests.backtest_helpers import config as make_config

    artifact = make_artifact()[0]
    config = make_config()
    adapter = PlanAdapter(
        config=config,
        calendar=CALENDAR,
        artifacts=[artifact],
        decisions={},
        trace_links={artifact.plan_id: ["trace-1"]},
    )
    result = adapter.adapt()
    assert result.included_count == 0
    assert result.inputs[0].exclusion_reason == "missing_account_decision"


def test_observe_only_decision_is_excluded():
    from tests.backtest_helpers import config as make_config

    artifact, decision = make_artifact()
    decision = decision.model_copy(update={"execution_status": "observe_only"})
    config = make_config()
    result = _adapter(config, artifact=artifact, decision=decision).adapt()
    assert result.included_count == 0
    assert result.inputs[0].exclusion_reason == "decision_status:observe_only"


def test_selection_top_n_excludes_lower_rank_with_reason():
    from tests.backtest_helpers import config as make_config

    first_artifact, first_decision = make_artifact(plan_id="plan-1")
    second_artifact, second_decision = make_artifact(plan_id="plan-2")
    second_artifact.scenarios[0].base_rank = 2
    link1 = SelectionLink(selection_trace_ref="selection-1", confirmation_trace_ref="trace-1", symbol="600519")
    link2 = SelectionLink(selection_trace_ref="selection-1", confirmation_trace_ref="trace-2", symbol="600519")
    config = make_config(
        source_pipeline=SourcePipeline.SELECTION_CONFIRMED,
        selection_links=[link1, link2],
    )
    config.risk_policy.selection_top_n = 1
    adapter = PlanAdapter(
        config=config,
        calendar=CALENDAR,
        artifacts=[first_artifact, second_artifact],
        decisions={"plan-1": first_decision, "plan-2": second_decision},
        trace_links={"plan-1": ["trace-1"], "plan-2": ["trace-2"]},
    )
    result = adapter.adapt()
    assert result.included_count == 1
    assert result.plans[0].plan_id == "plan-1"
    assert any(item.exclusion_reason == "selection_top_n_exceeded" for item in result.inputs)


def test_weekend_generation_cannot_trade_friday():
    from tests.backtest_helpers import config as make_config

    artifact, decision = make_artifact(created_at=datetime(2026, 1, 3, 12, 0))
    config = make_config()
    result = _adapter(config, artifact=artifact, decision=decision).adapt()
    assert result.included_count == 1
    assert result.plans[0].available_at.date().isoformat() == "2026-01-03"
    assert result.plans[0].valid_from.isoformat() == "2026-01-05"
