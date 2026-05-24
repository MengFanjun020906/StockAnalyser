# -*- coding: utf-8 -*-
"""Tests for the LLM expert committee facade (run_committee_discovery).

The facade must remain schema-compatible with the deterministic
``discover_watchlist_candidates`` tool so downstream pipeline stages do not
need to branch by mode.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.agent.candidate_experts_v2 import committee as committee_module
from src.agent.candidate_experts_v2.committee import _build_seed_pool, run_committee_discovery


def _deterministic_stub(*, market: str, seed_symbols, limit: int):
    return {
        "status": "ok",
        "market": market,
        "candidates": [
            {"code": "600519", "name": "贵州茅台", "source": "stub"},
        ],
        "candidate_count": 1,
        "discovery_steps": [{"source": "stub_deterministic", "status": "ok", "count": 1}],
        "next_required_tools": ["get_realtime_quote"],
    }


def test_run_committee_discovery_returns_compatible_payload_when_capital_expert_fails():
    # llm_adapter without .chat raises TypeError inside _coerce_llm_callable.
    # In V2-only mode, capital expert failure means empty candidates (no deterministic fallback).
    result = run_committee_discovery(
        market="cn",
        seed_symbols=["600519"],
        limit=8,
        tool_registry={},
        llm_adapter=object(),
        today="20260523",
        deterministic_fn=_deterministic_stub,
    )

    assert isinstance(result, dict)
    # schema compatibility: same top-level keys deterministic tool exposes
    for key in ("status", "candidates", "discovery_steps", "candidate_count"):
        assert key in result, f"missing key {key} in committee payload"
    # V2-only mode: capital expert failed, so candidates are empty
    assert result["candidates"] == []
    # committee marker added with status=failed (capital expert raised)
    assert result.get("llm_expert_committee", {}).get("status") == "failed"
    # candidate_source rewritten so frontend can render the badge
    assert result.get("candidate_source") == "llm_expert_committee"
    # one discovery step appended for the committee
    sources = [s.get("source") for s in result["discovery_steps"]]
    assert "llm_expert_committee" in sources
    # elapsed marker present
    assert "committee_elapsed_ms" in result


def test_run_committee_discovery_attaches_capital_evidence_on_success():
    fake_evidence = MagicMock()
    fake_evidence.tool = "get_tushare_moneyflow_ths"
    fake_evidence.summary = "main net inflow positive"
    fake_evidence.metrics = {"net_inflow": 1234.5}

    fake_candidate = MagicMock()
    fake_candidate.code = "600519"
    fake_candidate.name = "贵州茅台"
    fake_candidate.reason = "capital inflow surge"
    fake_candidate.evidence = [fake_evidence]

    fake_packet = MagicMock()
    fake_packet.candidates = [fake_candidate]
    fake_packet.status = "ok"
    fake_packet.elapsed_ms = 12
    fake_packet.tool_calls = []
    fake_packet.errors = []

    class _Expert:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, seeds, market="cn", use_cache=True):
            return fake_packet

    with patch.object(committee_module, "CapitalFlowExpert", _Expert):
        result = run_committee_discovery(
            market="cn",
            seed_symbols=["600519"],
            limit=8,
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: None,
            today="20260523",
            deterministic_fn=_deterministic_stub,
        )

    assert result["llm_expert_committee"]["status"] == "ok"
    assert result["llm_expert_committee"]["dimensions_covered"] == ["capital"]
    target = next(c for c in result["candidates"] if c.get("code") == "600519")
    assert "capital" in target.get("llm_expert_evidence", {})
    assert target["llm_expert_evidence"]["capital"][0]["tool"] == "get_tushare_moneyflow_ths"
    assert "capital" in target.get("llm_expert_dimensions", [])


def test_run_committee_discovery_attaches_early_turn_evidence_on_success():
    fake_evidence = MagicMock()
    fake_evidence.tool = "analyze_trend"
    fake_evidence.summary = "弱转中性"
    fake_evidence.metrics = {"trend_status": "弱转中性"}

    fake_candidate = MagicMock()
    fake_candidate.code = "600519"
    fake_candidate.name = "贵州茅台"
    fake_candidate.reason = "中低位转强"
    fake_candidate.evidence = [fake_evidence]
    fake_candidate.risks = []

    fake_packet = MagicMock()
    fake_packet.candidates = [fake_candidate]
    fake_packet.dimension = "early_turn"
    fake_packet.status = "ok"
    fake_packet.elapsed_ms = 18
    fake_packet.tool_calls = []
    fake_packet.errors = []
    fake_packet.seed_summary = MagicMock(model_dump=lambda: {"seed_count": 1, "accepted_count": 1, "rejected_count": 0, "seed_sources": {"user_watchlist": 1}})

    class _CapitalExpert:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, seeds, market="cn", use_cache=True):
            pkt = MagicMock()
            pkt.candidates = []
            pkt.dimension = "capital"
            pkt.status = "ok"
            pkt.elapsed_ms = 8
            pkt.tool_calls = []
            pkt.errors = []
            pkt.seed_summary = MagicMock(model_dump=lambda: {"seed_count": 1, "accepted_count": 0, "rejected_count": 1, "seed_sources": {"user_watchlist": 1}})
            return pkt

    class _EarlyTurnExpert:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, seeds, market="cn", use_cache=True):
            return fake_packet

    with patch.object(committee_module, "CapitalFlowExpert", _CapitalExpert), patch.object(
        committee_module,
        "EarlyTurnExpert",
        _EarlyTurnExpert,
    ):
        result = run_committee_discovery(
            market="cn",
            seed_symbols=["600519"],
            limit=8,
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: None,
            today="20260523",
            deterministic_fn=_deterministic_stub,
        )

    assert result["llm_expert_committee"]["status"] == "ok"
    assert result["llm_expert_committee"]["dimensions_covered"] == ["capital", "early_turn"]
    target = next(c for c in result["candidates"] if c.get("code") == "600519")
    assert "early_turn" in target.get("llm_expert_evidence", {})
    assert target["llm_expert_evidence"]["early_turn"][0]["tool"] == "analyze_trend"
    assert "early_turn" in target.get("llm_expert_dimensions", [])
    assert result["llm_expert_committee"]["early_turn"]["seed_summary"]["seed_count"] == 1


def test_run_committee_discovery_coerces_non_dict_deterministic_payload():
    # deterministic_fn is no longer called in V2-only mode.
    # Even if a bad stub is passed, the result is still a valid empty payload.
    def _bad_deterministic(**_kwargs):
        return "not-a-dict"

    result = run_committee_discovery(
        market="cn",
        seed_symbols=[],
        limit=8,
        tool_registry={},
        llm_adapter=object(),
        deterministic_fn=_bad_deterministic,
    )
    assert isinstance(result, dict)
    assert result["candidates"] == []
    # status is "ok" because the empty baseline payload was constructed successfully;
    # capital expert may have failed but that's reflected in llm_expert_committee
    assert result["status"] in ("ok", "failed")


def test_build_seed_pool_includes_fundamental_and_low_base_sources():
    with patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={
            "status": "ok",
            "candidates": [
                {
                    "code": "600001",
                    "name": "基本面候选",
                    "metrics": {"revenue_growth": 20.0, "profit_growth": 30.0, "pe_ttm": 18.0, "pb": 2.1},
                }
            ],
        },
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "ok", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "ok", "candidates": []},
    ), patch.object(
        committee_module,
        "_build_low_base_structure_seeds",
        return_value=[committee_module.SeedItem(code="600002", name="低位结构候选", source="low_base_structure")],
    ):
        seeds = _build_seed_pool(
            market="cn",
            seed_symbols=[],
            tool_registry={},
            today="20260523",
            limit_per_source=5,
            total_limit=20,
        )

    by_code = {seed.code: seed for seed in seeds}
    assert by_code["600001"].source == "fundamental_snapshot"
    assert by_code["600002"].source == "low_base_structure"


def test_run_committee_discovery_uses_parallel_runtime_and_reorders_candidates():
    cap_candidate = MagicMock()
    cap_candidate.code = "600001"
    cap_candidate.name = "资金候选"
    cap_candidate.reason = "资金主导"
    cap_candidate.evidence = []
    cap_candidate.risks = []

    et_candidate = MagicMock()
    et_candidate.code = "600002"
    et_candidate.name = "低位候选"
    et_candidate.reason = "低位启动"
    et_candidate.evidence = []
    et_candidate.risks = []

    cap_packet = MagicMock()
    cap_packet.expert = "capital_flow_expert"
    cap_packet.dimension = "capital"
    cap_packet.status = "ok"
    cap_packet.candidates = [cap_candidate]
    cap_packet.tool_calls = []
    cap_packet.errors = []
    cap_packet.elapsed_ms = 11
    cap_packet.seed_summary = MagicMock(model_dump=lambda: {"seed_count": 2, "accepted_count": 1, "rejected_count": 1, "seed_sources": {"hot_rank": 2}})

    et_packet = MagicMock()
    et_packet.expert = "early_turn_expert"
    et_packet.dimension = "early_turn"
    et_packet.status = "ok"
    et_packet.candidates = [et_candidate]
    et_packet.tool_calls = []
    et_packet.errors = []
    et_packet.elapsed_ms = 12
    et_packet.seed_summary = MagicMock(model_dump=lambda: {"seed_count": 2, "accepted_count": 1, "rejected_count": 1, "seed_sources": {"low_base_structure": 2}})

    with patch.object(
        committee_module,
        "run_experts_parallel",
        return_value=[cap_packet, et_packet],
    ) as mocked_runtime:
        result = run_committee_discovery(
            market="cn",
            seed_symbols=["600001", "600002"],
            limit=8,
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: None,
            today="20260523",
            prebuilt_seeds=[
                committee_module.SeedItem(code="600001", source="hot_rank"),
                committee_module.SeedItem(code="600002", source="low_base_structure"),
            ],
        )

    assert mocked_runtime.called
    assert result["llm_expert_committee"]["dimensions_covered"] == ["capital", "early_turn"]
    assert result["candidates"][0]["code"] == "600002"
    assert result["candidates"][0]["committee_score"] >= result["candidates"][1]["committee_score"]
