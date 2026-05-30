# -*- coding: utf-8 -*-
"""Tests for the Agent models discovery service and endpoint."""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api.v1.endpoints import agent
from src.config import Config
from src.services.agent_model_service import list_agent_model_deployments


def _build_config(**overrides):
    config = Config(
        litellm_model="gemini/gemini-2.5-flash",
        litellm_fallback_models=["openai/gpt-4o-mini"],
        llm_model_list=[],
        llm_channels=[],
        litellm_config_path=None,
        llm_models_source="legacy_env",
        openai_base_url=None,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class AgentModelsApiTestCase(unittest.TestCase):
    def test_models_endpoint_returns_litellm_config_deployments(self) -> None:
        config = _build_config(
            litellm_config_path="config/litellm.yaml",
            llm_models_source="litellm_config",
            llm_model_list=[
                {
                    "model_name": "gemini-primary",
                    "litellm_params": {"model": "gemini/gemini-2.5-flash", "api_key": "secret-1"},
                },
                {
                    "model_name": "openai-fallback",
                    "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "secret-2"},
                },
            ],
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 2)
        self.assertEqual(deployments[0]["source"], "litellm_config")
        self.assertTrue(deployments[0]["is_primary"])
        self.assertFalse("api_key" in str(deployments))

    def test_models_endpoint_returns_channel_deployments_with_api_base(self) -> None:
        config = _build_config(
            llm_channels=[{"name": "openai"}],
            llm_models_source="llm_channels",
            llm_model_list=[
                {
                    "model_name": "openai/gpt-4o-mini",
                    "litellm_params": {
                        "model": "openai/gpt-4o-mini",
                        "api_key": "secret-1",
                        "api_base": "https://api.example.com/v1",
                    },
                }
            ],
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(deployments[0]["source"], "llm_channels")
        self.assertEqual(deployments[0]["api_base"], "https://api.example.com/v1")

    def test_models_endpoint_uses_agent_primary_override_for_primary_marker(self) -> None:
        config = _build_config(
            litellm_model="gemini/gemini-2.5-flash",
            litellm_fallback_models=["openai/gpt-4o-mini"],
            agent_litellm_model="openai/gpt-4o-mini",
            llm_channels=[{"name": "mixed"}],
            llm_models_source="llm_channels",
            llm_model_list=[
                {
                    "model_name": "gemini/gemini-2.5-flash",
                    "litellm_params": {"model": "gemini/gemini-2.5-flash", "api_key": "secret-g"},
                },
                {
                    "model_name": "openai/gpt-4o-mini",
                    "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "secret-o"},
                },
            ],
        )

        deployments = list_agent_model_deployments(config)
        by_model = {item["model"]: item for item in deployments}

        self.assertTrue(by_model["openai/gpt-4o-mini"]["is_primary"])
        self.assertFalse(by_model["openai/gpt-4o-mini"]["is_fallback"])
        self.assertFalse(by_model["gemini/gemini-2.5-flash"]["is_primary"])
        self.assertFalse(by_model["gemini/gemini-2.5-flash"]["is_fallback"])

    def test_models_endpoint_resolves_legacy_placeholders_to_real_models(self) -> None:
        config = _build_config(
            llm_model_list=[
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-1"}},
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-2"}},
                {"model_name": "__legacy_openai__", "litellm_params": {"model": "__legacy_openai__", "api_key": "o-1"}},
            ],
            openai_base_url="https://openai.example.com/v1",
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 3)
        self.assertEqual(deployments[0]["model"], "gemini/gemini-2.5-flash")
        self.assertEqual(deployments[1]["model"], "gemini/gemini-2.5-flash")
        self.assertEqual(deployments[2]["model"], "openai/gpt-4o-mini")
        self.assertEqual(deployments[2]["api_base"], "https://openai.example.com/v1")
        self.assertEqual(deployments[2]["source"], "legacy_env")
        self.assertTrue(all(not item["deployment_name"].startswith("__legacy_") for item in deployments))

    def test_models_endpoint_resolves_unprefixed_legacy_openai_model_names(self) -> None:
        config = _build_config(
            litellm_model="gpt-4o-mini",
            litellm_fallback_models=[],
            llm_model_list=[
                {"model_name": "__legacy_openai__", "litellm_params": {"model": "__legacy_openai__", "api_key": "o-1"}},
            ],
            openai_base_url="https://openai.example.com/v1",
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0]["model"], "gpt-4o-mini")
        self.assertEqual(deployments[0]["provider"], "openai")
        self.assertEqual(deployments[0]["source"], "legacy_env")
        self.assertEqual(deployments[0]["api_base"], "https://openai.example.com/v1")

    def test_models_endpoint_collapses_legacy_fallbacks_to_single_runtime_deployment(self) -> None:
        config = _build_config(
            llm_model_list=[
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-12345678"}},
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-87654321"}},
                {"model_name": "__legacy_openai__", "litellm_params": {"model": "__legacy_openai__", "api_key": "o-12345678"}},
                {"model_name": "__legacy_openai__", "litellm_params": {"model": "__legacy_openai__", "api_key": "o-87654321"}},
            ],
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 3)
        primary = [item for item in deployments if item["is_primary"]]
        fallback = [item for item in deployments if item["is_fallback"]]

        self.assertEqual(len(primary), 2)
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0]["model"], "openai/gpt-4o-mini")
        self.assertEqual(fallback[0]["deployment_id"], "legacy:openai:0:openai/gpt-4o-mini")
        self.assertEqual(fallback[0]["deployment_name"], "legacy_openai_1")

    def test_models_endpoint_keeps_direct_env_primary_provider_in_legacy_mode(self) -> None:
        config = _build_config(
            litellm_model="cohere/command-r-plus",
            litellm_fallback_models=[],
            llm_model_list=[],
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 1)
        self.assertEqual(deployments[0]["model"], "cohere/command-r-plus")
        self.assertEqual(deployments[0]["provider"], "cohere")
        self.assertEqual(deployments[0]["source"], "legacy_env")
        self.assertTrue(deployments[0]["is_primary"])
        self.assertFalse(deployments[0]["is_fallback"])

    def test_models_endpoint_keeps_direct_env_fallback_provider_in_legacy_mode(self) -> None:
        config = _build_config(
            litellm_fallback_models=["cohere/command-r-plus"],
            llm_model_list=[
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-12345678"}},
                {"model_name": "__legacy_gemini__", "litellm_params": {"model": "__legacy_gemini__", "api_key": "g-87654321"}},
            ],
        )

        deployments = list_agent_model_deployments(config)

        self.assertEqual(len(deployments), 3)
        fallback = [item for item in deployments if item["is_fallback"]]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0]["model"], "cohere/command-r-plus")
        self.assertEqual(fallback[0]["provider"], "cohere")
        self.assertEqual(fallback[0]["deployment_id"], "legacy:cohere:0:cohere/command-r-plus")
        self.assertEqual(fallback[0]["deployment_name"], "legacy_cohere_1")

    def test_models_endpoint_returns_empty_list_when_no_model_is_configured(self) -> None:
        config = _build_config(
            litellm_model="",
            litellm_fallback_models=[],
            llm_model_list=[],
        )

        self.assertEqual(list_agent_model_deployments(config), [])


