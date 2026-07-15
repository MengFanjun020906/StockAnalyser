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
import time
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except Exception:
    sys.modules["litellm"] = MagicMock()

from src.agent.candidate_experts_v2 import committee as committee_module
from src.agent.candidate_experts_v2.committee import (
    _assemble_seed_pool,
    _build_seed_pool,
    _build_seed_pool_result,
    _market_regime_key,
    run_committee_discovery,
    run_thesis_desk_committee,
)
from src.agent.candidate_experts_v2.runtime import run_experts_parallel
from src.agent.candidate_experts_v2.experts.base import LLMToolCall, LLMTurn
from src.agent.candidate_experts_v2.experts.desk_base import BaseDeskExpert
from src.agent.candidate_experts_v2.experts.theme_catalyst_desk import ThemeCatalystDeskExpert
from src.agent.candidate_experts_v2.schemas import (
    EvidenceItem,
    ExpertCandidateV2,
    ExpertPacketV2,
    FactSheet,
    FeatureFlag,
    FeatureRow,
    RecallResult,
    SeedFactDataQuality,
    SeedFactPacket,
    SeedFactToolResult,
    SeedSummaryV2,
)
from src.agent.candidate_experts_v2.seed_facts import (
    build_seed_fact_packets_parallel,
    compact_seed_fact_packets_for_model,
)


def test_seed_pool_assembly_round_robins_sources_instead_of_starving_late_sources():
    def seed(code: str, source: str, score: float = 80.0):
        return committee_module.SeedItem(code=code, name=code, source=source, priority_score=score)

    seeds_by_source = {
        "daily_screener": [seed(f"60000{i}", "daily_screener", 90 - i) for i in range(5)],
        "news_theme_daily": [seed(f"60010{i}", "news_theme_daily", 90 - i) for i in range(5)],
        "capital_flow_anomaly": [seed(f"60030{i}", "capital_flow_anomaly", 90 - i) for i in range(3)],
        "margin_financing": [seed("600400", "margin_financing", 88)],
        "block_trade": [seed("600500", "block_trade", 87)],
        "dragon_tiger": [seed("600600", "dragon_tiger", 86)],
        "limit_up_pool": [seed("600700", "limit_up_pool", 85)],
        "alphasift": [seed("600800", "alphasift", 84)],
        "sequoia": [seed("600900", "sequoia", 83)],
    }

    result = _assemble_seed_pool(seeds_by_source, total_limit=10, limit_per_source=5)
    sources = [item.source for item in result]

    assert len(result) == 10
    assert set(sources[:3]) == {"daily_screener", "alphasift", "sequoia"}
    assert "low_base_structure" not in sources
    assert "news_theme_daily" in sources
    assert "capital_flow_anomaly" in sources
    assert "limit_up_pool" in sources
    assert "alphasift" in sources
    assert "sequoia" in sources
    assert sources.count("daily_screener") < 5


def test_market_regime_key_accepts_market_regime_payload_dict():
    assert _market_regime_key({"regime": "risk_off", "volatility": "high"}) == "risk_off"
    assert _market_regime_key({"market_regime": "TRENDING_UP"}) == "trending_up"
    assert _market_regime_key({}) == "unknown"


def test_parallel_expert_timeout_records_budget_reason():
    def slow_task():
        time.sleep(0.05)
        return ExpertPacketV2(expert="slow_desk", dimension="desk", status="empty")

    packets = run_experts_parallel(
        {"slow_desk": slow_task},
        per_expert_timeout_s=120.0,
        overall_timeout_s=0.01,
    )

    assert packets[0].status == "timeout"
    assert "overall timeout" in packets[0].errors[0]
    assert packets[0].diagnostics[0]["reason"] == "overall_timeout_exhausted_before_expert_returned"
    assert packets[0].diagnostics[0]["configured_per_expert_timeout_s"] == 120.0


def test_seed_pool_assembly_allocates_total_limit_evenly_across_live_sources():
    def seed(code: str, source: str, score: float = 80.0):
        return committee_module.SeedItem(code=code, name=code, source=source, priority_score=score)

    live_sources = [
        "daily_screener",
        "news_theme_daily",
        "limit_up_pool",
        "capital_flow_anomaly",
        "margin_financing",
        "block_trade",
        "dragon_tiger",
        "valuation_liquidity",
        "alphasift",
        "sequoia",
    ]
    seeds_by_source = {
        source: [seed(f"{index:03d}{offset:03d}", source, 90 - offset) for offset in range(3)]
        for index, source in enumerate(live_sources)
    }

    result = _assemble_seed_pool(seeds_by_source, total_limit=20, limit_per_source=5)
    counts = committee_module._seed_source_counts(result)

    assert len(result) == 20
    assert set(counts) == set(live_sources)
    assert counts["alphasift"] == 3
    assert counts["sequoia"] == 3
    assert max(counts.values()) - min(counts.values()) <= 2


def test_seed_fact_packets_run_seed_tool_tasks_once_and_record_failures():
    calls = []

    def _ok_tool(stock_code):
        calls.append(("ok_tool", stock_code))
        return {"status": "ok", "value": stock_code}

    def _bad_tool(stock_code):
        calls.append(("bad_tool", stock_code))
        raise RuntimeError(f"boom {stock_code}")

    rows = [
        FeatureRow(code="600001", name="测试一", flags=[FeatureFlag(detector="x", summary="x")]),
        FeatureRow(code="600002", name="测试二", flags=[FeatureFlag(detector="y", summary="y")]),
    ]

    packets = build_seed_fact_packets_parallel(
        rows,
        tool_registry={"ok_tool": _ok_tool, "bad_tool": _bad_tool},
        tools=["ok_tool", "bad_tool", "missing_tool"],
        max_workers=4,
        tool_timeout_seconds=3.0,
    )

    assert len(packets) == 2
    assert sorted(calls) == [
        ("bad_tool", "600001"),
        ("bad_tool", "600002"),
        ("ok_tool", "600001"),
        ("ok_tool", "600002"),
    ]
    by_code = {packet.code: packet for packet in packets}
    assert by_code["600001"].facts["ok_tool"].status == "ok"
    assert by_code["600001"].facts["bad_tool"].status == "failed"
    assert by_code["600001"].facts["missing_tool"].status == "missing"
    assert by_code["600001"].data_quality.status == "partial"
    assert by_code["600001"].data_quality.ok_tools == 1
    assert by_code["600001"].data_quality.failed_tools == ["bad_tool"]
    assert by_code["600001"].data_quality.missing_tools == ["missing_tool"]


