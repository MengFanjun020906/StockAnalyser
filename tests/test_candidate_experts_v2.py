# -*- coding: utf-8 -*-
"""Tests for src.agent.candidate_experts_v2 skeleton.

Covers:
- schemas round-trip (no v1 compatibility shim required)
- file cache TTL + cross-trading-day isolation
- runtime parallel: success / failure / timeout packets
- BaseExpert bounded loop: tool whitelist enforcement, tool_call cap,
  no-tool partial/invalid, malformed JSON failure
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from src.agent.candidate_experts_v2 import (
    EvidenceItem,
    ExpertCandidateV2,
    ExpertPacketV2,
    SeedItem,
)
from src.agent.candidate_experts_v2 import cache as cache_mod
from src.agent.candidate_experts_v2 import runtime as runtime_mod
from src.agent.candidate_experts_v2.experts.base import (
    BaseExpert,
    LLMToolCall,
    LLMTurn,
)
from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.schemas import FeatureFlag, FeatureRow
from src.agent.llm_telemetry import llm_telemetry_scope, record_llm_telemetry

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_evidence_item_required_tool():
    ev = EvidenceItem(tool="get_tushare_moneyflow_ths", summary="x", metrics={"a": 1})
    dumped = ev.model_dump()
    assert dumped["tool"] == "get_tushare_moneyflow_ths"
    assert dumped["metrics"] == {"a": 1}


def test_expert_candidate_v2_accepts_edges():
    assert ExpertCandidateV2(code="600000", score=0).score == 0
    assert ExpertCandidateV2(code="600000", score=100).score == 100


def test_expert_candidate_v2_rejects_out_of_range():
    with pytest.raises(Exception):
        ExpertCandidateV2(code="600000", score=200)


def test_packet_default_status_empty():
    pkt = ExpertPacketV2(expert="capital_flow_expert", dimension="capital")
    assert pkt.status == "empty"
    assert pkt.candidates == []
    assert pkt.cache_hit is False
    assert pkt.seed_summary.seed_count == 0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _seeds() -> List[SeedItem]:
    return [
        SeedItem(code="600000", name="浦发银行", source="limit_up_pool"),
        SeedItem(code="000001", name="平安银行", source="hot_rank"),
    ]


def test_cache_roundtrip_within_day(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    seeds = _seeds()
    h = cache_mod.seed_hash(seeds)
    key = cache_mod.cache_key("capital_flow_expert", "cn", h)

    pkt = ExpertPacketV2(
        expert="capital_flow_expert",
        dimension="capital",
        status="ok",
        candidates=[ExpertCandidateV2(code="600000", name="浦发银行", score=80, confidence=0.7)],
    )
    cache_mod.save_packet(key, pkt, dimension="capital")
    loaded = cache_mod.load_packet(key, dimension="capital")
    assert loaded is not None
    assert loaded.cache_hit is True
    assert loaded.candidates[0].code == "600000"


def test_cache_skips_when_cross_day(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    key = cache_mod.cache_key("capital_flow_expert", "cn", "abc")
    pkt = ExpertPacketV2(expert="capital_flow_expert", dimension="capital", status="ok")
    cache_mod.save_packet(key, pkt, dimension="capital")

    # tamper the file to simulate a different trading day
    path = cache_mod._key_to_path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["_date"] = "1999-01-01"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache_mod.load_packet(key, dimension="capital") is None


def test_cache_ttl_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    key = cache_mod.cache_key("capital_flow_expert", "cn", "abc")
    pkt = ExpertPacketV2(expert="capital_flow_expert", dimension="capital", status="ok")
    cache_mod.save_packet(key, pkt, dimension="capital")
    path = cache_mod._key_to_path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["_ts"] = time.time() - 10 * 24 * 3600  # 10 days ago
    payload["_ttl"] = 60
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache_mod.load_packet(key, dimension="capital") is None


def test_seed_hash_is_stable_and_order_insensitive():
    a = cache_mod.seed_hash(
        [SeedItem(code="600000", source="limit_up_pool"), SeedItem(code="000001", source="hot_rank")]
    )
    b = cache_mod.seed_hash(
        [SeedItem(code="000001", source="hot_rank"), SeedItem(code="600000", source="limit_up_pool")]
    )
    assert a == b


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def test_runtime_parallel_collects_success_and_failure():
    def ok_task() -> ExpertPacketV2:
        return ExpertPacketV2(expert="capital_flow_expert", dimension="capital", status="ok")

    def boom_task() -> ExpertPacketV2:
        raise RuntimeError("boom")

    packets = runtime_mod.run_experts_parallel(
        {"capital_flow_expert": ok_task, "broken_expert": boom_task},
        overall_timeout_s=5.0,
        per_expert_timeout_s=5.0,
    )
    by_name = {pkt.expert: pkt for pkt in packets}
    assert by_name["capital_flow_expert"].status == "ok"
    assert by_name["broken_expert"].status == "failed"
    assert by_name["broken_expert"].errors


def test_runtime_parallel_timeout_marks_missing():
    def slow_task() -> ExpertPacketV2:
        time.sleep(2.0)
        return ExpertPacketV2(expert="slow_expert", dimension="capital", status="ok")

    packets = runtime_mod.run_experts_parallel(
        {"slow_expert": slow_task},
        overall_timeout_s=0.2,
        per_expert_timeout_s=0.2,
        max_workers=1,
    )
    assert len(packets) == 1
    assert packets[0].status == "timeout"


# ---------------------------------------------------------------------------
# BaseExpert bounded loop
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """LLM driver that replays a fixed list of turns."""

    def __init__(self, turns: List[LLMTurn]) -> None:
        self._turns = list(turns)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, messages, tool_decls, **kwargs):  # noqa: D401
        self.calls.append({"n_messages": len(messages), "n_tools": len(tool_decls), "kwargs": dict(kwargs)})
        if not self._turns:
            return LLMTurn(text="{}")
        return self._turns.pop(0)


def _make_expert(
    *,
    llm: _ScriptedLLM,
    tool_registry: Dict[str, Any],
    allowed=("get_tushare_moneyflow_ths",),
    decls=None,
    max_tool_calls: int = 6,
    max_rounds: int = 3,
) -> BaseExpert:
    if decls is None:
        decls = [
            {"type": "function", "function": {"name": name}}
            for name in allowed
        ]
    return BaseExpert(
        allowed_tools=allowed,
        tool_registry=tool_registry,
        tool_decls=decls,
        llm=llm,
        system_prompt="You are a test expert.",
        max_llm_rounds=max_rounds,
        max_tool_calls=max_tool_calls,
    )


def test_base_expert_happy_path_with_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    moneyflow_payload = {
        "status": "ok",
        "items": [{"code": "600000", "net_inflow": 280_000_000, "rank": 3}],
    }
    tool_registry = {"get_tushare_moneyflow_ths": lambda **kwargs: moneyflow_payload}

    final_text = json.dumps(
        {
            "candidates": [
                {
                    "code": "600000",
                    "name": "浦发银行",
                    "score": 86,
                    "confidence": 0.72,
                    "reason": "主力净流入靠前",
                    "evidence": [
                        {
                            "tool": "get_tushare_moneyflow_ths",
                            "summary": "近一日主力净流入靠前",
                            "metrics": {"net_inflow": 280_000_000},
                        }
                    ],
                }
            ]
        }
    )
    llm = _ScriptedLLM(
        [
            LLMTurn(tool_calls=[LLMToolCall(name="get_tushare_moneyflow_ths", call_id="c1")]),
            LLMTurn(text=final_text),
        ]
    )
    expert = _make_expert(llm=llm, tool_registry=tool_registry)
    pkt = expert.run([SeedItem(code="600000", source="limit_up_pool")], use_cache=False)
    assert pkt.status == "ok"
    assert len(pkt.candidates) == 1
    assert pkt.candidates[0].evidence[0].tool == "get_tushare_moneyflow_ths"
    assert any(c["status"] == "ok" for c in pkt.tool_calls)


def test_base_expert_uses_json_output_for_final_turn_after_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    tool_registry = {"get_tushare_moneyflow_ths": lambda **kwargs: {"status": "ok"}}
    llm = _ScriptedLLM(
        [
            LLMTurn(tool_calls=[LLMToolCall(name="get_tushare_moneyflow_ths", call_id="c1")]),
            LLMTurn(text='{"data_quality":{"warnings":[]},"candidates":[],"rejected":[]}'),
        ]
    )
    expert = _make_expert(llm=llm, tool_registry=tool_registry)

    pkt = expert.run([SeedItem(code="600000", source="limit_up_pool")], use_cache=False)

    assert pkt.status == "empty"
    assert llm.calls[0]["kwargs"] == {}
    assert llm.calls[1]["kwargs"]["response_format"] == {"type": "json_object"}
    assert llm.calls[1]["kwargs"]["max_tokens"] >= 8192


def test_base_expert_marks_empty_json_output_content(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    llm = _ScriptedLLM([LLMTurn(text="")])
    expert = _make_expert(llm=llm, tool_registry={}, allowed=(), decls=[], max_rounds=1)

    pkt = expert.run([SeedItem(code="600000", source="limit_up_pool")], use_cache=False)

    assert pkt.status == "failed"
    assert "final_output_not_json" in pkt.errors
    assert "final_output_empty_content" in pkt.errors


def test_base_desk_expert_runs_and_saves_one_prompt_per_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))

    class _PerSeedLLM:
        def __init__(self) -> None:
            self.prompts: List[str] = []

        def __call__(self, messages, tool_decls):  # noqa: D401
            prompt = str(messages[-1]["content"])
            self.prompts.append(prompt)
            code = "600000" if "600000" in prompt else "000001"
            return LLMTurn(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "code": code,
                                "name": code,
                                "market": "cn",
                                "setup_type": "early_turn",
                                "stance": "support",
                                "reason": "single seed decision",
                                "evidence": [{"tool": "manual_check", "summary": "ok"}],
                            }
                        ],
                        "rejected": [],
                    },
                    ensure_ascii=False,
                )
            )

    llm = _PerSeedLLM()
    desk = BaseDeskExpert(
        allowed_tools=(),
        tool_registry={},
        tool_decls=[],
        llm=llm,
        system_prompt="test desk",
    )
    rows = [
        FeatureRow(
            code="600000",
            name="浦发银行",
            recall_sources=["daily_screener"],
            flags=[FeatureFlag(detector="unit:a", kind="pattern", summary="A")],
        ),
        FeatureRow(
            code="000001",
            name="平安银行",
            recall_sources=["fundamental_snapshot"],
            flags=[FeatureFlag(detector="unit:b", kind="fundamental", summary="B")],
        ),
    ]

    packet = desk.run_desk(rows, market="cn", regime="unknown")

    assert len(llm.prompts) == 2
    assert '"code": "600000"' in llm.prompts[0]
    assert '"code": "000001"' not in llm.prompts[0]
    assert '"code": "000001"' in llm.prompts[1]
    assert [candidate.code for candidate in packet.candidates] == ["600000", "000001"]
    dumped = packet.model_dump(mode="json")
    assert len(dumped["per_seed_packets"]) == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_base_desk_expert_fails_and_stops_after_per_seed_timeouts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))

    class _SlowLLM:
        def __call__(self, messages, tool_decls):  # noqa: D401
            time.sleep(2.0)
            return LLMTurn(text='{"candidates": []}')

    desk = BaseDeskExpert(
        allowed_tools=(),
        tool_registry={},
        tool_decls=[],
        llm=_SlowLLM(),
        system_prompt="test desk",
        max_llm_rounds=1,
    )
    rows = [
        FeatureRow(code="600000", name="浦发银行", recall_sources=["daily_screener"]),
        FeatureRow(code="000001", name="平安银行", recall_sources=["daily_screener"]),
        FeatureRow(code="000002", name="万科A", recall_sources=["daily_screener"]),
    ]

    started = time.time()
    packet = desk.run_desk(
        rows,
        market="cn",
        regime="unknown",
        per_seed_timeout_s=1.0,
        max_consecutive_seed_timeouts=2,
    )

    assert time.time() - started < 3.0
    assert packet.status == "failed"
    assert packet.seed_summary.seed_count == 3
    dumped = packet.model_dump(mode="json")
    assert [item["status"] for item in dumped["per_seed_packets"]] == [
        "timeout",
        "timeout",
        "unavailable",
    ]
    assert "consecutive_seed_timeouts" in json.dumps(packet.diagnostics, ensure_ascii=False)
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_base_desk_timeout_preserves_llm_tool_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))

    class _ToolCallingLLM:
        def __call__(self, messages, tool_decls):  # noqa: D401
            return LLMTurn(
                tool_calls=[
                    LLMToolCall(
                        name="analyze_trend",
                        arguments={"stock_code": "600000"},
                        call_id="call_trend",
                    )
                ]
            )

    def _slow_tool(**kwargs):
        time.sleep(2.0)
        return {"status": "ok", "kwargs": kwargs}

    desk = BaseDeskExpert(
        allowed_tools=("analyze_trend",),
        tool_registry={"analyze_trend": _slow_tool},
        tool_decls=[{"type": "function", "function": {"name": "analyze_trend"}}],
        llm=_ToolCallingLLM(),
        system_prompt="test desk",
        max_llm_rounds=2,
    )

    packet = desk.run_desk(
        [FeatureRow(code="600000", name="浦发银行", recall_sources=["daily_screener"])],
        market="cn",
        regime="unknown",
        per_seed_timeout_s=0.5,
        max_consecutive_seed_timeouts=1,
    )

    dumped = packet.model_dump(mode="json")
    seed_packet = dumped["per_seed_packets"][0]
    assert seed_packet["status"] == "timeout"
    assert seed_packet["tool_calls"] == [
        {
            "tool": "analyze_trend",
            "status": "requested_before_timeout",
            "stock_code": "600000",
        }
    ]
    diagnostics_text = json.dumps(seed_packet["diagnostics"], ensure_ascii=False)
    assert "LLM 已返回工具调用" in diagnostics_text
    assert "analyze_trend" in diagnostics_text
    assert "pending_tools" in diagnostics_text


def test_base_desk_expert_stops_after_consecutive_seed_llm_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))

    class _FailingLLM:
        def __call__(self, messages, tool_decls):  # noqa: D401
            raise RuntimeError("provider hard timeout")

    desk = BaseDeskExpert(
        allowed_tools=(),
        tool_registry={},
        tool_decls=[],
        llm=_FailingLLM(),
        system_prompt="test desk",
        max_llm_rounds=1,
    )
    rows = [
        FeatureRow(code="600000", name="浦发银行", recall_sources=["daily_screener"]),
        FeatureRow(code="000001", name="平安银行", recall_sources=["daily_screener"]),
        FeatureRow(code="000002", name="万科A", recall_sources=["daily_screener"]),
    ]

    packet = desk.run_desk(
        rows,
        market="cn",
        regime="unknown",
        per_seed_timeout_s=5.0,
        max_consecutive_seed_timeouts=2,
    )

    assert packet.status == "failed"
    dumped = packet.model_dump(mode="json")
    assert [item["status"] for item in dumped["per_seed_packets"]] == [
        "failed",
        "failed",
        "unavailable",
    ]
    assert "consecutive_seed_failures" in json.dumps(packet.diagnostics, ensure_ascii=False)
    assert "provider hard timeout" in json.dumps(packet.errors, ensure_ascii=False)


def test_candidate_runtime_propagates_llm_telemetry_context(tmp_path):
    def _task():
        record_llm_telemetry(
            model="unit/model",
            provider="unit",
            ok=True,
            latency_ms=12.0,
        )
        return ExpertPacketV2(expert="unit_desk", dimension="unit", status="ok")

    with llm_telemetry_scope(
        trace_id="trace-unit",
        artifact_dir=str(tmp_path),
        stage="candidate_discovery",
        agent_role="unit_desk",
    ):
        packets = runtime_mod.run_experts_parallel({"unit_desk": _task}, overall_timeout_s=3.0)

    assert packets[0].status == "ok"
    rows = (tmp_path / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    assert payload["trace_id"] == "trace-unit"
    assert payload["stage"] == "candidate_discovery"
    assert payload["agent_role"] == "unit_desk"


def test_base_desk_expert_propagates_llm_telemetry_context_to_seed_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path / "cache"))

    class _TelemetryLLM:
        def __call__(self, messages, tool_decls):  # noqa: D401
            record_llm_telemetry(
                model="unit/model",
                provider="unit",
                ok=True,
                latency_ms=34.0,
            )
            return LLMTurn(text='{"candidates": []}')

    desk = BaseDeskExpert(
        allowed_tools=(),
        tool_registry={},
        tool_decls=[],
        llm=_TelemetryLLM(),
        system_prompt="test desk",
        max_llm_rounds=1,
    )

    with llm_telemetry_scope(
        trace_id="trace-unit",
        artifact_dir=str(tmp_path),
        stage="candidate_discovery",
        agent_role="thesis_desk_committee",
    ):
        packet = desk.run_desk(
            [FeatureRow(code="600000", name="浦发银行", recall_sources=["daily_screener"])],
            market="cn",
            regime="unknown",
            per_seed_timeout_s=5.0,
        )

    assert packet.status in {"empty", "partial"}
    rows = (tmp_path / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    assert payload["trace_id"] == "trace-unit"
    assert payload["stage"] == "candidate_discovery:base_desk_expert"
    assert payload["agent_role"] == "base_desk_expert"
    assert payload["symbol"] == "600000"


def test_base_expert_rejects_non_whitelisted_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    tool_registry = {
        "get_tushare_moneyflow_ths": lambda **kw: {"status": "ok", "items": []},
        "get_fundamental_indicators": lambda **kw: {"status": "ok", "items": []},
    }
    llm = _ScriptedLLM(
        [
            LLMTurn(tool_calls=[LLMToolCall(name="get_fundamental_indicators", call_id="c1")]),
            LLMTurn(text='{"candidates": []}'),
        ]
    )
    expert = _make_expert(
        llm=llm,
        tool_registry=tool_registry,
        allowed=("get_tushare_moneyflow_ths",),
        decls=[{"type": "function", "function": {"name": "get_tushare_moneyflow_ths"}}],
    )
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    rejections = [diag for diag in pkt.diagnostics if diag.get("status") == "tool_not_whitelisted"]
    assert rejections, "non-whitelisted tool should be rejected"
    assert rejections[0]["tool"] == "get_fundamental_indicators"
    # status is "empty" (or "partial") because no whitelisted tool was actually used
    assert pkt.status in {"partial", "empty"}


def test_base_expert_marks_partial_when_no_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    llm = _ScriptedLLM(
        [
            LLMTurn(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "code": "600000",
                                "name": "浦发银行",
                                "evidence": [
                                    {"tool": "get_tushare_moneyflow_ths", "summary": "x"}
                                ],
                            }
                        ]
                    }
                )
            )
        ]
    )
    expert = _make_expert(llm=llm, tool_registry={})
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    assert pkt.status == "partial"
    no_tool_diag = [d for d in pkt.diagnostics if d.get("status") == "no_tool_calls"]
    assert no_tool_diag


def test_base_expert_fails_on_non_json_final(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    llm = _ScriptedLLM(
        [
            LLMTurn(
                text="this is not json",
                usage={"completion_tokens": 8192},
            )
        ]
    )
    expert = _make_expert(llm=llm, tool_registry={})
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    assert pkt.status == "failed"
    assert "final_output_not_json" in pkt.errors
    assert "final_output_maybe_truncated" in pkt.errors
    assert pkt.rejected[0]["code"] == "600000"
    diag = [item for item in pkt.diagnostics if item.get("status") == "final_output_not_json"][0]
    assert diag["completion_tokens"] == 8192
    assert "this is not json" in diag["text_preview"]


def test_base_expert_extracts_json_object_from_wrapped_final(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    wrapped = (
        "以下是最终 JSON：\n"
        '{"candidates":[{"code":"600000","name":"浦发银行","evidence":[{"tool":"get_tushare_moneyflow_ths","summary":"x"}]}],'
        '"rejected":[],"data_quality":{"status":"partial"}}\n'
        "请查收。"
    )
    llm = _ScriptedLLM([LLMTurn(text=wrapped)])
    expert = _make_expert(llm=llm, tool_registry={})
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    assert pkt.status == "partial"
    assert pkt.candidates[0].code == "600000"


def test_base_expert_tool_call_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    call_count = {"n": 0}

    def fake_tool(**kwargs):
        call_count["n"] += 1
        return {"status": "ok", "items": []}

    tool_registry = {"get_tushare_moneyflow_ths": fake_tool}
    # 3 tool calls in one assistant turn, cap=2
    llm = _ScriptedLLM(
        [
            LLMTurn(
                tool_calls=[
                    LLMToolCall(name="get_tushare_moneyflow_ths", call_id=f"c{i}")
                    for i in range(3)
                ]
            ),
            LLMTurn(text='{"candidates": []}'),
        ]
    )
    expert = _make_expert(llm=llm, tool_registry=tool_registry, max_tool_calls=2)
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    assert call_count["n"] == 2
    assert any(d.get("status") == "tool_call_cap_reached" for d in pkt.diagnostics)
