# -*- coding: utf-8 -*-
"""Unit tests for LLMToolAdapter message conversion (offline, no network)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace


def _minimal_adapter_config() -> SimpleNamespace:
    return SimpleNamespace(
        agent_litellm_model="",
        litellm_model="",
        llm_model_list=[],
        llm_temperature=0.7,
    )


def test_convert_messages_handles_nested_and_flat_tool_calls():
    """Regression: candidate-expert committee emits nested OpenAI tool_calls
    ({"function": {"name", "arguments"}}); the main executor emits the flat
    shape ({"name", "arguments"}). _convert_messages must accept both without
    raising KeyError('name') on multi-round desk tool calls."""
    from src.agent.llm_adapter import LLMToolAdapter

    conv = LLMToolAdapter._convert_messages
    dummy = SimpleNamespace()

    nested = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0_0",
                    "type": "function",
                    "function": {
                        "name": "calculate_ma",
                        "arguments": '{"stock_code": "600027"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0_0", "content": {"ma20": 1.2}},
    ]
    out = conv(dummy, nested)
    assert out[1]["tool_calls"][0]["function"]["name"] == "calculate_ma"
    assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"stock_code": "600027"}'

    flat = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "x", "name": "get_volume_analysis", "arguments": {"stock_code": "600027"}}
            ],
        }
    ]
    out2 = conv(dummy, flat)
    assert out2[0]["tool_calls"][0]["function"]["name"] == "get_volume_analysis"
    assert json.loads(out2[0]["tool_calls"][0]["function"]["arguments"]) == {"stock_code": "600027"}


def test_call_completion_enforces_hard_timeout_around_litellm_retries(monkeypatch):
    """Regression: LiteLLM/Router timeout may apply per retry, so adapter-level
    calls need a hard wall-clock guard before the desk seed timeout fires."""
    from src.agent import llm_adapter as module
    from src.agent.llm_adapter import LLMToolAdapter

    monkeypatch.setattr(module, "get_effective_agent_models_to_try", lambda _config: ["unit/model"])

    adapter = LLMToolAdapter(config=_minimal_adapter_config())

    def slow_call(*_args, **_kwargs):
        time.sleep(0.3)
        raise AssertionError("slow call should be cut off by hard timeout")

    monkeypatch.setattr(adapter, "_call_litellm_model", slow_call)

    started = time.time()
    response = adapter.call_completion(
        [{"role": "user", "content": "ping"}],
        timeout=0.05,
    )

    assert time.time() - started < 0.2
    assert response.provider == "error"
    assert "hard timeout" in str(response.content)


def test_call_completion_passes_full_remaining_timeout_to_current_model(monkeypatch):
    """The adapter should not pre-split the timeout between primary and fallback."""
    from src.agent import llm_adapter as module
    from src.agent.llm_adapter import LLMResponse, LLMToolAdapter

    monkeypatch.setattr(module, "get_effective_agent_models_to_try", lambda _config: ["unit/slow", "unit/fast"])

    adapter = LLMToolAdapter(config=_minimal_adapter_config())
    calls = []

    def fake_call(*_args, **kwargs):
        model = _args[2]
        calls.append((model, kwargs.get("timeout")))
        if model == "unit/slow":
            raise RuntimeError("primary failed quickly")
        return LLMResponse(content="ok", provider="unit", model=model)

    monkeypatch.setattr(adapter, "_call_litellm_model", fake_call)

    response = adapter.call_completion(
        [{"role": "user", "content": "ping"}],
        timeout=0.2,
    )

    assert response.provider == "unit"
    assert response.model == "unit/fast"
    assert [item[0] for item in calls] == ["unit/slow", "unit/fast"]
    assert calls[0][1] > 0.18
    assert 0 < calls[1][1] <= calls[0][1]


def test_call_completion_passes_response_format_to_litellm(monkeypatch):
    """Candidate desks rely on provider-side JSON mode when available."""
    from src.agent import llm_adapter as module
    from src.agent.llm_adapter import LLMResponse, LLMToolAdapter

    monkeypatch.setattr(module, "get_effective_agent_models_to_try", lambda _config: ["unit/model"])

    adapter = LLMToolAdapter(config=_minimal_adapter_config())
    seen = {}

    def fake_call(*_args, **kwargs):
        seen["response_format"] = kwargs.get("response_format")
        return LLMResponse(content='{"candidates":[],"rejected":[]}', provider="unit", model="unit/model")

    monkeypatch.setattr(adapter, "_call_litellm_model", fake_call)

    response = adapter.call_completion(
        [{"role": "user", "content": "ping"}],
        response_format={"type": "json_object"},
    )

    assert response.provider == "unit"
    assert seen["response_format"] == {"type": "json_object"}


def test_call_completion_injects_json_prompt_for_json_response_format(monkeypatch):
    """DeepSeek JSON Output requires prompts to mention JSON."""
    from src.agent import llm_adapter as module
    from src.agent.llm_adapter import LLMResponse, LLMToolAdapter

    monkeypatch.setattr(module, "get_effective_agent_models_to_try", lambda _config: ["unit/model"])

    adapter = LLMToolAdapter(config=_minimal_adapter_config())
    seen = {}

    def fake_call(messages, *_args, **kwargs):
        seen["messages"] = messages
        seen["response_format"] = kwargs.get("response_format")
        return LLMResponse(content='{"ok":true}', provider="unit", model="unit/model")

    monkeypatch.setattr(adapter, "_call_litellm_model", fake_call)

    response = adapter.call_completion(
        [{"role": "user", "content": "只输出对象"}],
        response_format={"type": "json_object"},
    )

    assert response.provider == "unit"
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["messages"][0]["role"] == "system"
    assert "JSON" in seen["messages"][0]["content"]
