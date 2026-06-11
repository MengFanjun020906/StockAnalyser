# -*- coding: utf-8 -*-
"""Unit tests for P4 aggregator (aggregator.py) and desk layer (experts/desk_base.py + desks).

All tests are offline (no network, no LLM, no SQLite).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.agent.candidate_experts_v2.aggregator import (
    _compute_confidence,
    _detect_conflicts,
    _parse_allocation,
    _select_primary_desk,
    aggregate_desk_picks,
    allocate_slots,
)
from src.agent.candidate_experts_v2.schemas import (
    AggregatedCandidate,
    AggregatedPool,
    EvidenceItem,
    ExpertCandidateV2,
    ExpertPacketV2,
    FactSheet,
    FeatureFlag,
    FeatureRow,
    RiskNote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_packet(
    expert: str,
    candidates: List[Dict[str, Any]],
    status: str = "ok",
) -> ExpertPacketV2:
    cands = [
        ExpertCandidateV2(
            code=c["code"],
            name=c.get("name", ""),
            stance=c.get("stance", "support"),
            setup_type=c.get("setup_type", "unknown"),
            reason=c.get("reason", ""),
            evidence=[
                EvidenceItem(tool=ev["tool"], summary=ev.get("summary", "ok"))
                for ev in c.get("evidence", [])
            ],
        )
        for c in candidates
    ]
    return ExpertPacketV2(
        expert=expert,
        dimension=expert,
        status=status,
        candidates=cands,
    )


def _make_row(
    code: str,
    *,
    fact_sheet: Optional[FactSheet] = None,
    recall_sources: Optional[List[str]] = None,
    flags: Optional[List[FeatureFlag]] = None,
) -> FeatureRow:
    return FeatureRow(
        code=code,
        name=f"N{code}",
        market="cn",
        fact_sheet=fact_sheet,
        recall_sources=recall_sources or [],
        flags=flags or [],
    )


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    def test_full_coverage_early_turn_desk(self):
        evidence = [
            EvidenceItem(tool="analyze_price_structure", summary="ok"),
            EvidenceItem(tool="get_volume_analysis", summary="ok"),
            EvidenceItem(tool="analyze_trend", summary="ok"),
            EvidenceItem(tool="get_capital_flow", summary="ok"),
        ]
        conf = _compute_confidence("early_turn_desk", evidence)
        assert conf == 1.0

    def test_half_coverage(self):
        evidence = [
            EvidenceItem(tool="analyze_price_structure", summary="ok"),
            EvidenceItem(tool="get_volume_analysis", summary="ok"),
        ]
        conf = _compute_confidence("early_turn_desk", evidence)
        assert conf == 0.5

    def test_quality_desk_full_coverage(self):
        evidence = [
            EvidenceItem(tool="get_tushare_financial_indicators", summary="ok"),
            EvidenceItem(tool="get_tushare_daily_basic", summary="ok"),
            EvidenceItem(tool="analyze_price_structure", summary="ok"),
        ]
        conf = _compute_confidence("quality_repair_desk", evidence)
        assert conf == 1.0

    def test_theme_catalyst_desk_full_coverage(self):
        evidence = [
            EvidenceItem(tool="get_eastmoney_cjzc_daily", summary="ok"),
            EvidenceItem(tool="get_stock_business_context", summary="ok"),
            EvidenceItem(tool="get_stockapi_hot_sectors", summary="ok"),
            EvidenceItem(tool="get_capital_flow", summary="ok"),
            EvidenceItem(tool="analyze_price_structure", summary="ok"),
        ]
        conf = _compute_confidence("theme_catalyst_desk", evidence)
        assert conf == 1.0

    def test_empty_evidence_returns_zero(self):
        assert _compute_confidence("early_turn_desk", []) == 0.0

    def test_same_dim_multiple_tools_count_once(self):
        # Both moneyflow_ths and moneyflow_dc cover "capital" dim → still 1 coverage
        evidence = [
            EvidenceItem(tool="get_tushare_moneyflow_ths", summary="ok"),
            EvidenceItem(tool="get_tushare_moneyflow_dc", summary="ok"),
            EvidenceItem(tool="analyze_trend", summary="ok"),
        ]
        # momentum_desk: 4 dims; covered: capital + trend = 2/4 = 0.5
        conf = _compute_confidence("momentum_desk", evidence)
        assert conf == 0.5

    def test_unknown_desk_heuristic(self):
        evidence = [
            EvidenceItem(tool="tool_a", summary="ok"),
            EvidenceItem(tool="tool_b", summary="ok"),
        ]
        conf = _compute_confidence("unknown_desk", evidence)
        assert 0.0 < conf < 1.0

    def test_empty_summary_does_not_count(self):
        evidence = [
            EvidenceItem(tool="analyze_price_structure", summary=""),  # empty summary
            EvidenceItem(tool="get_volume_analysis", summary="ok"),
        ]
        # analyze_price_structure has empty summary → not counted
        conf = _compute_confidence("early_turn_desk", evidence)
        assert conf == 0.25  # only volume covered out of 4 dims


# ---------------------------------------------------------------------------
# _detect_conflicts
# ---------------------------------------------------------------------------

class TestDetectConflicts:
    def test_capital_thesis_mismatch(self):
        fs = FactSheet(code="A", capital_direction="outflow")
        flags = _detect_conflicts("A", "momentum_desk", {}, fs, "capital_momentum")
        assert "capital_thesis_mismatch" in flags

    def test_no_mismatch_when_inflow(self):
        fs = FactSheet(code="A", capital_direction="inflow")
        flags = _detect_conflicts("A", "momentum_desk", {}, fs, "capital_momentum")
        assert "capital_thesis_mismatch" not in flags

    def test_thesis_position_mismatch_high_range(self):
        fs = FactSheet(code="A", range_pct_120=0.85)
        flags = _detect_conflicts("A", "early_turn_desk", {}, fs, "early_turn")
        assert "thesis_position_mismatch" in flags

    def test_no_position_mismatch_low_range(self):
        fs = FactSheet(code="A", range_pct_120=0.3)
        flags = _detect_conflicts("A", "early_turn_desk", {}, fs, "early_turn")
        assert "thesis_position_mismatch" not in flags

    def test_trend_thesis_mismatch(self):
        fs = FactSheet(code="A", trend_state="bearish")
        flags = _detect_conflicts("A", "momentum_desk", {}, fs, "trend_continuation")
        assert "trend_thesis_mismatch" in flags

    def test_quality_price_mismatch(self):
        fs = FactSheet(code="A", gain_5d=35.0)
        flags = _detect_conflicts("A", "quality_repair_desk", {}, fs, "quality_repair")
        assert "quality_price_mismatch" in flags

    def test_theme_catalyst_outflow_and_chase_risk(self):
        fs = FactSheet(code="A", capital_direction="outflow", gain_5d=35.0)
        flags = _detect_conflicts("A", "theme_catalyst_desk", {}, fs, "theme_catalyst")
        assert "theme_without_capital_validation" in flags
        assert "theme_chase_risk" in flags

    def test_no_conflicts_no_factsheet(self):
        flags = _detect_conflicts("A", "early_turn_desk", {}, None, "early_turn")
        assert flags == []


# ---------------------------------------------------------------------------
# _select_primary_desk
# ---------------------------------------------------------------------------

class TestSelectPrimaryDesk:
    def test_single_desk(self):
        assert _select_primary_desk(["early_turn_desk"], {"early_turn_desk": "support"}, {}) == "early_turn_desk"

    def test_support_beats_watch(self):
        desks = ["momentum_desk", "early_turn_desk"]
        stance = {"momentum_desk": "watch", "early_turn_desk": "support"}
        result = _select_primary_desk(desks, stance, {})
        assert result == "early_turn_desk"

    def test_priority_order_breaks_tie(self):
        desks = ["momentum_desk", "early_turn_desk"]
        stance = {"momentum_desk": "support", "early_turn_desk": "support"}
        result = _select_primary_desk(desks, stance, {})
        # Trend/pattern continuation is the main seat when evidence ties.
        assert result == "momentum_desk"

    def test_theme_catalyst_priority_wins_same_stance(self):
        desks = ["momentum_desk", "theme_catalyst_desk"]
        stance = {"momentum_desk": "support", "theme_catalyst_desk": "support"}
        result = _select_primary_desk(desks, stance, {})
        assert result == "theme_catalyst_desk"

    def test_empty_returns_unknown(self):
        assert _select_primary_desk([], {}, {}) == "unknown"


# ---------------------------------------------------------------------------
# aggregate_desk_picks
# ---------------------------------------------------------------------------

class TestAggregateDeskPicks:
    def test_single_desk_single_pick(self):
        packet = _make_packet(
            "early_turn_desk",
            [{"code": "600519", "stance": "support", "setup_type": "early_turn",
              "evidence": [{"tool": "analyze_price_structure", "summary": "ok"}]}],
        )
        row = _make_row("600519")
        pool = aggregate_desk_picks([packet], [row])
        assert "early_turn_desk" in pool.by_desk
        assert len(pool.by_desk["early_turn_desk"]) == 1
        cand = pool.by_desk["early_turn_desk"][0]
        assert cand.code == "600519"
        assert cand.primary_desk == "early_turn_desk"

    def test_multi_desk_conviction(self):
        p1 = _make_packet(
            "early_turn_desk",
            [{"code": "000001", "stance": "support",
              "evidence": [{"tool": "analyze_price_structure", "summary": "ok"}]}],
        )
        p2 = _make_packet(
            "quality_repair_desk",
            [{"code": "000001", "stance": "support",
              "evidence": [{"tool": "get_tushare_daily_basic", "summary": "ok"}]}],
        )
        row = _make_row("000001")
        pool = aggregate_desk_picks([p1, p2], [row])
        # Find by any desk
        all_cands = [c for cs in pool.by_desk.values() for c in cs]
        cand = next(c for c in all_cands if c.code == "000001")
        assert cand.multi_desk_conviction is True
        assert len(cand.desks) == 2

    def test_unpicked_row_goes_to_observe(self):
        packet = _make_packet("early_turn_desk", [])
        row = _make_row("999999")
        pool = aggregate_desk_picks([packet], [row])
        assert any(o.code == "999999" for o in pool.observe)

    def test_watch_stance_included(self):
        packet = _make_packet(
            "momentum_desk",
            [{"code": "300001", "stance": "watch",
              "evidence": [{"tool": "analyze_trend", "summary": "ok"}]}],
        )
        row = _make_row("300001")
        pool = aggregate_desk_picks([packet], [row])
        assert any(c.code == "300001" for cs in pool.by_desk.values() for c in cs)

    def test_oppose_stance_excluded(self):
        packet = _make_packet(
            "momentum_desk",
            [{"code": "300001", "stance": "oppose",
              "evidence": [{"tool": "analyze_trend", "summary": "ok"}]}],
        )
        row = _make_row("300001")
        pool = aggregate_desk_picks([packet], [row])
        picked = [c for cs in pool.by_desk.values() for c in cs]
        assert not any(c.code == "300001" for c in picked)

    def test_confidence_computed_not_from_llm(self):
        # Even if LLM emitted score=99, confidence is from tool coverage rate
        packet = _make_packet(
            "early_turn_desk",
            [{"code": "600519", "stance": "support",
              "evidence": [{"tool": "analyze_price_structure", "summary": "ok"}]}],
        )
        row = _make_row("600519")
        pool = aggregate_desk_picks([packet], [row])
        cand = pool.by_desk["early_turn_desk"][0]
        # 1 dim covered out of 4 → 0.25
        assert cand.confidence == 0.25

    def test_fact_sheet_attached(self):
        fs = FactSheet(code="600519", trend_state="bullish")
        packet = _make_packet(
            "momentum_desk",
            [{"code": "600519", "stance": "support",
              "evidence": [{"tool": "analyze_trend", "summary": "ok"}]}],
        )
        row = _make_row("600519", fact_sheet=fs)
        pool = aggregate_desk_picks([packet], [row])
        cand = pool.by_desk["momentum_desk"][0]
        assert cand.fact_sheet is not None
        assert cand.fact_sheet.trend_state == "bullish"

    def test_conflict_flag_detected(self):
        fs = FactSheet(code="C", capital_direction="outflow")
        packet = _make_packet(
            "momentum_desk",
            [{"code": "C", "stance": "support", "setup_type": "capital_momentum",
              "evidence": [{"tool": "get_tushare_moneyflow_ths", "summary": "ok"}]}],
        )
        row = _make_row("C", fact_sheet=fs)
        pool = aggregate_desk_picks([packet], [row])
        cand = pool.by_desk["momentum_desk"][0]
        assert "capital_thesis_mismatch" in cand.conflict_flags

    def test_desk_sorted_by_multi_conviction_then_confidence(self):
        packet = _make_packet(
            "early_turn_desk",
            [
                {"code": "A", "stance": "support",
                 "evidence": [{"tool": "analyze_price_structure", "summary": "ok"}]},
                {"code": "B", "stance": "watch",
                 "evidence": [{"tool": "analyze_price_structure", "summary": "ok"},
                               {"tool": "get_volume_analysis", "summary": "ok"},
                               {"tool": "analyze_trend", "summary": "ok"},
                               {"tool": "get_capital_flow", "summary": "ok"}]},
            ],
        )
        rows = [_make_row("A"), _make_row("B")]
        pool = aggregate_desk_picks([packet], rows)
        cands = pool.by_desk["early_turn_desk"]
        # B has higher confidence (4/4=1.0 vs 1/4=0.25) → B first
        assert cands[0].code == "B"


# ---------------------------------------------------------------------------
# Hash-safety regression: dict smuggled into a hashed field must not crash
# ---------------------------------------------------------------------------

class TestHashSafetyRegression:
    """Regression for the silent `unhashable type: 'dict'` aggregation crash.

    Upstream LLM/schema drift can land a dict into a field the aggregator
    hashes (stance/code/tool). Normal construction is blocked by pydantic
    validation, so we use model_construct to bypass it and reproduce the
    raw runtime payload. aggregate_desk_picks + allocate_slots must coerce
    via str() and never raise.
    """

    def test_dict_stance_does_not_crash(self):
        bad = ExpertCandidateV2.model_construct(
            code="600519",
            name="N",
            stance={"label": "support"},  # dict smuggled into hashed field
            setup_type="early_turn",
            reason="",
            evidence=[EvidenceItem(tool="analyze_price_structure", summary="ok")],
            risks=[],
        )
        packet = ExpertPacketV2.model_construct(
            expert="early_turn_desk",
            dimension="early_turn",
            status="ok",
            candidates=[bad],
            rejected=[],
        )
        row = _make_row("600519")
        # Must not raise
        pool = aggregate_desk_picks([packet], [row])
        result = allocate_slots(pool, "range_bound", total=8)
        assert isinstance(pool, AggregatedPool)
        assert isinstance(result, list)

    def test_dict_tool_in_evidence_does_not_crash(self):
        ev = EvidenceItem.model_construct(tool={"name": "analyze_trend"}, summary="ok")
        cand = ExpertCandidateV2.model_construct(
            code="000001",
            name="N",
            stance="support",
            setup_type="early_turn",
            reason="",
            evidence=[ev],
            risks=[],
        )
        packet = ExpertPacketV2.model_construct(
            expert="early_turn_desk",
            dimension="early_turn",
            status="ok",
            candidates=[cand],
            rejected=[],
        )
        row = _make_row("000001")
        pool = aggregate_desk_picks([packet], [row])
        result = allocate_slots(pool, "trending_up", total=8)
        assert isinstance(result, list)

    def test_valid_picks_still_tagged_after_coercion(self):
        # Mixed batch: one clean support pick + one dict-stance reject.
        good = ExpertCandidateV2(
            code="300001",
            name="N",
            stance="support",
            setup_type="early_turn",
            evidence=[EvidenceItem(tool="analyze_price_structure", summary="ok")],
        )
        bad = ExpertCandidateV2.model_construct(
            code="300002",
            name="N",
            stance={"x": 1},
            setup_type="early_turn",
            reason="",
            evidence=[],
            risks=[],
        )
        packet = ExpertPacketV2.model_construct(
            expert="early_turn_desk",
            dimension="early_turn",
            status="ok",
            candidates=[good, bad],
            rejected=[],
        )
        rows = [_make_row("300001"), _make_row("300002")]
        pool = aggregate_desk_picks([packet], rows)
        picked = [c for cs in pool.by_desk.values() for c in cs]
        # Clean pick survives with desk tag; dict-stance pick is filtered out.
        assert any(c.code == "300001" and c.primary_desk == "early_turn_desk" for c in picked)
        assert not any(c.code == "300002" for c in picked)


# ---------------------------------------------------------------------------
# allocate_slots
# ---------------------------------------------------------------------------

class TestAllocateSlots:
    def _make_pool(self, by_desk: Dict[str, List[str]]) -> AggregatedPool:
        bd: Dict[str, List[AggregatedCandidate]] = {}
        for desk, codes in by_desk.items():
            bd[desk] = [
                AggregatedCandidate(code=c, name=c, primary_desk=desk, confidence=0.5)
                for c in codes
            ]
        return AggregatedPool(by_desk=bd)

    def test_basic_allocation(self):
        pool = self._make_pool({
            "early_turn_desk": ["A", "B", "C", "D"],
            "momentum_desk": ["E", "F", "G"],
            "quality_repair_desk": ["H", "I"],
        })
        result = allocate_slots(pool, "range_bound", total=8)
        codes = [c.code for c in result]
        # range_bound: early=4, momentum=2, quality=2
        assert len(result) <= 8
        # should pick from all desks
        assert any(c in codes for c in ["A", "B"])
        assert any(c in codes for c in ["E", "F"])

    def test_defensive_regime_no_momentum(self):
        pool = self._make_pool({
            "early_turn_desk": ["A", "B", "C"],
            "momentum_desk": ["E", "F"],
            "quality_repair_desk": ["H", "I", "J", "K", "L"],
        })
        for regime in ["risk_off", "panic", "trending_down"]:
            result = allocate_slots(pool, regime, total=8)
            codes = [c.code for c in result]
            assert "E" not in codes
            assert "F" not in codes

    def test_no_duplicates(self):
        pool = self._make_pool({
            "early_turn_desk": ["A"],
            "momentum_desk": ["A"],  # same code in two desks
            "quality_repair_desk": ["B"],
        })
        result = allocate_slots(pool, "trending_up", total=8)
        codes = [c.code for c in result]
        assert len(codes) == len(set(codes))

    def test_total_cap_respected(self):
        pool = self._make_pool({
            "early_turn_desk": ["A", "B", "C", "D", "E"],
            "momentum_desk": ["F", "G", "H", "I", "J"],
            "quality_repair_desk": ["K", "L", "M", "N", "O"],
        })
        result = allocate_slots(pool, "trending_up", total=5)
        assert len(result) <= 5

    def test_custom_allocation_json(self):
        pool = self._make_pool({
            "early_turn_desk": ["A", "B"],
            "momentum_desk": ["E", "F"],
            "quality_repair_desk": ["H", "I"],
        })
        json_str = '{"trending_up": {"early_turn_desk": 1, "momentum_desk": 1, "quality_repair_desk": 1}}'
        result = allocate_slots(pool, "trending_up", total=3, allocation_json=json_str)
        assert len(result) == 3

    def test_invalid_allocation_json_falls_back(self):
        pool = self._make_pool({
            "early_turn_desk": ["A", "B"],
            "momentum_desk": ["E"],
        })
        result = allocate_slots(pool, "unknown", total=8, allocation_json="not_json")
        # should not raise; uses default
        assert isinstance(result, list)

    def test_empty_pool_returns_empty(self):
        pool = AggregatedPool()
        result = allocate_slots(pool, "range_bound", total=8)
        assert result == []


# ---------------------------------------------------------------------------
# _parse_allocation
# ---------------------------------------------------------------------------

class TestParseAllocation:
    def test_valid_json(self):
        js = '{"trending_up": {"early_turn_desk": 2, "momentum_desk": 4}}'
        result = _parse_allocation(js, "trending_up")
        assert result["early_turn_desk"] == 2
        assert result["momentum_desk"] == 4

    def test_missing_regime_falls_back_to_unknown(self):
        js = '{"unknown": {"early_turn_desk": 3}}'
        result = _parse_allocation(js, "range_bound")
        assert result["early_turn_desk"] == 3

    def test_none_returns_default(self):
        result = _parse_allocation(None, "risk_off")
        assert result["momentum_desk"] == 0
        assert result["quality_repair_desk"] == 4
        assert result["theme_catalyst_desk"] == 3
        assert result["early_turn_desk"] == 1

    def test_invalid_json_returns_default(self):
        result = _parse_allocation("{invalid}", "trending_up")
        assert isinstance(result, dict)
        assert "early_turn_desk" in result


# ---------------------------------------------------------------------------
# Desk eligibility (EarlyTurnDeskExpert)
# ---------------------------------------------------------------------------

class TestEarlyTurnDeskEligibility:
    def _make_expert(self):
        from src.agent.candidate_experts_v2.experts.early_turn_desk import EarlyTurnDeskExpert
        return EarlyTurnDeskExpert(
            tool_registry={},
            tool_decls=[],
            llm=MagicMock(),
        )

    def test_low_range_pct_without_turn_evidence_excluded(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", range_pct_120=0.2))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 0

    def test_low_range_pct_with_turn_evidence_included(self):
        expert = self._make_expert()
        rows = [
            _make_row(
                "A",
                flags=[FeatureFlag(detector="pattern:breakout", kind="pattern")],
                fact_sheet=FactSheet(code="A", range_pct_120=0.2),
            )
        ]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_high_range_pct_not_primary(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", range_pct_120=0.9))]
        filtered = expert._filter_eligible_rows(rows)
        # Should go to fallback, not primary
        assert len(filtered) == 0  # no primary, no fallback (no None range)

    def test_low_base_source_requires_low_range(self):
        expert = self._make_expert()
        rows = [_make_row("A", recall_sources=["low_base_structure"],
                          fact_sheet=FactSheet(code="A", range_pct_120=0.9))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 0

    def test_missing_range_pct_excluded(self):
        expert = self._make_expert()
        rows = [_make_row("A")]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 0

    def test_low_range_low_base_source_included(self):
        expert = self._make_expert()
        rows = [
            _make_row(
                "A",
                recall_sources=["low_base_structure"],
                fact_sheet=FactSheet(code="A", range_pct_120=0.2),
            )
        ]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Desk eligibility (MomentumDeskExpert)
# ---------------------------------------------------------------------------

class TestMomentumDeskEligibility:
    def _make_expert(self):
        from src.agent.candidate_experts_v2.experts.momentum_desk import MomentumDeskExpert
        return MomentumDeskExpert(
            tool_registry={},
            tool_decls=[],
            llm=MagicMock(),
        )

    def test_limit_flag_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", flags=[FeatureFlag(detector="limit_up", kind="limit")])]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_main_strategy_sources_included(self):
        expert = self._make_expert()
        rows = [
            _make_row("A", recall_sources=["sequoia"]),
            _make_row("B", recall_sources=["alphasift"]),
            _make_row("C", recall_sources=["limit_up_pool"]),
            _make_row("D", recall_sources=["capital_flow_anomaly"]),
        ]
        filtered = expert._filter_eligible_rows(rows)
        assert [row.code for row in filtered] == ["A", "B", "C", "D"]

    def test_bullish_trend_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", trend_state="bullish"))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_high_volume_ratio_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", volume_ratio=2.0))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_neutral_trend_low_volume_fallback(self):
        expert = self._make_expert()
        expert._fallback_supplement_n = 5
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", trend_state="neutral", volume_ratio=1.0))]
        filtered = expert._filter_eligible_rows(rows)
        # Goes to fallback when primary empty
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Desk eligibility (QualityRepairDeskExpert)
# ---------------------------------------------------------------------------

class TestQualityRepairDeskEligibility:
    def _make_expert(self):
        from src.agent.candidate_experts_v2.experts.quality_repair_desk import QualityRepairDeskExpert
        return QualityRepairDeskExpert(
            tool_registry={},
            tool_decls=[],
            llm=MagicMock(),
        )

    def test_fundamental_flag_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", flags=[FeatureFlag(detector="fundamental:turnaround", kind="fundamental")])]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_fundamental_snapshot_source_no_longer_primary(self):
        expert = self._make_expert()
        rows = [_make_row("A", recall_sources=["fundamental_snapshot"])]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1  # fallback only, because no quality primary matched

    def test_low_position_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", range_pct_120=0.3))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_no_match_goes_to_fallback(self):
        expert = self._make_expert()
        expert._fallback_supplement_n = 3
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", range_pct_120=0.8))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Desk eligibility (ThemeCatalystDeskExpert)
# ---------------------------------------------------------------------------

class TestThemeCatalystDeskEligibility:
    def _make_expert(self):
        from src.agent.candidate_experts_v2.experts.theme_catalyst_desk import ThemeCatalystDeskExpert
        return ThemeCatalystDeskExpert(
            tool_registry={},
            tool_decls=[],
            llm=MagicMock(),
        )

    def test_news_theme_source_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", recall_sources=["news_theme_daily"])]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_sector_flag_included(self):
        expert = self._make_expert()
        rows = [_make_row("A", flags=[FeatureFlag(detector="sector_theme", kind="sector")])]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1

    def test_strong_sector_fallback_when_no_direct_theme(self):
        expert = self._make_expert()
        rows = [_make_row("A", fact_sheet=FactSheet(code="A", sector_strength="strong"))]
        filtered = expert._filter_eligible_rows(rows)
        assert len(filtered) == 1


def test_aggregate_desk_picks_surfaces_failed_desk_errors():
    """Regression: a failed/timeout desk must expose its real error in the
    diagnostics, not silently degrade the pipeline with no visible cause."""
    from src.agent.candidate_experts_v2.aggregator import aggregate_desk_picks
    from src.agent.candidate_experts_v2.schemas import (
        ExpertDataQualityV2,
        ExpertPacketV2,
    )

    packets = [
        ExpertPacketV2(
            expert="early_turn_desk",
            dimension="early_turn",
            status="failed",
            errors=["All LLM models failed. Last error: 'name'"],
        ),
        ExpertPacketV2(
            expert="momentum_desk",
            dimension="momentum",
            status="timeout",
            errors=["momentum_desk timeout after 30.0s"],
            data_quality=ExpertDataQualityV2(warnings=["expert momentum_desk raised TimeoutError"]),
        ),
    ]
    pool = aggregate_desk_picks(packets, [])
    diags = {d["desk"]: d for d in pool.diagnostics}
    assert diags["early_turn_desk"]["errors"] == ["All LLM models failed. Last error: 'name'"]
    assert diags["momentum_desk"]["errors"] == ["momentum_desk timeout after 30.0s"]
    assert diags["momentum_desk"]["warnings"] == ["expert momentum_desk raised TimeoutError"]
