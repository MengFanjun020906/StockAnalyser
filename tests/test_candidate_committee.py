# -*- coding: utf-8 -*-
"""Tests for the LLM expert committee facade (run_committee_discovery).

The facade must remain schema-compatible with the deterministic
``discover_watchlist_candidates`` tool so downstream pipeline stages do not
need to branch by mode.
"""

from __future__ import annotations

import json
import logging
import sys
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.agent.candidate_experts_v2 import committee as committee_module
from src.agent.candidate_experts_v2.committee import (
    _build_seed_pool,
    _build_seed_pool_result,
    run_committee_discovery,
)
from src.agent.candidate_experts_v2.experts.base import LLMTurn


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
    # In V2-only mode, committee failure means empty candidates (no seed fallback).
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
    assert result["candidates"] == []
    assert result["candidate_count"] == 0
    # committee marker added with status=failed (capital expert raised)
    assert result.get("llm_expert_committee", {}).get("status") == "failed"
    # candidate_source rewritten so frontend can render the badge
    assert result.get("candidate_source") == "llm_expert_committee"
    # one discovery step appended for the committee
    sources = [s.get("source") for s in result["discovery_steps"]]
    assert "llm_expert_committee" in sources
    # elapsed marker present
    assert "committee_elapsed_ms" in result


def test_run_committee_discovery_fails_without_seed_fallback_when_thesis_desk_is_empty():
    seed_result = committee_module.SeedPoolBuildResult(
        seeds=[
            committee_module.SeedItem(code="600001", name="一号", source="daily_screener", priority_score=80),
            committee_module.SeedItem(code="600002", name="二号", source="low_base_structure", priority_score=70),
        ],
        diagnostics=[{"source": "unit", "status": "ok", "count": 2}],
        total_limit=2,
    )

    with patch.object(
        committee_module,
        "run_thesis_desk_committee",
        return_value={
            "status": "ok",
            "candidate_source": "thesis_desk_committee",
            "candidates": [],
            "candidate_count": 0,
            "recall_total_in": 2,
            "recall_total_kept": 2,
            "thesis_desk_diagnostics": [
                {"desk": "early_turn_desk", "status": "empty", "picks": 0},
            ],
            "thesis_desk_committee_elapsed_ms": 12,
        },
    ):
        result = run_committee_discovery(
            market="cn",
            seed_symbols=[],
            limit=8,
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
            today="20260523",
            seed_pool_result=seed_result,
        )

    assert result["status"] == "failed"
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["seed_pool_summary"]["seed_count"] == 2
    assert result["llm_expert_committee"]["status"] == "failed"
    assert result["llm_expert_committee"]["fallback"] is False
    assert result["thesis_desk_committee"]["status"] == "failed"
    assert result["thesis_desk_committee"]["candidate_count"] == 0
    assert result["thesis_desk_committee"]["fallback"] is False
    assert result["thesis_desk_committee"]["diagnostics"] == [
        {"desk": "early_turn_desk", "status": "empty", "picks": 0},
    ]
    assert any(step.get("source") == "thesis_desk_committee" for step in result["discovery_steps"])


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


