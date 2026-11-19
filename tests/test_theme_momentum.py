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


def test_theme_attribution_traces_leading_sector_without_changing_role():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
        hot_sector_leaders={"status": "partial", "items": [{"code": "300001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "net_inflow": 500_000_000}]},
    )
    seed = SeedItem(
        code="300001",
        name="光模块龙头",
        source="sector_theme",
        hint="CPO 光模块 龙头股",
        priority_score=90,
        freshness="2026-11-16",
        extras={"metrics": {"rank": 1, "bk_name": "CPO 光模块", "bk_code": "BK1001", "source_label": "stockapi_hot_sector_leader", "net_inflow": 500_000_000}},
    )

    profile = classify_seed_theme_profile(seed, snapshot)
    attribution = profile["theme_attribution"]

    assert profile["stock_role"] == "core_leader"
    assert attribution["decision"] == "matched"
    assert attribution["stock_role"] == "core_leader"
    assert attribution["as_of"] == "2026-11-16"
    assert attribution["display_only"] is True
    assert attribution["affects_ranking"] is False
    assert attribution["rule"]["membership_threshold"] == 0.25
    assert any(
        item["factor"] == "alias_match" and item["hit"] and item["weight"] == 0.45
        for item in attribution["contributions"]
    )
    sector = next(item for item in attribution["sources"] if item["source_type"] == "leading_sector")
    assert sector["source_id"] == "BK1001"
    assert sector["label"] == "CPO 光模块"
    assert sector["as_of"] == "2026-11-16"
    assert sector["detail"]["rank"] == 1
    assert any(item["source_type"] == "seed_text" and "CPO" in item["matched_aliases"] for item in attribution["sources"])


def test_theme_attribution_traces_news_theme_and_news_card():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
    )
    seed = SeedItem(
        code="300308",
        name="中际旭创",
        source="news_theme_daily",
        hint="AI服务器/光模块主题映射",
        freshness="2026-11-16",
        trigger_signals=[
            {
                "dimension": "news_event",
                "signal_type": "news_signal_card",
                "label": "光模块订单超预期",
                "value": {
                    "card_id": "card-300308-1",
                    "signal_date": "2026-11-15",
                    "summary_short": "光模块订单超预期",
                    "primary_industries": ["光模块"],
                    "company_direction": "benefit",
                    "signal_score": 82.0,
                    "mapping_confidence": 0.9,
                    "evidence_grade": "confirmed",
                },
            }
        ],
        extras={
            "metrics": {
                "theme": "AI服务器",
                "source_title": "11月16日财经早餐",
                "source_url": "https://finance.eastmoney.com/a/demo.html",
                "evidence_section": "热点题材",
                "matched_keywords": ["光模块"],
                "related_boards": ["光模块"],
            }
        },
    )

    profile = classify_seed_theme_profile(seed, snapshot)
    attribution = profile["theme_attribution"]

    assert profile["stock_role"] == "follower"
    assert attribution["decision"] == "matched"
    news_theme = next(item for item in attribution["sources"] if item["source_type"] == "news_theme")
    assert news_theme["source_id"] == "https://finance.eastmoney.com/a/demo.html"
    assert news_theme["detail"]["source_title"] == "11月16日财经早餐"
    assert news_theme["detail"]["evidence_section"] == "热点题材"
    news_card = next(item for item in attribution["sources"] if item["source_type"] == "news_card")
    assert news_card["source_id"] == "card-300308-1"
    assert news_card["as_of"] == "2026-11-15"
    assert news_card["detail"]["evidence_grade"] == "confirmed"


def test_theme_attribution_explains_unrelated_without_alias_hit():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
    )
    seed = SeedItem(
        code="600000",
        name="银行热榜",
        source="hot_rank",
        hint="银行热榜第1",
        freshness="2026-11-16",
        extras={"metrics": {"rank": 1, "concepts": ["银行"]}},
    )

    profile = classify_seed_theme_profile(seed, snapshot)
    attribution = profile["theme_attribution"]

    assert profile["stock_role"] == "unrelated"
    assert profile["theme_status"] == "unrelated_confirmed"
    assert attribution["decision"] == "unrelated_confirmed"
    assert attribution["confirmation"]["evidence_kind"] == "explicit_other_theme"
    assert attribution["missing_reason"]
    assert "明确他主题标签" in attribution["missing_reason"]
    assert "银行" in attribution["missing_reason"]
    assert attribution["fields_checked"]
    assert attribution["sources"][0]["source_type"] == "seed_source"
    assert attribution["sources"][0]["source_id"] == "hot_rank"
    assert attribution["sources"][0]["as_of"] == "2026-11-16"
    assert not any(item["source_type"] == "seed_text" for item in attribution["sources"])


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
    assert result["profile"]["stock_role"] == "unknown"
    assert result["profile"]["theme_status"] == "unclassified"