class AgentModelsEndpointTestCase(unittest.TestCase):
    def test_endpoint_returns_sorted_models_without_secrets(self) -> None:
        config = _build_config(
            llm_channels=[{"name": "primary"}, {"name": "secondary"}],
            llm_model_list=[
                {
                    "model_name": "openai/gpt-4o-mini",
                    "litellm_params": {
                        "model": "openai/gpt-4o-mini",
                        "api_key": "secret-openai",
                        "api_base": "https://api.openai.example/v1",
                    },
                },
                {
                    "model_name": "gemini/gemini-2.5-flash",
                    "litellm_params": {
                        "model": "gemini/gemini-2.5-flash",
                        "api_key": "secret-gemini",
                    },
                },
            ],
        )

        with patch("api.v1.endpoints.agent.get_config", return_value=config):
            payload = asyncio.run(agent.get_agent_models()).model_dump()

        self.assertEqual(len(payload["models"]), 2)
        self.assertEqual(payload["models"][0]["model"], "gemini/gemini-2.5-flash")
        self.assertTrue(payload["models"][0]["is_primary"])
        self.assertEqual(payload["models"][1]["model"], "openai/gpt-4o-mini")
        self.assertTrue(payload["models"][1]["is_fallback"])
        self.assertNotIn("api_key", str(payload))