def test_seed_fact_packets_build_wide_business_context_for_model():
    def _business_context_tool(stock_code):
        return {
            "status": "ok",
            "code": stock_code,
            "name": "风华高科",
            "industry": "电子元件",
            "boards": ["电子元件", "MLCC", "被动元件"],
            "business_summary": "业务归属线索：行业/板块归属为电子元件；相关主题/概念包括MLCC、被动元件。",
            "source": "data_fetcher:get_belong_boards",
            "as_of": "2026-06-02",
        }

    rows = [
        FeatureRow(
            code="000636",
            name="风华高科",
            flags=[
                FeatureFlag(
                    detector="news_momentum",
                    kind="news",
                    summary="MLCC 概念活跃，AI 服务器需求推动高端被动元件。",
                    metrics={"concept": "MLCC"},
                )
            ],
        )
    ]

    packets = build_seed_fact_packets_parallel(
        rows,
        tool_registry={"get_stock_business_context": _business_context_tool},
        tools=["get_stock_business_context"],
        max_workers=1,
        tool_timeout_seconds=3.0,
    )

    context = packets[0].business_context
    assert context["status"] == "ok"
    assert "电子器件/元器件" in context["broad_industries"]
    assert [item["name"] for item in context["board_names"][:3]] == ["电子元件", "MLCC", "被动元件"]
    assert any("MLCC 概念活跃" in clue["evidence"] for clue in context["theme_clues"])

    compact = compact_seed_fact_packets_for_model(packets, limit=1)[0]
    assert "business_context" in compact
    assert "电子器件/元器件" in compact["business_context"]["broad_industries"]
    assert compact["business_context"]["board_names"][1]["name"] == "MLCC"
    assert "data" not in compact["facts"]["get_stock_business_context"]


def test_desk_prompt_includes_seed_fact_packet_before_tool_calls():
    row = FeatureRow(
        code="600001",
        name="测试一",
        flags=[FeatureFlag(detector="low_base:range_low", kind="position", summary="低位")],
        fact_sheet=FactSheet(code="600001", range_pct_120=0.2),
    )
    row.seed_fact = build_seed_fact_packets_parallel(
        [row],
        tool_registry={"analyze_trend": lambda stock_code: {"status": "ok", "trend": "neutral", "raw_blob": "x" * 10000}},
        tools=["analyze_trend"],
        max_workers=1,
        tool_timeout_seconds=3.0,
    )[0]
    desk = BaseDeskExpert(
        allowed_tools=["analyze_trend"],
        tool_registry={"analyze_trend": lambda stock_code: {"status": "ok"}},
        tool_decls=[],
        llm=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
        system_prompt="system",
    )
    desk._desk_rows = [row]
    desk._desk_regime = "unknown"

    message = desk._build_user_message([], market="cn")

    assert "SeedFactPacket" in message
    payload_start = message.index("[")
    payload_end = message.index("\n\n请优先读取", payload_start)
    payload = json.loads(message[payload_start:payload_end])
    assert payload[0]["seed_fact"]["facts"]["analyze_trend"]["status"] == "ok"
    assert "data" not in payload[0]["seed_fact"]["facts"]["analyze_trend"]
    assert "raw_blob" not in message