def test_theme_status_keeps_unclassified_apart_from_confirmed_unrelated():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
    )
    # 没有别名命中,也没有任何明确主题标签 → 证据不足,不能判为无关。
    blank_seed = SeedItem(
        code="600001",
        name="测试标的",
        source="daily_screener",
        hint="形态突破",
        freshness="2026-11-16",
        extras={"metrics": {}},
    )
    blank_profile = classify_seed_theme_profile(blank_seed, snapshot)
    assert blank_profile["theme_status"] == "unclassified"
    assert blank_profile["stock_role"] == "unknown"
    assert blank_profile["theme_attribution"]["decision"] == "unclassified"
    assert "证据不足" in blank_profile["theme_attribution"]["missing_reason"]
    assert "不等于与主题无关" in blank_profile["theme_attribution"]["missing_reason"]

    # 明确属于另一主题(航运) → 有正证据,才算确认无关。
    shipping_seed = SeedItem(
        code="601919",
        name="中远海控",
        source="hot_rank",
        hint="航运热榜第1",
        freshness="2026-11-16",
        extras={"metrics": {"rank": 1, "concepts": ["航运港口"]}},
    )
    shipping_profile = classify_seed_theme_profile(shipping_seed, snapshot)
    assert shipping_profile["theme_status"] == "unrelated_confirmed"
    assert shipping_profile["stock_role"] == "unrelated"
    assert shipping_profile["theme_attribution"]["confirmation"]["evidence_kind"] == "explicit_other_theme"


def test_news_theme_daily_requires_theme_alias_hit_for_theme_weight():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
    )
    shipping_seed = SeedItem(
        code="601919",
        name="中远海控",
        source="news_theme_daily",
        hint="航运 盘前日报概念映射: 中远海控",
        freshness="2026-11-16",
        extras={
            "metrics": {
                "rank": 1,
                "theme": "航运",
                "related_boards": ["港口航运"],
                "matched_keywords": ["航运", "运价"],
            }
        },
    )
    shipping_profile = classify_seed_theme_profile(shipping_seed, snapshot)
    assert shipping_profile["theme_status"] == "unrelated_confirmed"
    assert shipping_profile["stock_role"] == "unrelated"
    weights = {item["factor"]: item["weight"] for item in shipping_profile["theme_attribution"]["contributions"]}
    assert weights["source_theme_priority"] == 0.0

    ai_seed = SeedItem(
        code="300308",
        name="中际旭创",
        source="news_theme_daily",
        hint="AI服务器 盘前日报概念映射: 中际旭创",
        freshness="2026-11-16",
        extras={"metrics": {"rank": 1, "theme": "AI服务器", "related_boards": ["CPO概念"]}},
    )
    ai_profile = classify_seed_theme_profile(ai_seed, snapshot)
    assert ai_profile["theme_status"] == "matched"
    assert ai_profile["stock_role"] == "follower"
    ai_weights = {item["factor"]: item["weight"] for item in ai_profile["theme_attribution"]["contributions"]}
    assert ai_weights["source_theme_priority"] == 0.25


def test_exclusion_rule_confirms_unrelated_with_higher_severity():
    snapshot = build_theme_momentum_snapshot(
        hot_sectors={"status": "partial", "sectors": [{"bk_name": "CPO 光模块", "rank": 1, "net_inflow": 1_000_000_000, "strength": 90}]},
    )
    seed = SeedItem(
        code="600002",
        name="测试标的",
        source="hot_rank",
        hint="热榜第1",
        freshness="2026-11-16",
        extras={"metrics": {"rank": 1, "theme_relation": "opposite"}},
    )
    profile = classify_seed_theme_profile(seed, snapshot)
    assert profile["theme_status"] == "unrelated_confirmed"
    assert profile["theme_confirmation"]["evidence_kind"] == "exclusion_rule"
    assert profile["theme_confirmation"]["severity"] == 0.85