class AgentSkillsEndpointTestCase(unittest.TestCase):
    def test_skills_endpoint_returns_skill_metadata_shape(self) -> None:
        config = _build_config()
        skill_manager = SimpleNamespace(
            list_skills=lambda: [
                SimpleNamespace(
                    name="bull_trend",
                    display_name="多头趋势",
                    description="趋势跟随",
                    user_invocable=True,
                    default_priority=20,
                    default_active=True,
                ),
                SimpleNamespace(
                    name="chan_theory",
                    display_name="缠论",
                    description="结构分析",
                    user_invocable=True,
                    default_priority=40,
                    default_active=False,
                ),
            ]
        )

        with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
            "src.agent.factory.get_skill_manager",
            return_value=skill_manager,
        ):
            payload = asyncio.run(agent.get_skills()).model_dump()

        self.assertEqual(payload["default_skill_id"], "bull_trend")
        self.assertEqual([item["id"] for item in payload["skills"]], ["bull_trend", "chan_theory"])

    def test_legacy_strategies_endpoint_preserves_legacy_field_names(self) -> None:
        config = _build_config()
        skill_manager = SimpleNamespace(
            list_skills=lambda: [
                SimpleNamespace(
                    name="bull_trend",
                    display_name="多头趋势",
                    description="趋势跟随",
                    user_invocable=True,
                    default_priority=20,
                    default_active=True,
                ),
            ]
        )

        with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
            "src.agent.factory.get_skill_manager",
            return_value=skill_manager,
        ):
            payload = asyncio.run(agent.get_strategies()).model_dump()

        self.assertNotIn("skills", payload)
        self.assertEqual(payload["default_strategy_id"], "bull_trend")
        self.assertEqual(
            payload["strategies"],
            [
                {
                    "id": "bull_trend",
                    "name": "多头趋势",
                    "description": "趋势跟随",
                }
            ],
        )

    def test_chat_request_empty_skills_clears_context_without_triggering_activate_all(self) -> None:
        config = SimpleNamespace(is_agent_available=lambda: True)
        executor = MagicMock()
        executor.chat.return_value = SimpleNamespace(success=True, content="ok", error=None)
        request = agent.ChatRequest(message="hello", skills=[], context={"skills": ["old_skill"]})
        real_get_running_loop = asyncio.get_running_loop

        class _ImmediateLoop:
            def __init__(self, loop):
                self._loop = loop

            def run_in_executor(self, _executor, func):
                future = self._loop.create_future()
                future.set_result(func())
                return future

        with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
            "api.v1.endpoints.agent._build_executor",
            return_value=executor,
        ) as mock_build_executor, patch(
            "api.v1.endpoints.agent.asyncio.get_running_loop",
            side_effect=lambda: _ImmediateLoop(real_get_running_loop()),
        ):
            payload = asyncio.run(agent.agent_chat(request)).model_dump()

        mock_build_executor.assert_called_once_with(config, None)
        executor.chat.assert_called_once()
        self.assertEqual(executor.chat.call_args.kwargs["context"]["skills"], [])
        self.assertEqual(payload["content"], "ok")

    def test_trace_run_returns_tool_log_and_progress_events(self) -> None:
        config = SimpleNamespace(
            is_agent_available=lambda: True,
            report_language="zh",
            agent_mode=True,
            agent_analysis_mode="planning_execute",
            agent_orchestration_mode="expert_graph",
            agent_arch="single",
            agent_max_steps=20,
            agent_orchestrator_timeout_s=600,
            agent_tool_call_timeout_seconds=30,
        )
        executor = MagicMock()
        executor.chat.side_effect = lambda **kwargs: (
            kwargs["progress_callback"]({"type": "tool_start", "step": 1, "tool": "get_realtime_quote"})
            or SimpleNamespace(
                success=True,
                content="trace ok",
                error=None,
                total_steps=2,
                total_tokens=123,
                provider="deepseek",
                model="deepseek/deepseek-v4-pro",
                debate={"enabled": True, "success": True, "judge_decision": {"final_action": "hold"}},
                tool_calls_log=[
                    {
                        "step": 1,
                        "tool": "get_realtime_quote",
                        "arguments": {"stock_code": "600519"},
                        "success": True,
                        "duration": 0.1,
                        "result_length": 20,
                        "result_preview": '{"price": 100}',
                    }
                ],
            )
        )
        request = agent.AgentTraceRunRequest(
            message="分析 600519",
            inject_portfolio_context=False,
        )
        real_get_running_loop = asyncio.get_running_loop

        class _ImmediateLoop:
            def __init__(self, loop):
                self._loop = loop

            def run_in_executor(self, _executor, func):
                future = self._loop.create_future()
                future.set_result(func())
                return future

        mock_db = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            config.database_path = os.path.join(tmpdir, "stock_analysis.db")
            with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
                "api.v1.endpoints.agent._build_executor",
                return_value=executor,
            ), patch(
                "src.agent.conversation.conversation_manager.clear",
            ) as mock_clear, patch(
                "src.storage.get_db",
                return_value=mock_db,
            ), patch(
                "api.v1.endpoints.agent.asyncio.get_running_loop",
                side_effect=lambda: _ImmediateLoop(real_get_running_loop()),
            ):
                payload = asyncio.run(agent.run_agent_trace(request)).model_dump()

            mock_clear.assert_called_once_with(payload["session_id"])
            mock_db.delete_conversation_session.assert_called_once_with(payload["session_id"])
            self.assertTrue(payload["session_id"].startswith("trace-"))
            self.assertTrue(payload["success"])
            self.assertEqual(payload["content"], "trace ok")
            self.assertEqual(payload["tool_calls"][0]["tool"], "get_realtime_quote")
            self.assertEqual(payload["events"][0]["display_name"], "获取实时行情")
            self.assertEqual(payload["planner"]["intent"], "entry_analysis")
            self.assertEqual(payload["planner"]["primary_symbol"], "600519")
            self.assertTrue(payload["artifact_dir"])
            self.assertTrue(os.path.isdir(payload["artifact_dir"]))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "context.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "planner.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "events.ndjson")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "tool_calls.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "evidence_ledger.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "debate.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "risk_gate.json")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "final.md")))
            self.assertTrue(os.path.exists(os.path.join(payload["artifact_dir"], "todo.md")))
            self.assertEqual(payload["debate"]["judge_decision"]["final_action"], "hold")
            self.assertEqual(payload["risk_gate"]["trade_plan"]["action"], "hold")
            self.assertEqual(payload["risk_gate"]["risk_gate"]["status"], "passed")
            self.assertEqual(payload["runtime_config"]["agent_orchestration_mode"], "expert_graph")
            with open(os.path.join(payload["artifact_dir"], "events.ndjson"), encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(lines[0]["type"], "tool_start")
            with open(os.path.join(payload["artifact_dir"], "final.md"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "trace ok")
            with open(os.path.join(payload["artifact_dir"], "evidence_ledger.json"), encoding="utf-8") as fh:
                evidence_ledger = json.load(fh)
            self.assertEqual(evidence_ledger["entry_count"], 1)
            self.assertEqual(evidence_ledger["entries"][0]["tool"], "get_realtime_quote")
            self.assertEqual(evidence_ledger["entries"][0]["status"], "success")
            self.assertIn("price", evidence_ledger["entries"][0]["evidence"])
            with open(os.path.join(payload["artifact_dir"], "debate.json"), encoding="utf-8") as fh:
                debate = json.load(fh)
            self.assertEqual(debate["judge_decision"]["final_action"], "hold")
            with open(os.path.join(payload["artifact_dir"], "risk_gate.json"), encoding="utf-8") as fh:
                risk_gate = json.load(fh)
            self.assertEqual(risk_gate["source"], "debate_judge")
            self.assertEqual(risk_gate["risk_gate"]["allowed_action"], "hold")
            with open(os.path.join(payload["artifact_dir"], "todo.md"), encoding="utf-8") as fh:
                todo_md = fh.read()
            self.assertIn("## 执行状态", todo_md)
            self.assertIn("get_realtime_quote: success", todo_md)
            self.assertIn('arguments={"stock_code": "600519"}', todo_md)
            self.assertIn('result_preview={"price": 100}', todo_md)
            self.assertIn("## Execute Protocol 复核", todo_md)

    def test_runtime_config_endpoint_returns_orchestration_mode(self) -> None:
        config = SimpleNamespace(
            agent_mode=True,
            agent_analysis_mode="planning_execute",
            agent_orchestration_mode="expert_graph",
            agent_arch="react",
            agent_max_steps=20,
            agent_orchestrator_timeout_s=600,
            agent_tool_call_timeout_seconds=30,
        )
        with patch("api.v1.endpoints.agent.get_config", return_value=config):
            payload = asyncio.run(agent.get_agent_runtime_config()).model_dump()

        self.assertEqual(payload["runtime_config"]["agent_orchestration_mode"], "expert_graph")
        self.assertEqual(payload["runtime_config"]["agent_tool_call_timeout_seconds"], 30)

    def test_trace_tool_status_marks_preview_errors_failed(self) -> None:
        call = {
            "step": 1,
            "tool": "get_capital_flow",
            "arguments": {"stock_code": "600519"},
            "success": True,
            "duration": 30.0,
            "result_preview": json.dumps({
                "status": "ok",
                "main_net_inflow": 123.4,
                "errors": ["capital flow fetch failed"],
            }, ensure_ascii=False),
        }

        normalized = agent._normalize_tool_calls_status([call])
        ledger = agent._build_evidence_ledger(normalized)
        event = agent._normalize_tool_event_status({"type": "tool_done", **call})

        self.assertFalse(normalized[0]["success"])
        self.assertEqual(ledger["entries"][0]["status"], "failed")
        self.assertFalse(event["success"])

    def test_sanitize_json_payload_coerces_non_finite_numbers(self) -> None:
        payload = {
            "ok": 1.23,
            "nan": float("nan"),
            "nested": {
                "pos_inf": float("inf"),
                "neg_inf": float("-inf"),
                "items": [1, float("nan"), {"value": float("inf")}],
            },
        }

        sanitized = agent._sanitize_json_payload(payload)

        self.assertEqual(sanitized["ok"], 1.23)
        self.assertIsNone(sanitized["nan"])
        self.assertIsNone(sanitized["nested"]["pos_inf"])
        self.assertIsNone(sanitized["nested"]["neg_inf"])
        self.assertEqual(sanitized["nested"]["items"][0], 1)
        self.assertIsNone(sanitized["nested"]["items"][1])
        self.assertIsNone(sanitized["nested"]["items"][2]["value"])

    def test_trace_stream_emits_context_planner_tool_and_done_events(self) -> None:
        config = SimpleNamespace(
            is_agent_available=lambda: True,
            report_language="zh",
            agent_mode=True,
            agent_analysis_mode="planning_execute",
            agent_orchestration_mode="expert_graph",
            agent_arch="single",
            agent_max_steps=20,
            agent_orchestrator_timeout_s=600,
            agent_tool_call_timeout_seconds=30,
        )
        executor = MagicMock()
        executor.chat.side_effect = lambda **kwargs: (
            kwargs["progress_callback"]({
                "type": "tool_done",
                "step": 1,
                "tool": "get_realtime_quote",
                "arguments": {"stock_code": "600519"},
                "success": True,
                "duration": 0.1,
                "result_length": 16,
                "result_preview": '{"price":100}',
            })
            or SimpleNamespace(
                success=True,
                content="stream ok",
                error=None,
                total_steps=1,
                total_tokens=42,
                provider="deepseek",
                model="deepseek/deepseek-v4-pro",
                debate={"enabled": True, "success": True, "judge_decision": {"final_action": "hold"}},
                tool_calls_log=[
                    {
                        "step": 1,
                        "tool": "get_realtime_quote",
                        "arguments": {"stock_code": "600519"},
                        "success": True,
                        "duration": 0.1,
                        "result_length": 16,
                        "result_preview": '{"price":100}',
                    }
                ],
            )
        )
        request = agent.AgentTraceRunRequest(
            message="我持有 600519，适合继续拿长线吗？",
            inject_portfolio_context=False,
        )

        async def collect_events() -> list:
            config.database_path = os.path.join(tempfile.mkdtemp(), "stock_analysis.db")
            with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
                "api.v1.endpoints.agent._build_executor",
                return_value=executor,
            ), patch(
                "src.agent.conversation.conversation_manager.clear",
            ), patch(
                "src.storage.get_db",
                return_value=MagicMock(),
            ):
                response = await agent.stream_agent_trace(request)
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
                parsed = []
                for line in "".join(chunks).splitlines():
                    if line.startswith("data: "):
                        parsed.append(json.loads(line[6:]))
                return parsed

        events = asyncio.run(collect_events())
        event_types = [event["type"] for event in events]

        self.assertIn("context_ready", event_types)
        self.assertIn("planner_ready", event_types)
        self.assertIn("tool_done", event_types)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "stream ok")
        self.assertEqual(events[-1]["debate"]["judge_decision"]["final_action"], "hold")
        self.assertEqual(events[-1]["risk_gate"]["risk_gate"]["status"], "passed")
        self.assertEqual(events[-1]["runtime_config"]["agent_orchestration_mode"], "expert_graph")
        self.assertTrue(events[-1]["artifact_dir"])
        tool_event = next(event for event in events if event["type"] == "tool_done")
        self.assertEqual(tool_event["display_name"], "获取实时行情")
        self.assertEqual(tool_event["result_preview"], '{"price":100}')

    def test_trace_risk_gate_blocks_t_plus_one_sell(self) -> None:
        config = SimpleNamespace(is_agent_available=lambda: True, report_language="zh")
        executor = MagicMock()
        executor.chat.return_value = SimpleNamespace(
            success=True,
            content="trace ok",
            error=None,
            total_steps=1,
            total_tokens=42,
            provider="deepseek",
            model="deepseek/deepseek-v4-pro",
            debate={
                "enabled": True,
                "success": True,
                "judge_decision": {
                    "final_action": "sell",
                    "risk_controls": ["跌破成本区复查"],
                    "confidence": 0.9,
                },
            },
            stock_selection=None,
            tool_calls_log=[
                {
                    "step": 1,
                    "tool": "get_realtime_quote",
                    "arguments": {"stock_code": "600519"},
                    "success": True,
                    "duration": 0.1,
                    "result_length": 32,
                    "result_preview": '{"price":100,"is_limit_down":false}',
                }
            ],
        )
        request = agent.AgentTraceRunRequest(
            message="我今天买了 600519，现在能卖吗？",
            stock_code="600519",
            inject_portfolio_context=False,
            context={
                "agent_user_context": {
                    "positions": [
                        {
                            "symbol": "600519",
                            "quantity": 100,
                            "avg_cost": 99,
                            "last_price": 100,
                            "holding_days": 0,
                        }
                    ],
                    "report": {
                        "analysis_mode": "planning_execute",
                        "intent": "position_review",
                        "primary_symbol": "600519",
                        "target_symbols": ["600519"],
                    },
                }
            },
        )
        real_get_running_loop = asyncio.get_running_loop

        class _ImmediateLoop:
            def __init__(self, loop):
                self._loop = loop

            def run_in_executor(self, _executor, func):
                future = self._loop.create_future()
                future.set_result(func())
                return future

        mock_db = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            config.database_path = os.path.join(tmpdir, "stock_analysis.db")
            with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
                "api.v1.endpoints.agent._build_executor",
                return_value=executor,
            ), patch(
                "src.agent.conversation.conversation_manager.clear",
            ), patch(
                "src.storage.get_db",
                return_value=mock_db,
            ), patch(
                "api.v1.endpoints.agent.asyncio.get_running_loop",
                side_effect=lambda: _ImmediateLoop(real_get_running_loop()),
            ):
                payload = asyncio.run(agent.run_agent_trace(request)).model_dump()

            self.assertEqual(payload["risk_gate"]["trade_plan"]["action"], "sell")
            self.assertEqual(payload["risk_gate"]["risk_gate"]["status"], "blocked")
            self.assertEqual(payload["risk_gate"]["risk_gate"]["allowed_action"], "manual_review")
            rule_ids = {
                check["rule_id"]
                for check in payload["risk_gate"]["risk_gate"]["checks"]
                if check["passed"] is False
            }
            self.assertIn("a_share_t_plus_one", rule_ids)
            with open(os.path.join(payload["artifact_dir"], "risk_gate.json"), encoding="utf-8") as fh:
                risk_gate = json.load(fh)
            self.assertEqual(risk_gate["risk_gate"]["status"], "blocked")

    def test_trace_watchlist_scan_without_single_symbol_skips_single_stock_risk_gate(self) -> None:
        config = SimpleNamespace(is_agent_available=lambda: True, report_language="zh")
        executor = MagicMock()
        executor.chat.return_value = SimpleNamespace(
            success=True,
            content="trace ok",
            error=None,
            total_steps=2,
            total_tokens=88,
            provider="deepseek",
            model="deepseek/deepseek-v4-pro",
            debate=None,
            stock_selection={
                "success": True,
                "selection_context": {
                    "orchestration_mode": "expert_graph",
                    "stages": {},
                },
                "final_report_json": {
                    "portfolio_allocation": {
                        "summary": {"portfolio_action": "wait"},
                        "full": {
                            "positions_plan": [
                                {"code": "688707", "name": "振华新材", "action": "wait"},
                                {"code": "688266", "name": "泽璟制药-U", "action": "monitor"},
                            ],
                        },
                    },
                    "judge_decision": {
                        "summary": {
                            "primary_plan_verdict": "wait_for_more_data",
                            "final_action": "wait",
                        }
                    },
                },
            },
            tool_calls_log=[
                {
                    "step": 1,
                    "tool": "get_realtime_quote",
                    "arguments": {"stock_code": "688707"},
                    "success": True,
                    "duration": 0.1,
                    "result_length": 32,
                    "result_preview": '{"code":"688707","price":16.47}',
                    "result_json": {"code": "688707", "price": 16.47},
                },
                {
                    "step": 2,
                    "tool": "get_realtime_quote",
                    "arguments": {"stock_code": "688266"},
                    "success": True,
                    "duration": 0.1,
                    "result_length": 33,
                    "result_preview": '{"code":"688266","price":102.07}',
                    "result_json": {"code": "688266", "price": 102.07},
                },
            ],
        )
        request = agent.AgentTraceRunRequest(
            message="帮我选下周可关注股票",
            inject_portfolio_context=False,
            context={
                "agent_user_context": {
                    "positions": [
                        {
                            "symbol": "300476",
                            "quantity": 100,
                            "avg_cost": 333.4998,
                            "last_price": 375.5,
                            "position_pct": 64.464051,
                        }
                    ],
                    "report": {
                        "analysis_mode": "planning_execute",
                        "intent": "watchlist_scan",
                        "primary_symbol": None,
                        "target_symbols": [],
                    },
                }
            },
        )
        real_get_running_loop = asyncio.get_running_loop

        class _ImmediateLoop:
            def __init__(self, loop):
                self._loop = loop

            def run_in_executor(self, _executor, func):
                future = self._loop.create_future()
                future.set_result(func())
                return future

        with tempfile.TemporaryDirectory() as tmpdir:
            config.database_path = os.path.join(tmpdir, "stock_analysis.db")
            with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
                "api.v1.endpoints.agent._build_executor",
                return_value=executor,
            ), patch(
                "src.agent.conversation.conversation_manager.clear",
            ), patch(
                "src.storage.get_db",
                return_value=MagicMock(),
            ), patch(
                "api.v1.endpoints.agent.asyncio.get_running_loop",
                side_effect=lambda: _ImmediateLoop(real_get_running_loop()),
            ):
                payload = asyncio.run(agent.run_agent_trace(request)).model_dump()

            self.assertIsNone(payload["risk_gate"])
            with open(os.path.join(payload["artifact_dir"], "risk_gate.json"), encoding="utf-8") as fh:
                self.assertIsNone(json.load(fh))

    def test_trace_finalize_ingests_graphiti_episode(self) -> None:
        config = SimpleNamespace(
            is_agent_available=lambda: True,
            report_language="zh",
            graphiti_enabled=True,
        )
        executor = MagicMock()
        executor.chat.return_value = SimpleNamespace(
            success=True,
            content="trace ok",
            error=None,
            total_steps=1,
            total_tokens=10,
            provider="deepseek",
            model="deepseek/deepseek-v4-pro",
            debate=None,
            stock_selection=None,
            tool_calls_log=[],
        )
        request = agent.AgentTraceRunRequest(
            message="分析 600519",
            stock_code="600519",
            analysis_mode="planning_execute",
            inject_portfolio_context=False,
        )

        mock_service = MagicMock()
        mock_service.is_available.return_value = True

        async def collect() -> dict:
            config.database_path = os.path.join(tempfile.mkdtemp(), "stock_analysis.db")
            with patch("api.v1.endpoints.agent.get_config", return_value=config), patch(
                "api.v1.endpoints.agent._build_executor",
                return_value=executor,
            ), patch(
                "src.agent.conversation.conversation_manager.clear",
            ), patch(
                "src.storage.get_db",
                return_value=MagicMock(),
            ), patch(
                "api.v1.endpoints.agent.get_graphiti_service",
                return_value=mock_service,
            ):
                return (await agent.run_agent_trace(request)).model_dump()

        payload = asyncio.run(collect())

        self.assertTrue(payload["artifact_dir"])
        mock_service.ingest_trace_sync.assert_called_once()
        kwargs = mock_service.ingest_trace_sync.call_args.kwargs
        self.assertEqual(kwargs["session_id"], payload["session_id"])
        self.assertEqual(kwargs["trace_type"], "single_stock_analysis")
        self.assertEqual(kwargs["artifact_dir"], payload["artifact_dir"])

    def test_trace_run_context_summary_exposes_account_position_and_profile(self) -> None:
        mock_portfolio_service = MagicMock()
        mock_portfolio_service.get_portfolio_snapshot.return_value = {
            "as_of": "2026-05-03",
            "currency": "CNY",
            "cost_method": "fifo",
            "accounts": [
                {
                    "account_id": 7,
                    "account_name": "A股主账户",
                    "broker": None,
                    "market": "cn",
                    "base_currency": "CNY",
                    "total_cash": 16982.65,
                    "total_market_value": 719550.0,
                    "total_equity": 736532.65,
                    "cost_method": "fifo",
                    "positions": [
                        {
                            "symbol": "600519",
                            "market": "cn",
                            "quantity": 150000,
                            "avg_cost": 4.797,
                            "total_cost": 719550.0,
                            "last_price": 5.0,
                            "market_value_base": 750000.0,
                            "unrealized_pnl_base": 30450.0,
                            "unrealized_pnl_pct": 4.231,
                        }
                    ],
                }
            ],
        }

        with patch("src.services.portfolio_service.PortfolioService", return_value=mock_portfolio_service):
            context = agent._build_trace_context(
                request=agent.AgentTraceRunRequest(
                    message="我持有 600519，适合继续拿长线吗？",
                    account_id=7,
                    stock_code="600519",
                    report_intent="position_review",
                    risk_preference="conservative",
                    trading_horizon="long_term",
                    max_single_position_pct=25,
                    max_total_equity_exposure_pct=75,
                    max_acceptable_drawdown_pct=12,
                    default_stop_loss_pct=8,
                    investor_notes="不能承受大回撤",
                ),
                config=SimpleNamespace(report_language="zh"),
            )
        summary = agent._build_trace_context_summary(context)

        mock_portfolio_service.get_portfolio_snapshot.assert_called_once()
        self.assertEqual(mock_portfolio_service.get_portfolio_snapshot.call_args.kwargs["account_id"], 7)
        self.assertEqual(summary["accounts"][0]["account_id"], 7)
        self.assertEqual(summary["accounts"][0]["account_name"], "A股主账户")
        self.assertEqual(summary["investor"]["risk_preference"], "conservative")
        self.assertEqual(summary["investor"]["trading_horizon"], "long_term")
        self.assertEqual(summary["investor"]["max_single_position_pct"], 25)
        self.assertEqual(summary["investor"]["max_total_equity_exposure_pct"], 75)
        self.assertEqual(summary["investor"]["max_acceptable_drawdown_pct"], 12)
        self.assertEqual(summary["investor"]["default_stop_loss_pct"], 8)
        self.assertEqual(context["agent_user_context"].report.intent, "position_review")
        self.assertEqual(summary["target_position"]["symbol"], "600519")
        self.assertEqual(summary["target_position"]["quantity"], 150000.0)

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_trace_context_builds_minimal_planner_context_for_stock_selection_without_portfolio(self, mock_completion):
        mock_completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"watchlist_scan"}'))]
        )
        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="我现在有5w，我希望你帮我选股，并告诉我怎么分配仓位",
                inject_portfolio_context=False,
                report_intent="entry_analysis",
                risk_preference="balanced",
                trading_horizon="long_term",
                max_single_position_pct=20,
                max_total_equity_exposure_pct=80,
                max_acceptable_drawdown_pct=15,
                default_stop_loss_pct=8,
                investor_notes="偏长期持有",
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertNotIn("stock_code", context)
        self.assertIsNotNone(context["agent_user_context"])
        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertEqual(context["agent_user_context"].report.primary_symbol, None)
        self.assertEqual(context["agent_user_context"].investor.max_single_position_pct, 20)
        planner = agent._build_planner_trace(context)
        self.assertIsNotNone(planner)
        self.assertEqual(planner["intent"], "watchlist_scan")
        self.assertIsNone(planner["primary_symbol"])

    @patch("src.services.portfolio_service.PortfolioService")
    def test_trace_context_injects_selected_account_even_when_checkbox_payload_is_false(self, mock_service_cls) -> None:
        mock_portfolio_service = MagicMock()
        mock_portfolio_service.get_portfolio_snapshot.return_value = {
            "as_of": "2026-05-15",
            "currency": "CNY",
            "cost_method": "fifo",
            "accounts": [
                {
                    "account_id": 3,
                    "account_name": "5w账户",
                    "market": "cn",
                    "base_currency": "CNY",
                    "total_cash": 34354.9,
                    "total_market_value": 17600,
                    "total_equity": 51954.9,
                    "positions": [
                        {
                            "symbol": "301028",
                            "quantity": 1000,
                            "avg_cost": 16.87,
                            "last_price": 17.6,
                            "market_value_base": 17600,
                            "unrealized_pnl_base": 730,
                            "unrealized_pnl_pct": 4.33,
                        }
                    ],
                }
            ],
        }
        mock_service_cls.return_value = mock_portfolio_service

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="根据我目前的持仓，给我下周一的操作建议",
                account_id=3,
                inject_portfolio_context=False,
                risk_preference="balanced",
                trading_horizon="long_term",
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        mock_portfolio_service.get_portfolio_snapshot.assert_called_once()
        self.assertEqual(mock_portfolio_service.get_portfolio_snapshot.call_args.kwargs["account_id"], 3)
        summary = agent._build_trace_context_summary(context)
        self.assertEqual(summary["account_count"], 1)
        self.assertEqual(summary["accounts"][0]["account_id"], 3)
        self.assertEqual(summary["position_count"], 1)
        self.assertEqual(context["agent_user_context"].report.intent, "position_review")

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_trace_context_forces_watchlist_scan_when_portfolio_context_is_injected(self, mock_completion) -> None:
        mock_completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"watchlist_scan"}'))]
        )
        mock_portfolio_service = MagicMock()
        mock_portfolio_service.get_portfolio_snapshot.return_value = {
            "as_of": "2026-05-15",
            "currency": "CNY",
            "cost_method": "fifo",
            "accounts": [
                {
                    "account_id": 7,
                    "account_name": "A股主账户",
                    "market": "cn",
                    "base_currency": "CNY",
                    "total_cash": 50000,
                    "total_market_value": 0,
                    "total_equity": 50000,
                    "positions": [],
                }
            ],
        }

        with patch("src.services.portfolio_service.PortfolioService", return_value=mock_portfolio_service):
            context = agent._build_trace_context(
                request=agent.AgentTraceRunRequest(
                    message="我现在有5w，我希望你帮我选股，并告诉我怎么分配仓位",
                    account_id=7,
                    inject_portfolio_context=True,
                    report_intent=None,
                    risk_preference="balanced",
                    trading_horizon="swing",
                ),
                config=SimpleNamespace(report_language="zh"),
            )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertTrue(context["agent_user_context"].report.include_watchlist_ranking)
        planner = agent._build_planner_trace(context)
        self.assertIsNotNone(planner)
        self.assertEqual(planner["intent"], "watchlist_scan")

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "", "XIAOMI_MIMO_KEY": "", "XIAOMI_MIMO_API_KEY": ""}, clear=False)
    def test_trace_context_does_not_use_keyword_matching_when_mimo_is_unconfigured(self) -> None:
        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="帮我选一下下周可以入手的股票",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertTrue(context["agent_user_context"].report.include_watchlist_ranking)

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "", "XIAOMI_MIMO_KEY": "", "XIAOMI_MIMO_API_KEY": ""}, clear=False)
    def test_trace_context_does_not_default_to_watchlist_without_explicit_selection_request(self) -> None:
        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="解释一下什么是均线多头排列",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "qa")
        self.assertFalse(context["agent_user_context"].report.include_watchlist_ranking)
        self.assertEqual(context["_trace_intent_resolution"]["source"], "default")
        self.assertFalse(context["_trace_intent_resolution"]["explicit_watchlist_request"])

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_trace_context_guards_mimo_watchlist_when_user_did_not_ask_for_selection(self, mock_completion) -> None:
        mock_completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"watchlist_scan"}'))]
        )

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="解释一下什么是均线多头排列",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "qa")
        self.assertFalse(context["agent_user_context"].report.include_watchlist_ranking)
        self.assertEqual(context["_trace_intent_resolution"]["source"], "mimo_guard")
        self.assertEqual(context["_trace_intent_resolution"]["classifier_intent"], "watchlist_scan")
        self.assertFalse(context["_trace_intent_resolution"]["explicit_watchlist_request"])

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_trace_context_uses_mimo_intent_classifier_for_natural_stock_selection_request(self, mock_completion) -> None:
        mock_completion.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"intent":"watchlist_scan","confidence":0.96,"reason":"用户要求从市场选择下周可入手股票"}'
                    )
                )
            ]
        )

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="帮我选一下下周可以入手的股票",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertTrue(context["agent_user_context"].report.include_watchlist_ranking)
        self.assertNotIn("stock_code", context)
        summary = agent._build_trace_context_summary(context)
        self.assertEqual(summary["intent_resolution"]["source"], "mimo")
        self.assertTrue(summary["intent_resolution"]["classifier_success"])
        self.assertEqual(summary["intent_resolution"]["classifier_model"], "mimo-v2.5")
        mock_completion.assert_called()
        call_kwargs = mock_completion.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "openai/mimo-v2.5")
        self.assertEqual(call_kwargs["api_base"], "https://mimo.example/v1")
        self.assertEqual(call_kwargs["max_tokens"], 600)

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_MODEL": "mimo-v2.5-pro", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_mimo_intent_classifier_model_is_configurable(self, mock_completion) -> None:
        mock_completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"watchlist_scan"}'))]
        )

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="帮我选一下下周可以入手的股票",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertEqual(mock_completion.call_args.kwargs["model"], "openai/mimo-v2.5-pro")
        summary = agent._build_trace_context_summary(context)
        self.assertEqual(summary["intent_resolution"]["classifier_model"], "mimo-v2.5-pro")

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_mimo_intent_classifier_failure_is_visible_in_context_summary(self, mock_completion) -> None:
        mock_completion.side_effect = RuntimeError("Not supported model")

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="帮我选一下下周可以入手的股票",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        summary = agent._build_trace_context_summary(context)
        self.assertEqual(summary["intent_resolution"]["source"], "default")
        self.assertEqual(summary["intent_resolution"]["intent"], "watchlist_scan")
        self.assertFalse(summary["intent_resolution"]["classifier_success"])
        self.assertIn("Not supported model", summary["intent_resolution"]["classifier_error"])

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "sk-test"}, clear=False)
    @patch("litellm.completion")
    def test_trace_context_does_not_override_position_review_on_classifier_failure(self, mock_completion) -> None:
        mock_completion.side_effect = RuntimeError("classifier down")
        mock_portfolio_service = MagicMock()
        mock_portfolio_service.get_portfolio_snapshot.return_value = {
            "as_of": "2026-05-15",
            "currency": "CNY",
            "cost_method": "fifo",
            "accounts": [
                {
                    "account_id": 7,
                    "account_name": "A股主账户",
                    "market": "cn",
                    "base_currency": "CNY",
                    "total_cash": 20000,
                    "total_market_value": 30000,
                    "total_equity": 50000,
                    "positions": [
                        {
                            "symbol": "301028",
                            "name": "鼎熔岩",
                            "quantity": 1000,
                            "avg_cost": 16.8,
                            "market_value": 30000,
                            "position_pct": 60,
                        }
                    ],
                }
            ],
        }

        with patch("src.services.portfolio_service.PortfolioService", return_value=mock_portfolio_service):
            context = agent._build_trace_context(
                request=agent.AgentTraceRunRequest(
                    message="我持有的301028要不要继续拿",
                    account_id=7,
                    inject_portfolio_context=True,
                    report_intent=None,
                ),
                config=SimpleNamespace(report_language="zh"),
            )

        self.assertEqual(context["agent_user_context"].report.intent, "position_review")
        self.assertFalse(context["agent_user_context"].report.include_watchlist_ranking)
        summary = agent._build_trace_context_summary(context)
        self.assertEqual(summary["intent_resolution"]["source"], "default")
        planner = agent._build_planner_trace(context)
        self.assertIsNotNone(planner)
        self.assertEqual(planner["intent"], "position_review")

    @patch.dict(os.environ, {"XIAOMI_MIMO_URL": "https://mimo.example/v1", "XIAOMI_MIMO_KEY": "", "XIAOMI_MIMO_API_KEY": "sk-api-key"}, clear=False)
    @patch("litellm.completion")
    def test_mimo_intent_classifier_accepts_legacy_api_key_env_name(self, mock_completion) -> None:
        mock_completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"intent":"watchlist_scan"}'))]
        )

        context = agent._build_trace_context(
            request=agent.AgentTraceRunRequest(
                message="帮我选一下下周可以入手的股票",
                inject_portfolio_context=False,
                report_intent=None,
            ),
            config=SimpleNamespace(report_language="zh"),
        )

        self.assertEqual(context["agent_user_context"].report.intent, "watchlist_scan")
        self.assertEqual(mock_completion.call_args.kwargs["api_key"], "sk-api-key")