def test_seed_fact_compaction_preserves_decision_facts_not_raw_payload():
    packet = SeedFactPacket(
        code="600001",
        name="事实保真",
        market="cn",
        fact_sheet={
            "range_pct_120": 0.21,
            "bias_ma20": -2.3,
            "volume_ratio": 1.8,
            "rsi14": 42.0,
            "freshness": "local_phase_a",
        },
        facts={
            "analyze_price_structure": SeedFactToolResult(
                status="ok",
                data={
                    "status": "ok",
                    "latest_bar": {"time": "2026-05-29", "close": 10.2, "high": 10.5, "low": 9.8, "volume": 123456},
                    "chan": {
                        "status": "ok",
                        "pen_count": 4,
                        "center_count": 1,
                        "structure_summary": {"latest_pen_direction": "up", "latest_center": {"ZG": 10.5, "ZD": 9.7}},
                        "latest_pens": [{"direction": "up", "amplitude_pct": 6.2, "power": {"macd_area": 1.1}}],
                        "latest_centers": [{"ZG": 10.5, "ZD": 9.7, "width_pct": 8.2}],
                    },
                    "smc": {
                        "status": "ok",
                        "swing_count": 8,
                        "structure_summary": {"bias": "up", "latest_labels": ["HL", "HH"]},
                        "bos": {"status": "bullish", "break_price": 10.2},
                        "choch": {"status": "none"},
                        "latest_swings": [{"type": "low", "price": 9.7}, {"type": "high", "price": 10.5}],
                    },
                    "raw": {"merged_bars": [{"close": 1}] * 1000},
                },
            ),
            "analyze_trend": SeedFactToolResult(
                status="ok",
                data={
                    "trend_status": "弱势多头",
                    "ma_alignment": "MA5>MA10",
                    "trend_strength": 55,
                    "current_price": 10.2,
                    "bias_ma5": 1.2,
                    "bias_ma10": 1.8,
                    "bias_ma20": -2.3,
                    "support_levels": [9.8, 9.5],
                    "resistance_levels": [10.5, 11.0],
                    "macd_status": "金叉初期",
                    "rsi_6": 51.2,
                    "rsi_status": "neutral",
                    "signal_reasons": ["站上MA10"],
                    "risk_factors": ["跌破9.8则失败"],
                },
            ),
            "get_capital_flow": SeedFactToolResult(
                status="ok",
                data={
                    "status": "ok",
                    "main_net_inflow": 12000000,
                    "inflow_5d": 33000000,
                    "inflow_10d": -5000000,
                    "source_chain": [{"provider": "long noisy transport detail"}],
                    "sector_rankings": {
                        "top_inflow_sectors": [{"name": "电力", "main_net_inflow": 100}],
                    },
                },
            ),
            "get_stock_business_context": SeedFactToolResult(
                status="ok",
                data={
                    "status": "ok",
                    "code": "600001",
                    "name": "事实保真",
                    "industry": "电力",
                    "boards": ["电力", "火电"],
                    "business_summary": "业务归属线索：行业/板块归属为电力；相关主题/概念包括火电。",
                    "source": "data_fetcher:get_belong_boards",
                    "as_of": "2026-06-02",
                },
            ),
            "get_stock_info": SeedFactToolResult(
                status="ok",
                data={
                    "status": "ok",
                    "code": "600001",
                    "name": "事实保真",
                    "belong_boards": [{"name": "电力", "code": "BK0428"}],
                    "fundamental_context": {
                        "status": "ok",
                        "coverage": {"valuation": "ok"},
                        "valuation": {"status": "ok", "data": {"pe_ratio": 12.3, "pb_ratio": 1.1}},
                        "growth": {"status": "ok", "data": {"revenue_yoy": 18.2}},
                    },
                },
            ),
        },
        data_quality=SeedFactDataQuality(status="ok", tool_count=5, ok_tools=5),
    )

    compact = compact_seed_fact_packets_for_model([packet], limit=1)[0]
    facts = compact["facts"]

    assert compact["fact_sheet"]["range_pct_120"] == 0.21
    assert facts["analyze_price_structure"]["summary"]["chan"]["structure_summary"]["latest_pen_direction"] == "up"
    assert facts["analyze_price_structure"]["summary"]["smc"]["bos"]["status"] == "bullish"
    assert "raw" not in facts["analyze_price_structure"]["summary"]
    assert facts["analyze_trend"]["summary"]["support_levels"] == [9.8, 9.5]
    assert facts["analyze_trend"]["summary"]["macd_status"] == "金叉初期"
    assert facts["get_capital_flow"]["summary"]["inflow_10d"] == -5000000
    assert "source_chain" not in facts["get_capital_flow"]["summary"]
    assert facts["get_stock_business_context"]["summary"]["boards"] == ["电力", "火电"]
    assert facts["get_stock_info"]["summary"]["fundamental_context"]["valuation"]["data"]["pe_ratio"] == 12.3
    assert facts["get_stock_info"]["summary"]["belong_boards"][0]["name"] == "电力"


def test_thesis_desk_init_failure_preserves_seed_fact_payload():
    seed_result = committee_module.SeedPoolBuildResult(
        seeds=[committee_module.SeedItem(code="600001", name="测试一", source="user_watchlist")],
        total_limit=1,
    )
    result = run_thesis_desk_committee(
        market="cn",
        seed_symbols=[],
        tool_registry={
            "analyze_price_structure": lambda stock_code: {"status": "ok"},
            "analyze_trend": lambda stock_code: {"status": "ok"},
        },
        llm_adapter=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
        seed_pool_result=seed_result,
        seed_fact_tools=["analyze_trend"],
        seed_fact_max_workers=1,
        seed_fact_tool_timeout_seconds=3.0,
    )

    assert result["status"] == "failed"
    assert result["seed_fact_summary"]["total"] == 1
    assert result["seed_fact_packets"][0]["facts"]["analyze_trend"]["status"] == "ok"
    assert "data" not in result["seed_fact_packets"][0]["facts"]["analyze_trend"]
    assert "summary" in result["seed_fact_packets"][0]["facts"]["analyze_trend"]
    assert result["discovery_steps"][0]["source"] == "seed_facts"
    assert result["discovery_steps"][-1]["dimension"] == "desk_init"


