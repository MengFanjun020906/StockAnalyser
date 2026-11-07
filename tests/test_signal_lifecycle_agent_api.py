# -*- coding: utf-8 -*-
"""End-to-end API contract for Agent-created signal previews."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

import src.auth as auth
from api.app import create_app
from src.config import Config
from src.agent.pricing.fingerprint import fingerprint
from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.schemas import (
    GenerationStatus,
    MarketRuleSnapshot,
    PricePlanArtifact,
    PriceRelation,
    ReachabilityBand,
    RuleSpec,
    ScenarioRole,
    TradePlanResult,
    ZoneScenario,
)
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


def _structured_price_plan(symbol: str, *, trace_ref: str | None = None) -> TradePlanResult:
    as_of = date(2026, 8, 11)
    scenario_values = (
        [("primary", "1470", "1500", "1420", "1560", "1620")]
        if symbol == "600519"
        else [
            ("primary", "19.6", "20.5", "18.5", "23", "25"),
            ("secondary", "21.2", "22.0", "20.6", "24", "26"),
        ]
    )
    scenarios = []
    for rank, values in enumerate(scenario_values, start=1):
        role, low, high, stop, target_1, target_2 = values
        scenario_id = f"scenario-{symbol}-{rank}"
        scenarios.append(
            ZoneScenario(
                scenario_id=scenario_id,
                role=ScenarioRole(role),
                base_rank=rank,
                source_families=["trend", "price_structure"],
                anchor_refs=[f"anchor-{rank}-trend", f"anchor-{rank}-structure"],
                entry_low=Decimal(low),
                entry_high=Decimal(high),
                entry_reference=Decimal(high),
                stop_loss=Decimal(stop),
                target_1=Decimal(target_1),
                target_2=Decimal(target_2),
                target_1_exit_ratio=Decimal("0.5"),
                reference_net_rr_1=Decimal("1.5"),
                reference_net_risk=Decimal(high) - Decimal(stop),
                reference_net_reward_1=Decimal(target_1) - Decimal(high),
                reachability_band=ReachabilityBand.NEAR,
                price_relation=PriceRelation.ABOVE,
                confirmation_rules=[
                    RuleSpec(
                        rule_type="closed_bar_reversal_pattern",
                        params={},
                        rule_version="confirmation.v1",
                    )
                ],
                invalidation_rules=[
                    RuleSpec(
                        rule_type="close_at_or_below_stop",
                        params={"stop_loss": Decimal(stop)},
                        rule_version="invalidation.v1",
                    )
                ],
                valid_from=as_of,
                confirmation_deadline=date(2026, 8, 18),
                confirmation_valid_trading_days=5,
                order_valid_trading_days=2,
                scenario_fingerprint=fingerprint({"symbol": symbol, "rank": rank}),
            )
        )
    rules = MarketRuleSnapshot(
        snapshot_id=f"rules-{symbol}-20260811",
        venue="sse",
        board="main",
        trade_date=as_of,
        tick_size=Decimal("0.01"),
        price_limit_ratio=Decimal("0.10"),
        previous_close=Decimal("100"),
        limit_up=Decimal("110"),
        limit_down=Decimal("90"),
        t_plus_one=True,
    )
    artifact = PricePlanArtifact(
        plan_id=f"plan-{symbol}-20260811",
        revision=1,
        symbol=symbol,
        requested_as_of=as_of,
        actual_as_of_session=as_of,
        generation_status=GenerationStatus.GENERATED,
        scenarios=scenarios,
        observation_anchors=[],
        market_rules_snapshot_ref=rules.snapshot_id,
        data_quality="sufficient",
        reason_codes=[],
        algorithm_version="deterministic-price-plan.v1",
        params_version="price-plan-params.v1",
        data_fingerprint=fingerprint({"data": symbol}),
        calculation_fingerprint=fingerprint({"calculation": symbol}),
        plan_fingerprint=fingerprint({"plan": symbol}),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        trace_ref=trace_ref,
    )
    repository = PricePlanRepository()
    repository.save_market_rules(rules)
    artifact = repository.save_artifact(artifact)
    return TradePlanResult(
        price_plan=artifact,
        execution_status="review_pending",
        reason_codes=["REVIEW_PENDING"],
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/v1/agent/chat",
        "/api/v1/agent/chat/stream",
        "/api/v1/agent/trace/run",
        "/api/v1/agent/trace/stream",
    ],
)
def test_single_stock_chat_trade_plan_is_visible_in_signal_preview(endpoint: str) -> None:
    _reset_auth_globals()
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        db_path = data_dir / "signal-agent-api.db"
        env_path = data_dir / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prior_env_file = os.environ.get("ENV_FILE")
        prior_database_path = os.environ.get("DATABASE_PATH")
        os.environ["ENV_FILE"] = str(env_path)
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        try:
            client = TestClient(create_app(static_dir=data_dir / "empty-static"))
            received_context = []

            def run_chat(**kwargs):
                received_context.append(dict(kwargs.get("context") or {}))
                return SimpleNamespace(
                    success=True,
                    content="结论偏积极，等待价格进入计划区间。",
                    error=None,
                    dashboard=None,
                    stock_selection=None,
                    tool_calls_log=[],
                    total_steps=1,
                    total_tokens=10,
                    provider="test",
                    model="test-model",
                    debate=None,
                    trade_plan={
                        "symbol": "600519",
                        "action": "open",
                        "current_action": "wait",
                        "order_type": "manual",
                        "entry_zone_low": 1470.0,
                        "entry_zone_high": 1500.0,
                        "stop_loss_price": 1420.0,
                        "invalidation_conditions": ["日线收盘跌破 1420"],
                        "metadata": {"source": "agent_machine_readable_sidecar"},
                    },
                )

            executor = SimpleNamespace(chat=run_chat)

            request_payload = {
                "message": "分析 600519 是否适合买入",
                "session_id": "chat-signal-001",
                "context": {
                    "stock_code": "600519",
                    "stock_name": "贵州茅台",
                    "market": "cn",
                    "as_of": "2026-08-11T15:00:00",
                },
            }
            if "/trace/" in endpoint:
                request_payload.update({
                    "stock_code": "600519",
                    "stock_name": "贵州茅台",
                    "inject_portfolio_context": False,
                })

            with (
                patch("api.v1.endpoints.agent._build_executor", return_value=executor),
                patch("api.v1.endpoints.agent._prefetch_trace_composite_verdict", return_value=None),
                patch("api.v1.endpoints.agent._build_trace_composite_verdict_payload", return_value=None),
                patch(
                    "src.agent.pricing.runtime.generate_trade_plan",
                    side_effect=lambda symbol, **kwargs: _structured_price_plan(
                        symbol,
                        trace_ref=kwargs.get("trace_ref"),
                    ),
                ),
                patch("api.v1.endpoints.agent.score_symbol_composite", return_value=None),
            ):
                response = client.post(
                    endpoint,
                    json=request_payload,
                )

            assert response.status_code == 200
            assert received_context[0]["signal_lifecycle_entry_intent"] is True
            preview = client.get("/api/v1/signal-preview")
            assert preview.status_code == 200
            signals = preview.json()["entry_signals"]
            assert len(signals) == 1
            assert signals[0]["stock_code"] == "600519"
            assert signals[0]["entry_low"] == 1470.0
            assert signals[0]["entry_high"] == 1500.0
            assert signals[0]["stop_price"] == 1420.0
            assert signals[0]["plan_id"] == "plan-600519-20260811"
            assert signals[0]["metadata"]["current_action"] == "wait"
            assert signals[0]["metadata"]["trigger_action"] == "open"
            expected_session = "trace-chat-signal-001" if "/trace/" in endpoint else "chat-signal-001"
            assert signals[0]["source_trace_id"] == expected_session
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            if prior_env_file is None:
                os.environ.pop("ENV_FILE", None)
            else:
                os.environ["ENV_FILE"] = prior_env_file
            if prior_database_path is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = prior_database_path


def test_generic_single_stock_qa_with_entry_zone_is_visible_in_signal_preview() -> None:
    """A plain single-stock QA may create an observation plan when evidence has a zone."""
    _reset_auth_globals()
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        db_path = data_dir / "signal-agent-qa.db"
        env_path = data_dir / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prior_env_file = os.environ.get("ENV_FILE")
        prior_database_path = os.environ.get("DATABASE_PATH")
        os.environ["ENV_FILE"] = str(env_path)
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        try:
            client = TestClient(create_app(static_dir=data_dir / "empty-static"))
            received_context = []

            def run_chat(**kwargs):
                received_context.append(dict(kwargs.get("context") or {}))
                return SimpleNamespace(
                    success=True,
                    content="当前不追价，观察回踩区间。",
                    error=None,
                    dashboard=None,
                    stock_selection=None,
                    tool_calls_log=[],
                    total_steps=1,
                    total_tokens=10,
                    provider="test",
                    model="test-model",
                    debate=None,
                    trade_plan={
                        "symbol": "600330",
                        "action": "open",
                        "current_action": "reject",
                        "order_type": "manual",
                        "entry_zone_low": 19.6,
                        "entry_zone_high": 20.5,
                        "entry_zones": [
                            {
                                "label": "ideal",
                                "low": 19.6,
                                "high": 20.5,
                                "rationale": "回踩 MA20 及筹码平均成本区",
                            },
                            {
                                "label": "secondary",
                                "low": 21.2,
                                "high": 22.0,
                                "rationale": "回踩 MA5 且缩量企稳",
                            },
                        ],
                        "stop_loss_price": 18.5,
                        "invalidation_conditions": ["日线收盘跌破 18.5"],
                        "metadata": {"source": "agent_machine_readable_sidecar"},
                    },
                )

            executor = SimpleNamespace(chat=run_chat)
            request_payload = {
                "message": "分析一下天通股份",
                "session_id": "qa-signal-001",
                "report_intent": "qa",
                "analysis_mode": "planning_execute",
                "inject_portfolio_context": False,
            }
            with (
                patch("api.v1.endpoints.agent._build_executor", return_value=executor),
                patch("api.v1.endpoints.agent._prefetch_trace_composite_verdict", return_value=None),
                patch("api.v1.endpoints.agent._build_trace_composite_verdict_payload", return_value=None),
                patch(
                    "src.agent.pricing.runtime.generate_trade_plan",
                    side_effect=lambda symbol, **kwargs: _structured_price_plan(
                        symbol,
                        trace_ref=kwargs.get("trace_ref"),
                    ),
                ),
            ):
                response = client.post("/api/v1/agent/trace/run", json=request_payload)

            assert response.status_code == 200
            assert received_context[0]["signal_lifecycle_plan_candidate"] is True
            assert received_context[0].get("signal_lifecycle_entry_intent") is not True
            preview = client.get("/api/v1/signal-preview")
            assert preview.status_code == 200
            signals = preview.json()["entry_signals"]
            assert len(signals) == 1
            assert signals[0]["stock_code"] == "600330"
            assert signals[0]["entry_low"] == 19.6
            assert signals[0]["entry_high"] == 20.5
            zones = signals[0]["trigger_rule"]["zones"]
            assert [
                (zone["scenario_id"], zone["role"], zone["low"], zone["high"])
                for zone in zones
            ] == [
                ("scenario-600330-1", "primary", 19.6, 20.5),
                ("scenario-600330-2", "secondary", 21.2, 22.0),
            ]
            assert zones[0]["stop_loss"] == 18.5
            assert zones[0]["target_1"] == 23.0
            assert zones[1]["stop_loss"] == 20.6
            assert zones[1]["target_1"] == 24.0
            assert signals[0]["plan_id"] == "plan-600330-20260811"
            # The structured artifact is authoritative even though the mocked
            # legacy LLM sidecar contains the same-looking free-text prices.
            assert signals[0]["metadata"]["price_source"] == "deterministic_price_plan"
            assert signals[0]["metadata"]["plan_mode"] == "observation_only"
            assert signals[0]["metadata"]["execution_eligible"] is False
            assert signals[0]["status"] == "needs_review"

            no_plan_result = SimpleNamespace(
                success=True,
                content="这里只讨论基本面，没有形成入场区间。",
                error=None,
                dashboard=None,
                stock_selection=None,
                tool_calls_log=[],
                total_steps=1,
                total_tokens=10,
                provider="test",
                model="test-model",
                debate=None,
                trade_plan=None,
            )
            with (
                patch(
                    "api.v1.endpoints.agent._build_executor",
                    return_value=SimpleNamespace(chat=lambda **_: no_plan_result),
                ),
                patch("api.v1.endpoints.agent._prefetch_trace_composite_verdict", return_value=None),
                patch("api.v1.endpoints.agent._build_trace_composite_verdict_payload", return_value=None),
            ):
                no_plan_response = client.post(
                    "/api/v1/agent/trace/run",
                    json={**request_payload, "session_id": "qa-signal-no-zone"},
                )
            assert no_plan_response.status_code == 200
            assert len(client.get("/api/v1/signal-preview").json()["entry_signals"]) == 1
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            if prior_env_file is None:
                os.environ.pop("ENV_FILE", None)
            else:
                os.environ["ENV_FILE"] = prior_env_file
            if prior_database_path is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = prior_database_path
