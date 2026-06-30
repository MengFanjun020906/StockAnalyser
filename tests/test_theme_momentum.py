# -*- coding: utf-8 -*-
"""Tests for deterministic mainline theme momentum classification."""

from __future__ import annotations

from src.agent.candidate_experts_v2.schemas import SeedItem
from src.agent.theme_momentum import (
    apply_theme_profile_to_seed,
    build_single_stock_theme_profile,
    build_theme_momentum_snapshot,
    classify_seed_theme_profile,
)


def test_theme_snapshot_stays_unknown_when_evidence_is_missing():
    snapshot = build_theme_momentum_snapshot()

    assert snapshot["regime"] == "unknown"
    assert snapshot["data_quality"] == "insufficient"
    assert snapshot["matched_counts"]["total"] == 0
    assert "不能把 AI 产业链默认为主线" in snapshot["evidence"][0]


def test_theme_snapshot_marks_ai_chain_as_mainline_markup_with_broad_confirmation():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={
            "status": "partial",
            "sectors": [
                {"bk_name": "CPO 光模块", "rank": 1, "return_pct": 5.2, "net_inflow": 1_800_000_000, "strength": 92, "inflow_days": 4},
                {"bk_name": "PCB 服务器", "rank": 3, "return_pct": 3.8, "net_inflow": 900_000_000, "strength": 80, "inflow_days": 3},
            ],
        },
        limit_up_pool={
            "status": "partial",
            "items": [
                {"code": "300001", "name": "光模块龙头", "concepts": "CPO,光模块", "limit_up_streak": 2, "bomb_num": 0, "change_ratio": 20.0},
                {"code": "002001", "name": "PCB中军", "concepts": "PCB,服务器", "limit_up_streak": 1, "bomb_num": 0, "change_ratio": 10.0},
            ],
        },
        hot_sector_leaders={
            "status": "partial",
            "items": [
                {"code": "300001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "change_ratio": 12.0, "net_inflow": 600_000_000},
                {"code": "002001", "name": "PCB中军", "bk_name": "PCB", "rank": 2, "change_ratio": 8.0, "net_inflow": 300_000_000},
            ],
        },
        popularity_rank={
            "status": "partial",
            "items": [{"code": "300001", "name": "光模块龙头", "concepts": ["CPO"], "rank": 1, "hot": 99}],
        },
    )

    assert snapshot["regime"] == "mainline_markup"
    assert snapshot["data_quality"] == "sufficient"
    assert snapshot["scores"]["leader_strength"] > 0.5
    assert snapshot["scores"]["exhaustion_risk"] < 0.58


def test_theme_snapshot_marks_climax_when_limit_pool_is_crowded_and_bombing():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={
            "status": "partial",
            "sectors": [{"bk_name": "AI 算力", "rank": 1, "return_pct": 7.0, "net_inflow": 2_000_000_000, "strength": 95}],
        },
        limit_up_pool={
            "status": "partial",
            "items": [
                {"code": f"30000{i}", "name": f"AI后排{i}", "concepts": "AI算力,CPO", "limit_up_streak": 1, "bomb_num": 3, "turnover_ratio": 24.0, "change_ratio": 20.0}
                for i in range(7)
            ],
        },
        popularity_rank={
            "status": "partial",
            "items": [{"code": "300001", "name": "AI后排1", "concepts": ["AI算力"], "rank": 2, "hot": 90}],
        },
    )

    assert snapshot["regime"] == "climax_extension"
    assert snapshot["scores"]["exhaustion_risk"] >= 0.58
    assert snapshot["limit_up_stats"]["bomb_count"] == 7


def test_seed_theme_profile_distinguishes_core_from_unmatched_hot_rank():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
        hot_sector_leaders={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "net_inflow": 500_000_000}]},
        limit_up_pool={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "concepts": "CPO", "bomb_num": 0, "limit_up_streak": 2}]},
    )
    core_seed = SeedItem(
        code="300001",
        name="光模块龙头",
        source="sector_theme",
        hint="CPO 光模块 龙头股",
        priority_score=90,
        extras={"metrics": {"rank": 1, "bk_name": "CPO 光模块", "source_label": "stockapi_hot_sector_leader", "net_inflow": 500_000_000}},
    )
    unrelated_hot_seed = SeedItem(
        code="600000",
        name="银行热榜",
        source="hot_rank",
        hint="银行热榜第1",
        priority_score=90,
        extras={"metrics": {"rank": 1, "concepts": ["银行"]}},
    )

    core_profile = classify_seed_theme_profile(core_seed, snapshot)
    unrelated_profile = classify_seed_theme_profile(unrelated_hot_seed, snapshot)

    assert core_profile["stock_role"] == "core_leader"
    assert core_profile["overbought_interpretation"] == "strength_requires_confirmation"
    assert core_profile["chase_permission"] == "conditional_only"
    assert unrelated_profile["stock_role"] == "unrelated"
    assert unrelated_profile["chase_permission"] == "none"


def test_apply_theme_profile_adds_auditable_signal_to_seed():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
        limit_up_pool={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "concepts": "CPO 光模块", "bomb_num": 0, "limit_up_streak": 2}]},
    )
    seed = SeedItem(code="300001", name="光模块龙头", source="limit_up_pool", hint="CPO 光模块 涨停", priority_score=88)

    apply_theme_profile_to_seed(seed, snapshot)

    assert seed.extras["theme_profile"]["stock_role"] in {"high_beta_leader", "follower"}
    assert seed.extras["metrics"]["theme_regime"] == snapshot["regime"]
    assert any(signal.get("dimension") == "theme_regime" for signal in seed.trigger_signals)


def test_single_stock_theme_profile_marks_matched_leader_as_core():
    result = build_single_stock_theme_profile(
        symbol="300001",
        name="光模块龙头",
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
        hot_sector_leaders={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "net_inflow": 500_000_000}]},
        limit_up_pool={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "concepts": "CPO", "bomb_num": 0, "limit_up_streak": 2}]},
        popularity_rank={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "concepts": ["CPO"], "rank": 2}]},
    )

    assert result["symbol"] == "300001"
    assert result["matched_sources"]["hot_sector_leaders"] == 1
    assert result["profile"]["stock_role"] == "core_leader"
    assert result["profile"]["chase_permission"] == "conditional_only"
    assert result["theme_momentum"]["data_quality"] in {"limited", "sufficient"}


def test_single_stock_theme_profile_does_not_upgrade_unmatched_hot_rank():
    result = build_single_stock_theme_profile(
        symbol="600000",
        name="银行热榜",
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
        popularity_rank={"status": "partial", "items": [{"code": "600000", "name": "银行热榜", "concepts": ["银行"], "rank": 1}]},
    )

    assert result["matched_sources"]["popularity_rank"] == 1
    assert result["profile"]["stock_role"] == "unrelated"
    assert result["profile"]["chase_permission"] == "none"
    assert any("不能当作主线核心证据" in flag for flag in result["profile"]["risk_flags"])


def test_single_stock_theme_profile_survives_failed_stockapi_payloads():
    result = build_single_stock_theme_profile(
        symbol="300001",
        name="测试股票",
        hot_sectors={"status": "error", "error": "60050 no package permission"},
        hot_sector_leaders={"status": "error", "error": "60050 no package permission"},
        limit_up_pool={"status": "error", "error": "timeout"},
        popularity_rank={"status": "error", "error": "timeout"},
    )

    assert result["status"] == "insufficient"
    assert result["theme_momentum"]["regime"] == "unknown"
    assert result["theme_momentum"]["data_quality"] == "insufficient"
    assert result["profile"]["stock_role"] == "unrelated"