def test_thesis_desk_committee_degrades_failed_desk_but_keeps_candidates():
    row = FeatureRow(
        code="301161",
        name="唯万密封",
        flags=[FeatureFlag(detector="alphasift", kind="pattern", summary="趋势候选")],
        fact_sheet=FactSheet(code="301161", trend_state="bullish"),
    )
    momentum_packet = ExpertPacketV2(
        expert="momentum_desk",
        dimension="momentum",
        status="ok",
        seed_summary=SeedSummaryV2(seed_count=1, accepted_count=1),
        candidates=[
            ExpertCandidateV2(
                code="301161",
                name="唯万密封",
                stance="watch",
                setup_type="trend_continuation",
                reason="趋势结构健康但追高风险明确，等待回踩确认。",
                evidence=[EvidenceItem(tool="analyze_trend", summary="均线多头")],
            )
        ],
    )
    quality_packet = ExpertPacketV2(
        expert="quality_repair_desk",
        dimension="quality",
        status="failed",
        seed_summary=SeedSummaryV2(seed_count=1),
        candidates=[],
        errors=["quality_repair_desk seed 688721 timeout after 106.7s"],
    )

    with patch(
        "src.agent.candidate_experts_v2.recall.build_recall_pool",
        return_value=RecallResult(rows=[row], all_rows=[row], total_in=1, total_kept=1),
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.build_seed_fact_packets_parallel",
        return_value=[],
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.summarize_seed_fact_packets",
        return_value={"total": 1, "ok": 0, "partial": 0, "failed": 0, "elapsed_ms": 0},
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.compact_seed_fact_packets_for_model",
        return_value=[],
    ), patch.object(
        committee_module,
        "run_experts_parallel",
        return_value=[
            ExpertPacketV2(expert="early_turn_desk", dimension="early_turn", status="empty"),
            momentum_packet,
            quality_packet,
            ExpertPacketV2(expert="theme_catalyst_desk", dimension="theme_catalyst", status="empty"),
        ],
    ):
        result = run_thesis_desk_committee(
            market="cn",
            seed_symbols=[],
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
            overall_timeout_s=30.0,
        )

    assert result["status"] == "partial"
    assert result["degraded"] is True
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["code"] == "301161"
    assert result["candidates"][0]["stance"] == "watch"
    assert result["candidates"][0]["primary_desk"] == "momentum_desk"
    assert "quality_repair_desk seed 688721 timeout" in result["partial_errors"][0]
    assert result["negative_conclusion_reasons"]
    assert any(item["conclusion"] == "desk_packet_failed" for item in result["negative_conclusion_reasons"])
    assert any(step.get("status") == "partial" and step.get("degraded") is True for step in result["discovery_steps"])


def test_thesis_desk_committee_records_rejection_reason_when_no_final_candidate():
    row = FeatureRow(
        code="600123",
        name="拒绝样本",
        flags=[FeatureFlag(detector="daily_screener", kind="pattern", summary="放量但位置高")],
        fact_sheet=FactSheet(code="600123", trend_state="neutral"),
        recall_sources=["daily_screener"],
    )
    rejected_packet = ExpertPacketV2(
        expert="momentum_desk",
        dimension="momentum",
        status="empty",
        seed_summary=SeedSummaryV2(seed_count=1, rejected_count=1),
        rejected=[
            {
                "code": "600123",
                "name": "拒绝样本",
                "stance": "oppose",
                "reason": "量能放大但趋势未确认，不符合动量席打法。",
            }
        ],
    )

    with patch(
        "src.agent.candidate_experts_v2.recall.build_recall_pool",
        return_value=RecallResult(rows=[row], all_rows=[row], total_in=1, total_kept=1),
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.build_seed_fact_packets_parallel",
        return_value=[],
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.summarize_seed_fact_packets",
        return_value={"total": 1, "ok": 0, "partial": 0, "failed": 0, "elapsed_ms": 0},
    ), patch(
        "src.agent.candidate_experts_v2.seed_facts.compact_seed_fact_packets_for_model",
        return_value=[],
    ), patch.object(
        committee_module,
        "run_experts_parallel",
        return_value=[
            ExpertPacketV2(expert="early_turn_desk", dimension="early_turn", status="empty"),
            rejected_packet,
            ExpertPacketV2(expert="quality_repair_desk", dimension="quality", status="empty"),
            ExpertPacketV2(expert="theme_catalyst_desk", dimension="theme_catalyst", status="empty"),
        ],
    ):
        result = run_thesis_desk_committee(
            market="cn",
            seed_symbols=[],
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
            overall_timeout_s=30.0,
        )

    reasons = result["negative_conclusion_reasons"]
    assert result["candidate_count"] == 0
    assert any(item["conclusion"] == "rejected_by_desk" for item in reasons)
    assert any("不符合动量席打法" in item["reason"] for item in reasons)
    assert any(item["conclusion"] == "not_promoted_to_final_candidates" for item in reasons)


def test_desk_filter_explains_why_seed_did_not_enter_desk():
    rows = [
        FeatureRow(
            code="600123",
            name="高位样本",
            flags=[FeatureFlag(detector="daily_screener", kind="unknown", summary="普通异动")],
            fact_sheet=FactSheet(code="600123", range_pct_120=0.80),
            recall_sources=["daily_screener"],
        )
    ]
    desk = BaseDeskExpert(
        allowed_tools=[],
        tool_registry={},
        tool_decls=[],
        llm=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
        system_prompt="test",
    )
    desk.expert_name = "test_filter_desk"

    def no_rows(_rows):
        return []

    desk._filter_eligible_rows = no_rows
    desk._ineligible_row_reason = lambda row: "测试席位未入席：缺少本席位需要的触发条件。"

    packet = desk.run_desk(rows)

    assert packet.status == "empty"
    assert any(
        diag.get("source") == "desk_filter_excluded_seed"
        and diag.get("code") == "600123"
        and "缺少本席位需要的触发条件" in diag.get("reason", "")
        for diag in packet.diagnostics
    )


def test_theme_catalyst_desk_only_accepts_ai_tech_chain_news_rows():
    desk = ThemeCatalystDeskExpert(
        tool_registry={},
        tool_decls=[],
        llm=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
    )
    tech_row = FeatureRow(
        code="000636",
        name="风华高科",
        flags=[
            FeatureFlag(
                detector="news_theme_daily",
                kind="news",
                summary="MLCC 出口和国产替代政策催化，关注电子元件产业链。",
            )
        ],
        fact_sheet=FactSheet(code="000636", sector_name="电子元件", sector_strength="strong"),
        recall_sources=["news_theme_daily"],
    )
    non_tech_row = FeatureRow(
        code="600001",
        name="食品样本",
        flags=[FeatureFlag(detector="sector_theme", kind="sector", summary="食品饮料板块走强")],
        fact_sheet=FactSheet(code="600001", sector_name="食品饮料", sector_strength="strong"),
        recall_sources=["sector_theme"],
    )
    hot_rank_row = FeatureRow(
        code="600002",
        name="普通热榜",
        flags=[FeatureFlag(detector="hot_rank", kind="sector", summary="市场关注度提升")],
        fact_sheet=FactSheet(code="600002", sector_name="基础化工", sector_strength="strong"),
        recall_sources=["hot_rank"],
    )

    eligible = desk._filter_eligible_rows([tech_row, non_tech_row, hot_rank_row])

    assert [row.code for row in eligible] == ["000636"]
    assert "未识别为 AI/科技产业链候选" in desk._ineligible_row_reason(non_tech_row)
    assert "召回来源未命中 news_theme_daily/sector_theme" in desk._ineligible_row_reason(hot_rank_row)


