# -*- coding: utf-8 -*-
"""Unit tests for P3 recall layer (src/agent/candidate_experts_v2/recall.py).

All tests are offline (no network, no SQLite).  The committee seed builder is
mocked so we can control exactly which SeedItems are returned.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.agent.candidate_experts_v2.schemas import (
    FactSheet,
    FeatureFlag,
    FeatureRow,
    RecallResult,
    SeedItem,
)
from src.agent.candidate_experts_v2.recall import (
    _apply_coarse_cap,
    _seed_to_flags,
    _seeds_to_rows,
    build_recall_pool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_seed(
    code: str,
    source: str = "daily_screener",
    *,
    recall_sources: Optional[List[str]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    hint: str = "test hint",
    freshness: str = "2024-01-01",
) -> SeedItem:
    extras: Dict[str, Any] = {}
    if recall_sources:
        extras["recall_sources"] = recall_sources
    if metrics:
        extras["metrics"] = metrics
    return SeedItem(
        code=code,
        name=f"N{code}",
        market="cn",
        source=source,
        hint=hint,
        freshness=freshness,
        extras=extras,
    )


def _fake_pool(seeds: List[SeedItem]) -> Any:
    """Minimal SeedPoolBuildResult stand-in."""
    result = MagicMock()
    result.seeds = seeds
    result.diagnostics = []
    return result


# ---------------------------------------------------------------------------
# FeatureFlag / FeatureRow schema
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_basic_fields(self):
        flag = FeatureFlag(
            detector="moneyflow_ths",
            kind="capital",
            summary="主力净流入2亿",
            metrics={"net_inflow": 2e8, "normalized_rank": 3},
            as_of="2024-01-01",
        )
        assert flag.detector == "moneyflow_ths"
        assert flag.kind == "capital"
        assert flag.metrics["net_inflow"] == 2e8

    def test_kind_default_unknown(self):
        flag = FeatureFlag(detector="custom:whatever")
        assert flag.kind == "unknown"

    def test_extra_fields_allowed(self):
        flag = FeatureFlag(detector="x", extra_field="extra")
        assert flag.extra_field == "extra"  # type: ignore[attr-defined]


class TestFeatureRow:
    def test_defaults(self):
        row = FeatureRow(code="600519")
        assert row.coarse_kept is True
        assert row.flags == []
        assert row.fact_sheet is None

    def test_multiple_flags(self):
        row = FeatureRow(
            code="000001",
            flags=[
                FeatureFlag(detector="screener:ma_breakout", kind="pattern"),
                FeatureFlag(detector="moneyflow_ths", kind="capital"),
            ],
        )
        assert len(row.flags) == 2

    def test_fact_sheet_optional(self):
        fs = FactSheet(code="000001")
        row = FeatureRow(code="000001", fact_sheet=fs)
        assert row.fact_sheet is not None
        assert row.fact_sheet.code == "000001"


# ---------------------------------------------------------------------------
# _seed_to_flags
# ---------------------------------------------------------------------------

class TestSeedToFlags:
    def test_single_source(self):
        seed = _make_seed("600519", source="daily_screener")
        flags = _seed_to_flags(seed)
        assert len(flags) == 1
        assert flags[0].detector == "screener:ma_breakout"
        assert flags[0].kind == "pattern"

    def test_multi_source_recall_sources(self):
        seed = _make_seed(
            "000001",
            source="daily_screener",
            recall_sources=["daily_screener", "alphasift"],
        )
        flags = _seed_to_flags(seed)
        detectors = {f.detector for f in flags}
        assert "screener:ma_breakout" in detectors
        assert "alphasift:high_tight_flag" in detectors
        assert len(detectors) == 2

    def test_capital_flow_api_label_moneyflow_ths(self):
        seed = _make_seed(
            "002271",
            source="capital_flow_anomaly",
            metrics={"api_label": "moneyflow_ths", "net_inflow": 1e8},
        )
        flags = _seed_to_flags(seed)
        assert len(flags) == 1
        assert flags[0].detector == "moneyflow_ths"
        assert flags[0].kind == "capital"
        assert flags[0].metrics["net_inflow"] == 1e8

    def test_capital_flow_api_label_moneyflow_dc(self):
        seed = _make_seed(
            "002271",
            source="capital_flow_anomaly",
            metrics={"api_label": "moneyflow_dc", "net_inflow": 5e7},
        )
        flags = _seed_to_flags(seed)
        assert flags[0].detector == "moneyflow_dc"

    def test_low_base_kind_position(self):
        seed = _make_seed("300750", source="low_base_structure")
        flags = _seed_to_flags(seed)
        assert flags[0].detector == "low_base:range_low"
        assert flags[0].kind == "position"

    def test_fundamental_snapshot(self):
        seed = _make_seed("601318", source="fundamental_snapshot")
        flags = _seed_to_flags(seed)
        assert flags[0].detector == "fundamental:turnaround"
        assert flags[0].kind == "fundamental"

    def test_rank_becomes_normalized_rank(self):
        seed = _make_seed("600000", source="dragon_tiger", metrics={"rank": 2, "net_inflow": 3e7})
        flags = _seed_to_flags(seed)
        assert flags[0].metrics["normalized_rank"] == 2

    def test_duplicate_sources_deduplicated(self):
        seed = _make_seed(
            "000002",
            source="alphasift",
            recall_sources=["alphasift", "alphasift"],
        )
        flags = _seed_to_flags(seed)
        # Same detector appears only once
        assert len(flags) == 1

    def test_trigger_signal_values_in_metrics(self):
        from src.agent.candidate_experts_v2.schemas import SeedItem

        seed = SeedItem(
            code="600036",
            name="招行",
            market="cn",
            source="daily_screener",
            hint="放量",
            freshness="2024-01-01",
            trigger_signals=[
                {"dimension": "technical", "signal_type": "vr_signal", "value": 2.5, "threshold": 1.0}
            ],
        )
        flags = _seed_to_flags(seed)
        assert flags[0].metrics.get("vr_signal") == 2.5


# ---------------------------------------------------------------------------
# _seeds_to_rows
# ---------------------------------------------------------------------------

class TestSeedsToRows:
    def test_one_seed_one_row(self):
        seeds = [_make_seed("600519")]
        rows = _seeds_to_rows(seeds)
        assert "600519" in rows
        assert len(rows) == 1

    def test_multi_source_merged_into_one_row(self):
        # Dedup already done by committee; seed has merged recall_sources
        seed = _make_seed(
            "000001",
            recall_sources=["daily_screener", "low_base_structure", "alphasift"],
        )
        rows = _seeds_to_rows([seed])
        row = rows["000001"]
        assert len(row.flags) == 3
        assert set(row.recall_sources) == {"daily_screener", "low_base_structure", "alphasift"}

    def test_empty_code_skipped(self):
        seeds = [_make_seed(""), _make_seed("600519")]
        rows = _seeds_to_rows(seeds)
        assert "" not in rows
        assert len(rows) == 1

    def test_multiple_codes(self):
        seeds = [
            _make_seed("600519", recall_sources=["alphasift", "sequoia"]),
            _make_seed("000001", recall_sources=["daily_screener"]),
            _make_seed("300750"),
        ]
        rows = _seeds_to_rows(seeds)
        assert len(rows) == 3
        # 600519 has 2 flags from 2 sources
        assert len(rows["600519"].flags) == 2


# ---------------------------------------------------------------------------
# _apply_coarse_cap
# ---------------------------------------------------------------------------

class TestApplyCoarseCap:
    def _make_row(self, code: str, n_flags: int) -> FeatureRow:
        return FeatureRow(
            code=code,
            flags=[FeatureFlag(detector=f"d{i}") for i in range(n_flags)],
        )

    def test_under_cap_all_kept(self):
        rows = [self._make_row(str(i), i + 1) for i in range(5)]
        result = _apply_coarse_cap(rows, cap=10)
        assert all(r.coarse_kept for r in result)

    def test_over_cap_low_hit_dropped(self):
        rows = [self._make_row(str(i), 3) for i in range(5)]
        # Add 1 row with only 1 flag
        rows.append(self._make_row("999", 1))
        result = _apply_coarse_cap(rows, cap=5)
        kept = [r for r in result if r.coarse_kept]
        dropped = [r for r in result if not r.coarse_kept]
        assert len(kept) == 5
        assert len(dropped) == 1
        assert dropped[0].code == "999"

    def test_tie_at_boundary_all_kept(self):
        # 7 rows all with 2 flags, cap=5 → all 7 must be kept (tie)
        rows = [self._make_row(str(i), 2) for i in range(7)]
        result = _apply_coarse_cap(rows, cap=5)
        assert all(r.coarse_kept for r in result)

    def test_coarse_drop_reason_recorded(self):
        rows = [self._make_row(str(i), 3) for i in range(4)]
        rows.append(self._make_row("low", 1))
        result = _apply_coarse_cap(rows, cap=4)
        dropped = [r for r in result if not r.coarse_kept]
        assert len(dropped) == 1
        assert "hit_count=1" in dropped[0].coarse_drop_reason

    def test_ordering_preserved_desc_by_flags(self):
        # Sorting only happens when cap is exceeded; use cap < len(rows) to trigger it
        rows = [self._make_row("a", 1), self._make_row("b", 3), self._make_row("c", 2)]
        result = _apply_coarse_cap(rows, cap=2)
        # With cap=2 and counts [3,2,1], the boundary is 2 → row "a" (count=1) dropped
        kept = [r for r in result if r.coarse_kept]
        flag_counts = [len(r.flags) for r in kept]
        assert flag_counts == sorted(flag_counts, reverse=True)


# ---------------------------------------------------------------------------
# build_recall_pool (integration, mocked)
# ---------------------------------------------------------------------------

class TestBuildRecallPool:
    def _mock_seeds(self) -> List[SeedItem]:
        return [
            _make_seed("600519", recall_sources=["daily_screener", "alphasift"]),
            _make_seed("000001", recall_sources=["daily_screener"]),
            _make_seed("300750", recall_sources=["low_base_structure"]),
        ]

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_basic_pool_structure(self, mock_pool, _mock_bars, _mock_fs):
        mock_pool.return_value = _fake_pool(self._mock_seeds())
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
            coarse_cap=120,
        )
        assert isinstance(result, RecallResult)
        assert result.total_in == 3
        assert result.total_kept == 3
        assert not result.coarse_truncated

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_reuses_prebuilt_seed_pool(self, mock_pool, _mock_bars, _mock_fs):
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
            coarse_cap=120,
            prebuilt_pool=_fake_pool(self._mock_seeds()),
        )

        mock_pool.assert_not_called()
        assert [row.code for row in result.rows] == ["600519", "000001", "300750"]

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_coarse_cap_applied(self, mock_pool, _mock_bars, _mock_fs):
        # 3 seeds, cap=2: one should be dropped
        seeds = [
            _make_seed("A", recall_sources=["daily_screener", "alphasift", "sequoia"]),  # 3 flags
            _make_seed("B", recall_sources=["daily_screener", "alphasift"]),             # 2 flags
            _make_seed("C", recall_sources=["daily_screener"]),                          # 1 flag
        ]
        mock_pool.return_value = _fake_pool(seeds)
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
            coarse_cap=2,
        )
        assert result.coarse_truncated
        assert result.total_kept == 2
        assert result.total_in == 3
        kept_codes = {r.code for r in result.rows}
        assert "C" not in kept_codes

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_fact_sheet_attached_to_each_row(self, mock_pool, _mock_bars, mock_fs):
        mock_pool.return_value = _fake_pool(self._mock_seeds())
        mock_fs.side_effect = lambda code, bars, **kw: FactSheet(code=code)
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
        )
        for row in result.rows:
            assert row.fact_sheet is not None

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_hit_count_hist_correct(self, mock_pool, _mock_bars, _mock_fs):
        seeds = [
            _make_seed("A", recall_sources=["daily_screener", "alphasift"]),
            _make_seed("B", recall_sources=["daily_screener"]),
            _make_seed("C", recall_sources=["daily_screener"]),
        ]
        mock_pool.return_value = _fake_pool(seeds)
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
        )
        # 1 row with 2 flags, 2 rows with 1 flag
        assert result.hit_count_hist.get(2) == 1
        assert result.hit_count_hist.get(1) == 2

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_no_global_priority_score_in_rows(self, mock_pool, _mock_bars, _mock_fs):
        """FeatureRows must not carry a global priority_score field (P3 invariant)."""
        mock_pool.return_value = _fake_pool(self._mock_seeds())
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
        )
        for row in result.rows:
            assert not hasattr(row, "priority_score") or row.__dict__.get("priority_score") is None

    @patch(
        "src.agent.candidate_experts_v2.recall._build_fact_sheet_phase_a",
        return_value=FactSheet(code="mock"),
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._load_daily_bars_batch",
        return_value={},
    )
    @patch(
        "src.agent.candidate_experts_v2.recall._build_seed_pool_result",
    )
    def test_sources_tally_correct(self, mock_pool, _mock_bars, _mock_fs):
        seeds = [
            _make_seed("A", recall_sources=["daily_screener", "alphasift"]),
            _make_seed("B", recall_sources=["daily_screener", "low_base_structure"]),
        ]
        mock_pool.return_value = _fake_pool(seeds)
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
        )
        assert result.sources.get("daily_screener") == 2
        assert result.sources.get("alphasift") == 1
        assert result.sources.get("low_base_structure") == 1