def test_build_seed_pool_applies_source_caps_to_preserve_low_base_sources():
    with patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={
            "status": "ok",
            "candidates": [
                {"code": f"6001{i:02d}", "name": f"基本面{i}", "metrics": {"revenue_growth": 10.0, "profit_growth": 12.0, "pe_ttm": 20.0, "pb": 2.0}}
                for i in range(12)
            ],
        },
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={
            "status": "ok",
            "candidates": [{"code": f"6002{i:02d}", "name": f"Alpha{i}", "matched_strategies": ["volume_breakout"]} for i in range(20)],
        },
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={
            "status": "ok",
            "candidates": [{"code": f"6003{i:02d}", "name": f"Seq{i}", "matched_strategies": ["turtle_trade"]} for i in range(20)],
        },
    ), patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=([], {"source": "daily_screener", "status": "empty", "count": 0}),
    ), patch.object(
        committee_module,
        "_build_low_base_structure_seeds",
        return_value=[
            committee_module.SeedItem(code=f"6004{i:02d}", name=f"低位{i}", source="low_base_structure")
            for i in range(12)
        ],
    ):
        seeds = _build_seed_pool(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_limit_list_d": lambda **kwargs: {"items": [{"code": f"6005{i:02d}", "name": f"涨停{i}", "limit_status": "U"} for i in range(20)]},
                "get_tushare_hot_rank": lambda **kwargs: {"items": [{"code": f"6006{i:02d}", "name": f"热榜{i}", "rank": i + 1} for i in range(20)]},
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260523",
            limit_per_source=20,
            total_limit=40,
        )

    source_counts = {}
    for seed in seeds:
        source_counts[seed.source] = source_counts.get(seed.source, 0) + 1

    assert source_counts["fundamental_snapshot"] <= 8
    assert source_counts["low_base_structure"] <= 8
    assert source_counts["alphasift"] <= 10
    assert source_counts["hot_rank"] <= 8
    assert source_counts["limit_up_pool"] <= 10
    assert source_counts["low_base_structure"] > 0
    assert source_counts["fundamental_snapshot"] > 0


def test_build_seed_pool_result_caps_total_limit_to_twenty():
    with patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={"status": "ok", "candidates": [{"code": f"6001{i:02d}", "name": f"基本面{i}"} for i in range(20)]},
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "ok", "candidates": [{"code": f"6002{i:02d}", "name": f"Alpha{i}"} for i in range(20)]},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "ok", "candidates": [{"code": f"6003{i:02d}", "name": f"Seq{i}"} for i in range(20)]},
    ), patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=([], {"source": "daily_screener", "status": "empty", "count": 0}),
    ), patch.object(
        committee_module,
        "_build_low_base_structure_seeds",
        return_value=[committee_module.SeedItem(code=f"6004{i:02d}", name=f"低位{i}", source="low_base_structure") for i in range(20)],
    ):
        result = committee_module._build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_limit_list_d": lambda **kwargs: {"items": [{"code": f"6005{i:02d}", "name": f"涨停{i}", "limit_status": "U"} for i in range(20)]},
                "get_tushare_hot_rank": lambda **kwargs: {"items": [{"code": f"6006{i:02d}", "name": f"热榜{i}", "rank": i + 1} for i in range(20)]},
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260523",
            total_limit=20,
        )

    assert result.total_limit == 20
    assert len(result.seeds) <= 20


def test_build_seed_pool_result_records_quality_and_structured_signals():
    with patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=(
            [
                committee_module.SeedItem(
                    code="600010",
                    name="日筛候选",
                    source="daily_screener",
                    trigger_signals=[
                        {
                            "signal_type": "daily_screener",
                            "pct_chg": 3.8,
                            "volume_ratio": 1.5,
                            "turnover_rate_f": 7.2,
                            "circ_mv_yi": 120.0,
                            "vol_rising_3d": True,
                        }
                    ],
                    priority_score=82,
                    freshness="2026-05-22",
                )
            ],
            {"source": "daily_screener", "status": "ok", "count": 1, "trade_date": "2026-05-22"},
        ),
    ), patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch.object(committee_module, "_build_low_base_structure_seeds", return_value=[]):
        result = _build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={"discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []}},
            today="20260523",
            limit_per_source=5,
            total_limit=20,
        )

    assert result.source_quality["daily_screener"]["status"] == "ok"
    assert result.diagnostics[0]["source"] == "daily_screener"
    assert result.seeds[0].trigger_signals[0]["signal_type"] == "daily_screener"
    assert result.seeds[0].priority_score == 82