def test_theme_catalyst_tool_result_is_compacted_for_model_context():
    desk = ThemeCatalystDeskExpert(
        tool_registry={},
        tool_decls=[],
        llm=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
    )
    compact = desk._tool_result_for_model(
        "get_eastmoney_cjzc_daily",
        {
            "status": "ok",
            "themes": [
                {
                    "theme": "MLCC",
                    "title": "MLCC 出口改善",
                    "evidence_section": "海外补库带动 MLCC 品类出口改善。",
                    "matched_keywords": ["MLCC", "出口"],
                    "raw_text": "原文正文" * 1000,
                    "ContentBody": "公告正文" * 1000,
                }
            ],
            "items": [{"title": f"新闻{i}", "summary": "国产替代政策验证"} for i in range(10)],
        },
    )

    dumped = json.dumps(compact, ensure_ascii=False)
    assert compact["context_policy"] == "theme_catalyst_summary_card"
    assert "product_export" in compact["evidence_focus"]
    assert "domestic_substitution_policy" in compact["evidence_focus"]
    assert "海外补库带动 MLCC 品类出口改善" in dumped
    assert "原文正文" not in dumped
    assert "raw_text" not in compact["result"]["themes"][0]
    assert "ContentBody" not in compact["result"]["themes"][0]
    assert set(compact["omitted_raw_fields"]) >= {"raw_text", "ContentBody"}
    assert compact["result"]["items"][-1]["omitted_count"] == 4


def test_theme_catalyst_loop_feeds_compacted_tool_result_to_next_llm_turn():
    captured_tool_content = {}

    def llm(messages, tool_decls):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return LLMTurn(
                tool_calls=[
                    LLMToolCall(
                        name="get_eastmoney_cjzc_daily",
                        arguments={"date": "2026-06-28"},
                        call_id="call_theme_news",
                    )
                ]
            )
        captured_tool_content.update(json.loads(tool_messages[-1]["content"]))
        return LLMTurn(text='{"data_quality":{"freshness":"intraday"},"candidates":[],"rejected":[]}')

    with patch("src.agent.candidate_experts_v2.experts.theme_catalyst_desk.validate_manifest"):
        desk = ThemeCatalystDeskExpert(
            tool_registry={
                "get_eastmoney_cjzc_daily": lambda date: {
                    "status": "ok",
                    "themes": [
                        {
                            "theme": "MLCC",
                            "evidence_section": "MLCC 出口改善，国产替代政策继续推进。",
                            "raw_text": "超长新闻正文" * 1000,
                        }
                    ],
                }
            },
            tool_decls=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_eastmoney_cjzc_daily",
                        "parameters": {"type": "object", "properties": {"date": {"type": "string"}}},
                    },
                }
            ],
            llm=llm,
            max_llm_rounds=2,
            max_tool_calls=1,
        )

    packet = desk._run_uncached(
        [committee_module.SeedItem(code="000636", name="风华高科", source="news_theme_daily")],
        market="cn",
    )

    dumped = json.dumps(captured_tool_content, ensure_ascii=False)
    assert packet.status == "empty"
    assert captured_tool_content["context_policy"] == "theme_catalyst_summary_card"
    assert captured_tool_content["result"]["themes"][0]["evidence_section"] == "MLCC 出口改善，国产替代政策继续推进。"
    assert "超长新闻正文" not in dumped
    assert "raw_text" not in captured_tool_content["result"]["themes"][0]


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


def test_run_committee_discovery_seed_summary_uses_build_total_limit():
    seed_result = committee_module.SeedPoolBuildResult(
        seeds=[
            committee_module.SeedItem(code=f"6000{i:02d}", name=f"测试{i}", source="alphasift")
            for i in range(32)
        ],
        total_limit=32,
    )

    with patch.object(
        committee_module,
        "run_thesis_desk_committee",
        return_value={
            "status": "ok",
            "candidate_source": "thesis_desk_committee",
            "candidates": [],
            "candidate_count": 0,
            "recall_total_in": 32,
            "recall_total_kept": 0,
            "thesis_desk_diagnostics": [],
        },
    ):
        result = run_committee_discovery(
            market="cn",
            seed_symbols=[],
            limit=20,
            tool_registry={},
            llm_adapter=lambda messages, tool_decls: LLMTurn(tool_calls=[], text="{}"),
            today="20260523",
            seed_pool_result=seed_result,
        )

    assert result["seed_pool_summary"]["seed_count"] == 32
    assert result["seed_pool_summary"]["total_limit"] == 32
    assert len(result["seed_pool_summary"]["preview"]) == 20