class AgentModelsSourceDetectionTestCase(unittest.TestCase):
    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_marks_channels_as_actual_source_after_yaml_fallback(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "LITELLM_CONFIG": "config/missing.yaml",
            "LLM_CHANNELS": "primary",
            "LLM_PRIMARY_API_KEY": "channel-secret-key",
            "LLM_PRIMARY_MODELS": "openai/gpt-4o-mini",
            "OPENAI_API_KEY": "",
            "AIHUBMIX_KEY": "",
            "GEMINI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(config.llm_models_source, "llm_channels")
        self.assertEqual(config.llm_model_list[0]["litellm_params"]["model"], "openai/gpt-4o-mini")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_marks_legacy_as_actual_source_after_yaml_fallback(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "LITELLM_CONFIG": "config/missing.yaml",
            "LLM_CHANNELS": "",
            "OPENAI_API_KEY": "legacy-openai-key",
            "LITELLM_MODEL": "gpt-4o-mini",
            "AIHUBMIX_KEY": "",
            "GEMINI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(config.llm_models_source, "legacy_env")
        self.assertTrue(config.llm_model_list)
        self.assertEqual(config.llm_model_list[0]["model_name"], "__legacy_openai__")


class AgentTraceRunRequestCandidateDiscoveryModeTestCase(unittest.TestCase):
    def test_default_candidate_discovery_mode_is_none(self) -> None:
        request = agent.AgentTraceRunRequest(message="hi")
        self.assertIsNone(request.candidate_discovery_mode)

    def test_accepts_deterministic_value(self) -> None:
        request = agent.AgentTraceRunRequest(
            message="hi",
            candidate_discovery_mode="deterministic",
        )
        self.assertEqual(request.candidate_discovery_mode, "deterministic")

    def test_accepts_llm_expert_committee_value(self) -> None:
        request = agent.AgentTraceRunRequest(
            message="hi",
            candidate_discovery_mode="llm_expert_committee",
        )
        self.assertEqual(request.candidate_discovery_mode, "llm_expert_committee")

    def test_rejects_invalid_candidate_discovery_mode(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            agent.AgentTraceRunRequest(
                message="hi",
                candidate_discovery_mode="nonsense_value",
            )


class TraceArtifactWriterSeedPoolTestCase(unittest.TestCase):
    def test_seed_pool_and_gate_events_write_incremental_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _build_config(database_path=os.path.join(tmpdir, "stock_analysis.db"))
            request = agent.AgentTraceRunRequest(message="下周可入手股票")
            with patch("api.v1.endpoints.agent.get_config", return_value=config):
                writer = agent.TraceArtifactWriter("trace-seed-test")
                writer.initialize(request=request, context={})
                writer.append_event({
                    "type": "selection_seed_pool_built",
                    "payload": {
                        "phase": "built",
                        "seed_pool_summary": {
                            "seed_count": 2,
                            "seed_sources": {"local_price_volume": 1, "user_watchlist": 1},
                        },
                        "seed_pool_diagnostics": [
                            {"source": "local_price_volume", "status": "ok", "count": 1}
                        ],
                        "seed_pool_hard_exclusion": {"excluded_count": 0},
                        "seed_source_quality": {"local_price_volume": {"status": "ok"}},
                    },
                })
                writer.append_event({
                    "type": "selection_seed_gate_done",
                    "payload": {
                        "status": "ok",
                        "phase": "gate",
                        "seed_pool_summary_before_gate": {"seed_count": 2},
                        "seed_pool_summary": {"seed_count": 1},
                        "seed_gate": {"status": "ok", "kept_count": 1, "rejected_count": 1},
                        "candidate_count": 1,
                        "candidate_source": "llm_expert_committee",
                    },
                })

                with open(os.path.join(writer.path, "seed_pool.json"), encoding="utf-8") as fh:
                    seed_pool = json.load(fh)
                with open(os.path.join(writer.path, "seed_gate.json"), encoding="utf-8") as fh:
                    seed_gate = json.load(fh)
                with open(os.path.join(writer.path, "events.ndjson"), encoding="utf-8") as fh:
                    event_types = [json.loads(line)["type"] for line in fh if line.strip()]

        self.assertEqual(event_types, ["selection_seed_pool_built", "selection_seed_gate_done"])
        self.assertEqual(seed_pool["seed_pool_summary"]["seed_count"], 2)
        self.assertEqual(seed_pool["seed_pool_diagnostics"][0]["source"], "local_price_volume")
        self.assertEqual(seed_gate["seed_gate"]["kept_count"], 1)
        self.assertEqual(seed_gate["candidate_source"], "llm_expert_committee")


if __name__ == "__main__":
    unittest.main()