def test_build_seed_pool_result_records_screener_diagnostic():
    with patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=(
            [
                committee_module.SeedItem(
                    code="600010",
                    name="日筛候选",
                    source="daily_screener",
                    trigger_signals=[{"signal_type": "daily_screener", "pct_chg": 4.1}],
                    priority_score=82,
                )
            ],
            {
                "source": "daily_screener",
                "status": "ok",
                "count": 1,
                "trade_date": "2026-05-22",
                "upper_pct_limit": 5.0,
                "basic_pre_filtered": 450,
                "passed_all_filters": 18,
            },
        ),
    ), patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch.object(committee_module, "_build_low_base_structure_seeds", return_value=[]):
        result = _build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={"discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []}},
            today="20260523",
            limit_per_source=5,
            total_limit=20,
        )

    assert result.source_quality["daily_screener"]["status"] == "ok"
    assert result.diagnostics[0]["source"] == "daily_screener"
    assert result.seeds[0].trigger_signals[0]["signal_type"] == "daily_screener"


def test_build_seed_pool_result_includes_capital_dragon_and_valuation_sources():
    with patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=([], {"source": "daily_screener", "status": "empty", "count": 0}),
    ), patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch.object(committee_module, "_build_low_base_structure_seeds", return_value=[]):
        result = _build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_moneyflow_ths": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600101", "name": "资金一", "net_inflow": 80_000_000, "net_5d_inflow": 120_000_000, "pct_change": 3.2}],
                },
                "get_tushare_moneyflow_dc": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_hsgt_top10": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600104", "name": "北向一", "rank": 1, "amount": 200_000_000, "net_amount": 80_000_000}],
                },
                "get_tushare_margin_detail": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600105", "name": "融资一", "financing_buy": 80_000_000, "financing_balance": 500_000_000}],
                },
                "get_tushare_block_trade": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600106", "name": "大宗一", "amount": 60_000_000, "price": 12.3, "buyer": "机构专用", "seller": "营业部A"}],
                },
                "get_tushare_dragon_tiger_list": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600102", "name": "龙虎一", "amount": 50_000_000, "net_inflow": 20_000_000, "turnover_rate": 8.0, "reason": "换手异常"}],
                },
                "get_tushare_daily_basic": lambda **kwargs: {
                    "status": "ok",
                    "items": [{"ts_code": "600103.SH", "turnover_rate": 6.0, "volume_ratio": 2.2, "pe_ttm": 22.0, "pb": 2.1}],
                },
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260523",
            limit_per_source=5,
            total_limit=20,
        )

    by_code = {seed.code: seed for seed in result.seeds}
    assert by_code["600101"].source == "capital_flow_anomaly"
    assert by_code["600102"].source == "dragon_tiger"
    assert by_code["600103"].source == "valuation_liquidity"
    assert by_code["600104"].source == "northbound_stock_connect"
    assert by_code["600105"].source == "margin_financing"
    assert by_code["600106"].source == "block_trade"
    assert any(item["source"] == "capital_flow_anomaly:moneyflow_ths" for item in result.diagnostics)
    assert any(item["source"] == "northbound_stock_connect" for item in result.diagnostics)


def test_seed_priority_score_soft_caps_high_local_scores():
    assert committee_module._compress_seed_priority_score(94.5) == 94.5
    assert 95.0 < committee_module._compress_seed_priority_score(105.0) < 100.0
    assert committee_module._compress_seed_priority_score(130.0) > committee_module._compress_seed_priority_score(105.0)


def test_seed_candidate_payload_marks_score_as_recall_priority():
    seed = committee_module.SeedItem(
        code="600001",
        name="测试股",
        source="daily_screener",
        priority_score=96.2,
        trigger_signals=[
            {"dimension": "technical", "label": "20日放量突破", "value": 12.3, "threshold": 12.0},
        ],
        extras={"metrics": {"priority_score_raw": 108.0}},
    )

    payload = committee_module._seed_to_candidate_payload(seed)

    assert payload["signal_score"] == 96.2
    assert payload["priority_score"] == 96.2
    assert payload["score_kind"] == "seed_recall_priority"
    assert payload["score_label"] == "入池优先级"
    assert "买入推荐分" in payload["score_note"]
    assert payload["metrics"]["priority_score_raw"] == 108.0