def test_run_committee_discovery_propagates_partial_thesis_desk_candidates():
    seed_result = committee_module.SeedPoolBuildResult(
        seeds=[committee_module.SeedItem(code="301161", name="唯万密封", source="alphasift", priority_score=88)],
        total_limit=1,
    )

    with patch.object(
        committee_module,
        "run_thesis_desk_committee",
        return_value={
            "status": "partial",
            "degraded": True,
            "partial_errors": ["quality_repair_desk seed 688721 timeout after 106.7s"],
            "candidate_source": "thesis_desk_committee",
            "candidates": [
                {
                    "code": "301161",
                    "name": "唯万密封",
                    "stance": "watch",
                    "primary_desk": "momentum_desk",
                    "candidate_source": "thesis_desk_committee",
                }
            ],
            "candidate_count": 1,
            "recall_total_in": 1,
            "recall_total_kept": 1,
            "thesis_desk_diagnostics": [
                {"desk": "quality_repair_desk", "status": "failed", "errors": ["timeout"]},
            ],
            "thesis_desk_committee_elapsed_ms": 123,
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

    assert result["status"] == "partial"
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["code"] == "301161"
    assert result["llm_expert_committee"]["status"] == "partial"
    assert result["llm_expert_committee"]["degraded"] is True
    assert result["thesis_desk_committee"]["status"] == "partial"
    assert result["thesis_desk_committee"]["degraded"] is True
    assert result["thesis_desk_committee"]["partial_errors"] == [
        "quality_repair_desk seed 688721 timeout after 106.7s",
    ]


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


def test_build_seed_pool_excludes_fundamental_snapshot_and_low_base_sources():
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
        side_effect=AssertionError("low_base_structure should not be called by default"),
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
    assert "600001" not in by_code
    assert "600002" not in by_code
    assert all(seed.source != "fundamental_snapshot" for seed in seeds)
    assert all(seed.source != "low_base_structure" for seed in seeds)


def test_build_seed_pool_applies_source_caps_and_weights_strategy_sources():
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
        side_effect=AssertionError("low_base_structure should not be called by default"),
    ):
        seeds = _build_seed_pool(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_limit_list_d": lambda **kwargs: {"items": [{"code": f"6005{i:02d}", "name": f"涨停{i}", "limit_status": "U"} for i in range(20)]},
                "get_stockapi_popularity_rank": lambda **kwargs: {"items": [{"code": f"6006{i:02d}", "name": f"热榜{i}", "rank": i + 1} for i in range(20)]},
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260523",
            limit_per_source=20,
            total_limit=40,
        )

    source_counts = {}
    for seed in seeds:
        source_counts[seed.source] = source_counts.get(seed.source, 0) + 1

    assert "low_base_structure" not in source_counts
    assert source_counts["alphasift"] <= 14
    assert source_counts["sequoia"] <= 12
    assert source_counts["alphasift"] >= source_counts["hot_rank"]
    assert source_counts["sequoia"] >= source_counts["hot_rank"]
    assert source_counts["hot_rank"] <= 8
    assert source_counts["limit_up_pool"] <= 10
    assert "fundamental_snapshot" not in source_counts


def test_build_seed_pool_result_caps_total_limit_to_thirty_two():
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
        side_effect=AssertionError("low_base_structure should not be called by default"),
    ):
        result = committee_module._build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_limit_list_d": lambda **kwargs: {"items": [{"code": f"6005{i:02d}", "name": f"涨停{i}", "limit_status": "U"} for i in range(20)]},
                "get_stockapi_popularity_rank": lambda **kwargs: {"items": [{"code": f"6006{i:02d}", "name": f"热榜{i}", "rank": i + 1} for i in range(20)]},
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260523",
            total_limit=32,
        )

        assert result.total_limit == 32
        assert len(result.seeds) <= 32


def test_build_seed_pool_enriches_existing_seed_with_news_signal_card_evidence():
    service_instance = MagicMock()
    service_instance.seed_evidence_for_codes.return_value = {
        "requested_codes": 1,
        "matched_codes": 1,
        "attached_cards": 1,
        "skipped": {},
        "items_by_code": {
            "600001": [
                {
                    "card_id": "card:news:600001",
                    "summary_short": "订单催化已获得多源确认",
                    "signal_date": "2026-07-10",
                    "signal_layer": "company",
                    "impact_horizon": "medium",
                    "evidence_grade": "confirmed",
                    "inference_level": "explicit",
                    "mapping_confidence": 0.92,
                    "signal_score": 82.0,
                    "company_direction": "benefit",
                    "company_confidence": 0.9,
                    "company_rationale": "公告明确点名公司",
                    "primary_industries": ["AI服务器"],
                    "raw_episode_ids": ["raw:1"],
                    "gate_result": "matched_existing_seed",
                }
            ]
        },
    }

    with patch("src.services.news_signal_service.NewsSignalService", return_value=service_instance):
        result = _build_seed_pool_result(
            market="cn",
            seed_symbols=["600001"],
            tool_registry={},
            today="20260710",
            total_limit=1,
        )

    seed = result.seeds[0]
    signal = next(item for item in seed.trigger_signals if item.get("signal_type") == "news_signal_card")
    assert signal["value"]["card_id"] == "card:news:600001"
    assert signal["value"]["gate_result"] == "matched_existing_seed"
    assert "持久化新闻信号卡" in seed.context_hint
    assert any(item.get("source") == "news_signal_cards" for item in result.diagnostics)


def test_news_signal_evidence_matches_seed_code_with_exchange_suffix():
    service_instance = MagicMock()
    service_instance.seed_evidence_for_codes.return_value = {
        "items_by_code": {
            "600001": [
                {
                    "card_id": "card:news:600001",
                    "summary_short": "后缀代码兼容",
                    "signal_score": 80.0,
                    "mapping_confidence": 0.9,
                    "gate_result": "matched_existing_seed",
                }
            ]
        },
        "skipped": {},
    }
    buckets = {
        "user_watchlist": [
            committee_module.SeedItem(
                code="600001.SH",
                name="测试股票",
                source="user_watchlist",
            )
        ]
    }

    with patch("src.services.news_signal_service.NewsSignalService", return_value=service_instance):
        diagnostic = committee_module._attach_news_signal_card_evidence(
            buckets,
            signal_date="20260710",
        )

    seed = buckets["user_watchlist"][0]
    signal = next(item for item in seed.trigger_signals if item.get("signal_type") == "news_signal_card")
    assert signal["value"]["card_id"] == "card:news:600001"
    assert diagnostic["status"] == "ok"
    service_instance.seed_evidence_for_codes.assert_called_once_with(
        ["600001"],
        signal_date="2026-07-10",
    )


