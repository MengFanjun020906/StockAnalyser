# -*- coding: utf-8 -*-
"""Tests for src.agent.candidate_experts_v2 skeleton + capital_flow expert.

Covers:
- schemas round-trip (no v1 compatibility shim required)
- file cache TTL + cross-trading-day isolation
- runtime parallel: success / failure / timeout packets
- BaseExpert bounded loop: tool whitelist enforcement, tool_call cap,
  no-tool partial/invalid, malformed JSON failure
- CapitalFlowExpert: tool list, dimension, end-to-end happy path with fake LLM
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
from src.agent.candidate_experts_v2.experts.capital_flow import (
    CAPITAL_FLOW_TOOLS,
    CapitalFlowExpert,
)
from src.agent.candidate_experts_v2.experts.early_turn import (
    EARLY_TURN_TOOLS,
    EarlyTurnExpert,
)


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

    def __call__(self, messages, tool_decls):  # noqa: D401
        self.calls.append({"n_messages": len(messages), "n_tools": len(tool_decls)})
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
    llm = _ScriptedLLM([LLMTurn(text="this is not json")])
    expert = _make_expert(llm=llm, tool_registry={})
    pkt = expert.run([SeedItem(code="600000")], use_cache=False)
    assert pkt.status == "failed"
    assert "final_output_not_json" in pkt.errors


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


# ---------------------------------------------------------------------------
# CapitalFlowExpert
# ---------------------------------------------------------------------------


def test_capital_flow_expert_whitelist_and_dimension():
    expert = CapitalFlowExpert(
        tool_registry={},
        tool_decls=[],
        llm=_ScriptedLLM([LLMTurn(text='{"candidates": []}')]),
    )
    assert expert.dimension == "capital"
    assert "get_tushare_moneyflow_ths" in expert.allowed_tools
    assert "get_fundamental_indicators" not in expert.allowed_tools


def test_capital_flow_expert_runs_with_capital_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    tool_registry = {
        name: (lambda **kw: {"status": "ok", "items": []})
        for name in CAPITAL_FLOW_TOOLS
    }
    tool_registry["get_tushare_moneyflow_ths"] = lambda **kw: {
        "status": "ok",
        "items": [{"code": "600000", "net_inflow": 100_000_000}],
    }
    tool_decls = [
        {"type": "function", "function": {"name": name}}
        for name in CAPITAL_FLOW_TOOLS
    ]
    llm = _ScriptedLLM(
        [
            LLMTurn(tool_calls=[LLMToolCall(name="get_tushare_moneyflow_ths", call_id="c1")]),
            LLMTurn(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "code": "600000",
                                "name": "浦发银行",
                                "score": 80,
                                "confidence": 0.65,
                                "reason": "主力净流入靠前",
                                "evidence": [
                                    {
                                        "tool": "get_tushare_moneyflow_ths",
                                        "summary": "近一日主力净流入靠前",
                                        "metrics": {"net_inflow": 100_000_000},
                                    }
                                ],
                            }
                        ]
                    }
                )
            ),
        ]
    )
    expert = CapitalFlowExpert(tool_registry=tool_registry, tool_decls=tool_decls, llm=llm)
    pkt = expert.run([SeedItem(code="600000", source="limit_up_pool")], use_cache=False)
    assert pkt.expert == "capital_flow_expert"
    assert pkt.status == "ok"
    assert pkt.candidates[0].code == "600000"
    assert pkt.candidates[0].evidence[0].tool == "get_tushare_moneyflow_ths"


def test_early_turn_expert_whitelist_and_dimension():
    expert = EarlyTurnExpert(
        tool_registry={},
        tool_decls=[],
        llm=_ScriptedLLM([LLMTurn(text='{"candidates": []}')]),
    )
    assert expert.dimension == "early_turn"
    assert "analyze_trend" in expert.allowed_tools
    assert "get_tushare_moneyflow_ths" not in expert.allowed_tools


def test_early_turn_expert_runs_with_structure_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANDIDATE_V2_CACHE_DIR", str(tmp_path))
    tool_registry = {
        name: (lambda **kw: {"status": "ok"})
        for name in EARLY_TURN_TOOLS
    }
    tool_registry["analyze_trend"] = lambda **kw: {
        "trend_status": "弱转中性",
        "support_levels": [10.2],
        "resistance_levels": [11.0],
    }
    tool_decls = [
        {"type": "function", "function": {"name": name}}
        for name in EARLY_TURN_TOOLS
    ]
    llm = _ScriptedLLM(
        [
            LLMTurn(tool_calls=[LLMToolCall(name="analyze_trend", call_id="c1")]),
            LLMTurn(
                text=json.dumps(
                    {
                        "candidates": [
                            {
                                "code": "600000",
                                "name": "浦发银行",
                                "score": 78,
                                "confidence": 0.66,
                                "reason": "仍处中低位，趋势弱转中性，适合作为低位启动候选继续观察。",
                                "evidence": [
                                    {
                                        "tool": "analyze_trend",
                                        "summary": "趋势由弱转中性",
                                        "metrics": {"trend_status": "弱转中性"},
                                    }
                                ],
                            }
                        ]
                    }
                )
            ),
        ]
    )
    expert = EarlyTurnExpert(tool_registry=tool_registry, tool_decls=tool_decls, llm=llm)
    pkt = expert.run([SeedItem(code="600000", source="low_base_structure")], use_cache=False)
    assert pkt.expert == "early_turn_expert"
    assert pkt.status == "ok"
    assert pkt.candidates[0].code == "600000"
    assert pkt.candidates[0].evidence[0].tool == "analyze_trend"
    assert pkt.seed_summary.seed_count == 1
    assert pkt.seed_summary.accepted_count == 1