def test_build_seed_pool_attaches_required_knowledge_graph_evidence_to_seed():
    graph_evidence = {
        "required": True,
        "status": "ok",
        "source": "graphiti",
        "degraded": False,
        "by_code": {
            "600001": [
                {
                    "type": "analysis_history",
                    "code": "600001",
                    "analysis_summary": "历史相似情形中订单兑现后趋势延续",
                }
            ]
        },
    }

    result = _build_seed_pool_result(
        market="cn",
        seed_symbols=["600001"],
        tool_registry={},
        today="20260711",
        total_limit=1,
        graph_evidence=graph_evidence,
    )

    seed = result.seeds[0]
    signal = next(item for item in seed.trigger_signals if item.get("signal_type") == "knowledge_graph_evidence")
    assert signal["value"]["source"] == "graphiti"
    assert signal["value"]["items"][0]["analysis_summary"].startswith("历史相似情形")
    assert any(item.get("source") == "knowledge_graph" for item in result.diagnostics)


def test_seed_pool_attaches_theme_momentum_profiles_with_partial_stockapi_sources():
    with patch(
        "src.agent.candidate_providers.fundamental_provider.FundamentalCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.alphasift_provider.AlphaSiftCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch(
        "src.agent.candidate_providers.sequoia_provider.SequoiaCandidateProvider.discover",
        return_value={"status": "empty", "candidates": []},
    ), patch.object(
        committee_module,
        "_build_daily_screener_seeds",
        return_value=([], {"source": "daily_screener", "status": "empty", "count": 0}),
    ), patch.object(
        committee_module,
        "_build_low_base_structure_seeds",
        side_effect=AssertionError("low_base_structure should not be called by default"),
    ):
        result = committee_module._build_seed_pool_result(
            market="cn",
            seed_symbols=[],
            tool_registry={
                "get_tushare_moneyflow_dc": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_margin_detail": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_block_trade": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_dragon_tiger_list": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_daily_basic": lambda **kwargs: {"status": "empty", "items": []},
                "get_tushare_limit_list_d": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260622",
                    "items": [
                        {"code": "600001", "name": "光模块龙头", "limit_status": "U", "concepts": "CPO 光模块", "limit_up_streak": 2, "bomb_num": 0},
                        {"code": "600000", "name": "银行热榜", "limit_status": "U", "concepts": "银行", "limit_up_streak": 1, "bomb_num": 0},
                    ],
                },
                "get_stockapi_popularity_rank": lambda **kwargs: {
                    "status": "partial",
                    "date": "2026-06-22",
                    "items": [
                        {"code": "600001", "name": "光模块龙头", "rank": 1, "concepts": ["CPO", "光模块"]},
                        {"code": "600000", "name": "银行热榜", "rank": 2, "concepts": ["银行"]},
                    ],
                },
                "get_stockapi_hot_sectors": lambda **kwargs: {
                    "status": "failed",
                    "date": "2026-06-22",
                    "sectors": [],
                    "errors": ["stockapi:/v1/hotBkJlrDr:60050:permission"],
                },
                "get_stockapi_hot_sector_leaders": lambda **kwargs: {
                    "status": "partial",
                    "date": "2026-06-22",
                    "items": [
                        {"code": "600001", "name": "光模块龙头", "bk_name": "CPO 光模块", "rank": 1, "net_inflow": 600_000_000, "strength": 90}
                    ],
                },
                "get_stockapi_sector_constituents": lambda **kwargs: {"status": "empty", "items": []},
                "get_eastmoney_cjzc_daily": lambda **kwargs: {"status": "empty", "themes": []},
                "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
            },
            today="20260623",
            limit_per_source=5,
            total_limit=12,
        )

    assert result.theme_momentum["regime"] in {"mainline_markup", "mainline_divergence", "range_rotation"}
    assert result.theme_momentum["source_status"]["hot_sectors"] == "failed"
    assert result.theme_momentum["matched_counts"]["total"] >= 2
    assert any(item["source"] == "theme_momentum_snapshot" for item in result.diagnostics)

    core_profiles = [
        seed.extras.get("theme_profile")
        for seed in result.seeds
        if seed.code == "600001" and isinstance(seed.extras, dict)
    ]
    unrelated_profiles = [
        seed.extras.get("theme_profile")
        for seed in result.seeds
        if seed.code == "600000" and isinstance(seed.extras, dict)
    ]
    assert any(
        profile and profile["stock_role"] in {"core_leader", "core_midcap", "high_beta_leader"}
        for profile in core_profiles
    ), core_profiles
    assert any(profile and profile["stock_role"] == "unrelated" for profile in unrelated_profiles)
    assert any(
        signal.get("dimension") == "theme_regime"
        for seed in result.seeds
        for signal in seed.trigger_signals
    )


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


def test_build_seed_pool_result_exposes_only_sector_auto_supplement_bucket_diagnostics():
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
                "get_stockapi_hot_sectors": lambda **kwargs: {"status": "empty", "sectors": []},
                "get_stockapi_hot_sector_leaders": lambda **kwargs: {"status": "empty", "items": []},
                "get_stockapi_change_all_history": lambda **kwargs: {"status": "failed", "items": [], "errors": ["should_not_be_called"]},
                "discover_watchlist_candidates": lambda **kwargs: {
                    "status": "ok",
                    "candidates": [],
                    "discovery_steps": [
                        {"source": "event_impact", "status": "empty", "count": 0},
                        {"source": "news_momentum", "status": "failed", "count": 0, "error": "request limit"},
                        {"source": "get_sector_rankings", "status": "empty", "sectors": []},
                    ],
                },
            },
            today="20260523",
            limit_per_source=5,
            total_limit=20,
        )

    diagnostics_by_source = {item["source"]: item for item in result.diagnostics}
    assert diagnostics_by_source["sector_theme"]["status"] == "empty"
    assert result.source_quality["sector_theme"]["available"] is True
    assert "event_impact" not in diagnostics_by_source
    assert "news_momentum" not in diagnostics_by_source
    assert "event_impact" not in result.source_quality
    assert "news_momentum" not in result.source_quality


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


def test_build_seed_pool_result_includes_news_theme_daily_concept_mapping():
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
        with patch("src.agent.tools.data_tools._latest_tushare_trade_date", return_value="20260601"):
            result = _build_seed_pool_result(
                market="cn",
                seed_symbols=[],
                tool_registry={
                    "get_stockapi_hot_sectors": lambda **kwargs: {"status": "empty", "sectors": []},
                    "get_stockapi_hot_sector_leaders": lambda **kwargs: {"status": "empty", "items": []},
                    "get_stockapi_change_all_history": lambda **kwargs: {"status": "empty", "items": []},
                    "get_eastmoney_cjzc_daily": lambda **kwargs: {
                        "status": "ok",
                        "target_date": kwargs.get("target_date"),
                        "trade_date": kwargs.get("target_date"),
                        "matched_publish_date": kwargs.get("target_date"),
                        "session": "pre_market_daily",
                        "title": "东方财富财经早餐 6月2日周二",
                        "link": "https://finance.eastmoney.com/a/20260602.html",
                        "themes": [
                            {
                                "theme": "MLCC",
                                "keywords": ["MLCC"],
                                "polarity": "positive",
                                "evidence": "MLCC 涨价，AI服务器需求提升。",
                                "related_boards": ["电子元件", "被动元件"],
                                "mapped_stocks": [
                                    {"code": "000636", "name": "风华高科", "role": "passive_component"},
                                    {"code": "300408", "name": "三环集团", "role": "ceramic_component"},
                                ],
                            }
                        ],
                        "mentioned_stocks": [],
                        "company_events": [{"name": "春秋电子", "polarity": "deny_or_clarification", "seed_allowed": False}],
                        "errors": [],
                    },
                    "discover_watchlist_candidates": lambda **kwargs: {"status": "empty", "candidates": []},
                },
                today="20260602",
                limit_per_source=5,
                total_limit=20,
            )

    by_code = {seed.code: seed for seed in result.seeds}
    assert by_code["000636"].source == "news_theme_daily"
    assert by_code["000636"].trigger_signals[0]["signal_type"] == "eastmoney_cjzc_daily_concept_mapping"
    assert by_code["000636"].extras["metrics"]["theme"] == "MLCC"
    assert by_code["000636"].extras["metrics"]["directness"] == "concept_board"
    diagnostics_by_source = {item["source"]: item for item in result.diagnostics}
    assert diagnostics_by_source["news_theme_daily"]["status"] == "ok"
    assert diagnostics_by_source["news_theme_daily"]["target_date"] == "2026-06-02"
    assert diagnostics_by_source["news_theme_daily"]["mapped_stock_count"] == 2
    assert diagnostics_by_source["news_theme_daily"]["company_event_count"] == 1


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
                    "status": "failed",
                    "items": [],
                    "errors": ["disabled_for_permission_gap"],
                },
                "get_tushare_moneyflow_dc": lambda **kwargs: {
                    "status": "ok",
                    "trade_date": "20260522",
                    "items": [{"code": "600101", "name": "资金一", "net_inflow": 80_000_000, "net_5d_inflow": 120_000_000, "pct_change": 3.2}],
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
                "get_stockapi_hot_sectors": lambda **kwargs: {"status": "empty", "sectors": []},
                "get_stockapi_hot_sector_leaders": lambda **kwargs: {
                    "status": "ok",
                    "date": "2026-05-22",
                    "items": [{"code": "600104", "name": "板块一", "bk_code": "BK1", "bk_name": "机器人", "main_net_inflow": 30_000_000, "strength": 88}],
                },
                "get_stockapi_change_all_history": lambda **kwargs: {
                    "status": "ok",
                    "date": "2026-05-22",
                    "items": [{"code": "600107", "name": "异动一", "event_name": "火箭发射", "event_type": kwargs.get("event_type"), "info": "5%"}],
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
    assert by_code["600104"].source == "sector_theme"
    assert by_code["600105"].source == "margin_financing"
    assert by_code["600106"].source == "block_trade"
    assert "600107" not in by_code
    assert any(item["source"] == "capital_flow_anomaly:moneyflow_dc" for item in result.diagnostics)
    assert not any(item["source"] == "northbound_stock_connect" for item in result.diagnostics)
    assert not any(item["source"] in {"event_impact", "news_momentum"} for item in result.diagnostics)


def test_seed_priority_score_soft_caps_high_local_scores():
    assert committee_module._compress_seed_priority_score(94.5) == 94.5
    assert 95.0 < committee_module._compress_seed_priority_score(105.0) < 100.0
    assert committee_module._compress_seed_priority_score(130.0) > committee_module._compress_seed_priority_score(105.0)


def test_seed_candidate_payload_keeps_recall_score_as_source_diagnostic():
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

    assert "signal_score" not in payload
    assert "score" not in payload
    assert "priority_score" not in payload
    assert payload["metrics"]["source_diagnostics"]["priority_score"] == 96.2
    assert payload["metrics"]["source_diagnostics"]["priority_score_raw"] == 108.0
    assert payload["metrics"]["source_diagnostics"]["score_kind"] == "seed_recall_priority"
