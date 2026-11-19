# -*- coding: utf-8 -*-
"""ST / 退市 / 停牌 硬风险识别与 FactSheet 接线测试."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from src.agent.candidate_experts_v2.fact_sheet import build_fact_sheet
from src.agent.candidate_experts_v2.hard_risk import (
    assess_one,
    build_hard_risk_snapshot,
    summarize_hard_risk,
)
from src.agent.candidate_experts_v2.instruments import classify_instrument, is_a_share
from src.agent.candidate_experts_v2.recall import build_recall_pool
from src.agent.candidate_experts_v2.schemas import FactSheet, SeedItem
from src.agent.candidate_experts_v2.seed_facts import _compact_fact_sheet


def _write_stock_basic(cache_dir: Path, rows) -> None:
    pd.DataFrame(
        rows,
        columns=["ts_code", "name", "industry", "market", "list_date", "delist_date", "list_status"],
    ).to_csv(cache_dir / "stock_basic_LD.csv", index=False)


def _write_namechange(cache_dir: Path, rows) -> None:
    pd.DataFrame(
        rows,
        columns=["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"],
    ).to_csv(cache_dir / "namechange_snapshot.csv", index=False)


def _write_suspend(cache_dir: Path, day: str, rows) -> None:
    pd.DataFrame(
        rows,
        columns=["ts_code", "trade_date", "suspend_timing", "suspend_type"],
    ).to_csv(cache_dir / f"suspend_d_{day}.csv", index=False)


def _snapshot(cache_dir: Path, codes, **kwargs):
    return build_hard_risk_snapshot(
        codes,
        cache_dir=cache_dir,
        reference_date=kwargs.pop("reference_date", "20261116"),
        allow_online=False,
        **kwargs,
    )


class TestAssessOne:
    def test_st_delist_and_suspended_hits(self, tmp_path):
        _write_stock_basic(
            tmp_path,
            [
                ("600001.SH", "*ST测试", "软件", "主板", "20000101", "", "L"),
                ("600002.SH", "退市样本", "软件", "主板", "20000101", "20261101", "D"),
                ("600003.SH", "正常公司", "软件", "主板", "20000101", "", "L"),
            ],
        )
        _write_namechange(tmp_path, [("600001.SH", "*ST测试", "20250101", "", "20250101", "实施ST")])
        _write_suspend(tmp_path, "20261116", [("600003.SH", "20261116", "", "S")])

        result = _snapshot(tmp_path, ["600001", "600002", "600003"])

        assert result["600001"]["hard_risk_flags"] == ["st"]
        st_evidence = [e for e in result["600001"]["evidence"] if e["flag"] == "st"]
        assert any(e["status"] == "hit" and e["source"] == "namechange" for e in st_evidence)
        assert all(e["as_of"] for e in st_evidence if e["status"] == "hit")

        assert result["600002"]["hard_risk_flags"] == ["delist"]
        delist = [e for e in result["600002"]["evidence"] if e["flag"] == "delist" and e["status"] == "hit"]
        assert delist and "list_status=D" in delist[0]["detail"]

        assert result["600003"]["hard_risk_flags"] == ["suspended"]
        suspend = [e for e in result["600003"]["evidence"] if e["flag"] == "suspended" and e["status"] == "hit"]
        assert suspend and suspend[0]["source"] == "suspend_d"

    def test_missing_stock_basic_row_is_unknown_not_safe(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "正常公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(tmp_path, [])
        _write_suspend(tmp_path, "20261116", [])

        result = _snapshot(tmp_path, ["999999"])["999999"]

        assert result["hard_risk_flags"] == []
        assert set(result["unknown"]) == {"st", "delist"}
        unknown_items = [e for e in result["evidence"] if e["status"] == "unknown"]
        assert any("stock_basic 无" in e["detail"] for e in unknown_items)
        # ST 现在由 namechange 段在 stock_basic 缺失时兜底标注 unknown
        st_unknown = [e for e in unknown_items if e["flag"] == "st"]
        assert st_unknown and st_unknown[0]["source"] == "namechange"

    def test_all_sources_missing_marks_unknown(self, tmp_path):
        result = _snapshot(tmp_path, ["600001"])["600001"]

        assert result["hard_risk_flags"] == []
        assert set(result["unknown"]) == {"st", "delist", "suspended"}
        sources = {e["source"] for e in result["evidence"] if e["status"] == "unknown"}
        assert sources == {"stock_basic", "namechange", "suspend_d"}

    def test_namechange_current_st_confirms_even_if_basic_name_clean(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "测试公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(tmp_path, [("600001.SH", "ST测试", "20260101", "", "20260101", "实施ST")])
        _write_suspend(tmp_path, "20261116", [])

        result = _snapshot(tmp_path, ["600001"])["600001"]

        assert "st" in result["hard_risk_flags"]
        hit = [e for e in result["evidence"] if e["flag"] == "st" and e["status"] == "hit"]
        assert hit and hit[0]["source"] == "namechange"

    def test_historical_st_with_end_date_is_not_vetoed(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "测试公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(
            tmp_path,
            [
                ("600001.SH", "ST测试", "20180101", "20200101", "20180101", "实施ST"),
                ("600001.SH", "测试公司", "20200102", "", "20200102", "撤销ST"),
            ],
        )
        _write_suspend(tmp_path, "20261116", [])

        result = _snapshot(tmp_path, ["600001"])["600001"]

        assert result["hard_risk_flags"] == []
        clear = [e for e in result["evidence"] if e["flag"] == "st" and e["source"] == "namechange"]
        assert clear and clear[0]["status"] == "clear"
        assert "历史 ST" in clear[0]["detail"]

    def test_local_bar_gap_is_unknown_not_hit(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "测试公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(tmp_path, [])
        _write_suspend(tmp_path, "20261116", [])

        result = build_hard_risk_snapshot(
            ["600001"],
            bars_by_code={"600001": [{"date": "2026-11-10", "close": 10.0}]},
            reference_date="20261116",
            local_bar_date="2026-11-16",
            cache_dir=tmp_path,
            allow_online=False,
        )["600001"]

        assert "suspended" not in result["hard_risk_flags"]
        assert "suspended" in result["unknown"]
        gap = [e for e in result["evidence"] if e["source"] == "local_daily"]
        assert gap and gap[0]["status"] == "unknown"

    def test_non_a_share_code_is_unknown(self, tmp_path):
        result = _snapshot(tmp_path, ["00700"])["00700"]
        assert result["hard_risk_flags"] == []
        assert set(result["unknown"]) == {"st", "delist", "suspended"}
        assert all(e["source"] == "unsupported_market" for e in result["evidence"])

    def test_empty_suspend_cache_means_clear(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "测试公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(tmp_path, [])
        _write_suspend(tmp_path, "20261116", [])

        result = _snapshot(tmp_path, ["600001"])["600001"]

        assert result["hard_risk_flags"] == []
        suspend = [e for e in result["evidence"] if e["flag"] == "suspended"]
        assert suspend and suspend[0]["status"] == "clear"
        assert suspend[0]["source"] == "suspend_d"


class TestFactSheetWiring:
    def test_build_fact_sheet_keeps_evidence(self):
        evidence = [{"flag": "st", "status": "hit", "source": "stock_basic", "as_of": "2026-11-16", "detail": "x"}]
        sheet = build_fact_sheet(
            "600001",
            None,
            hard_risk_flags=["st"],
            hard_risk_evidence=evidence,
            hard_risk_unknown=["suspended"],
        )
        assert sheet.hard_risk_flags == ["st"]
        assert sheet.hard_risk_evidence == evidence
        assert sheet.hard_risk_unknown == ["suspended"]

    def test_summarize_hard_risk_counts(self):
        sheets = [
            FactSheet(code="600001", hard_risk_flags=["st"], hard_risk_evidence=[{"flag": "st", "status": "hit"}]),
            FactSheet(code="600002", hard_risk_evidence=[{"flag": "st", "status": "unknown"}]),
            FactSheet(code="600003"),
        ]
        summary = summarize_hard_risk(sheets)
        assert summary["evaluated"] == 3
        assert summary["hit"]["st"] == 1
        assert summary["unknown"]["st"] == 1
        assert summary["hits"] == [{"code": "600001", "flags": ["st"]}]


def _make_seed(code: str) -> SeedItem:
    return SeedItem(code=code, name=code, source="daily_screener")


def _fake_pool(seeds):
    pool = MagicMock()
    pool.seeds = seeds
    pool.diagnostics = []
    return pool


class TestRecallWiring:
    @patch("src.agent.candidate_experts_v2.recall._load_daily_bars_batch", return_value={"600001": []})
    @patch("src.agent.candidate_experts_v2.recall._build_seed_pool_result")
    def test_hard_risk_flags_reach_fact_sheet(self, mock_pool, _mock_bars):
        mock_pool.return_value = _fake_pool([_make_seed("600001")])
        snapshot = {
            "600001": {
                "hard_risk_flags": ["st"],
                "evidence": [{"flag": "st", "status": "hit", "source": "stock_basic", "as_of": "2026-11-16", "detail": "x"}],
                "unknown": ["suspended"],
            }
        }
        result = build_recall_pool(
            market="cn",
            seed_symbols=[],
            tool_registry=MagicMock(),
            hard_risk_snapshot=snapshot,
        )
        sheet = result.rows[0].fact_sheet
        assert sheet is not None
        assert sheet.hard_risk_flags == ["st"]
        assert sheet.hard_risk_unknown == ["suspended"]
        assert sheet.hard_risk_evidence[0]["source"] == "stock_basic"


class TestInstrumentClassification:
    def test_convertible_bond_etf_and_fund(self):
        assert classify_instrument("127045")[0] == "convertible_bond"
        assert classify_instrument("110059.SH")[0] == "convertible_bond"
        assert classify_instrument("128136")[0] == "convertible_bond"
        assert classify_instrument("510300")[0] == "etf"
        assert classify_instrument("159915")[0] == "etf"
        assert classify_instrument("161725")[0] == "fund"

    def test_a_share_including_beijing_exchange(self):
        for code in ("600519", "000001", "300750", "688981", "920753", "430047", "830799", "871981"):
            assert is_a_share(code), code

    def test_non_stock_instrument_is_not_applicable_not_unknown(self, tmp_path):
        result = _snapshot(tmp_path, ["127045", "510300", "161725"])

        for code in ("127045", "510300", "161725"):
            verdict = result[code]
            assert verdict["hard_risk_flags"] == []
            assert verdict["unknown"] == []
            assert verdict["not_applicable"] is True
            assert all(item["status"] == "not_applicable" for item in verdict["evidence"])
            assert all(item["source"] == "instrument_type" for item in verdict["evidence"])


class TestUnstarredStock:
    def test_current_non_st_name_overrides_stale_stock_basic_name(self, tmp_path):
        # stock_basic 名称滞后仍带 *ST，但 namechange 显示已摘帽 (end_date 非空 + 当前在册非 ST)
        _write_stock_basic(tmp_path, [("600001.SH", "*ST测试", "软件", "主板", "20000101", "", "L")])
        _write_namechange(
            tmp_path,
            [
                ("600001.SH", "*ST测试", "20180101", "20200101", "20180101", "实施ST"),
                ("600001.SH", "测试公司", "20200102", "", "20200102", "撤销ST"),
            ],
        )
        _write_suspend(tmp_path, "20261116", [])

        verdict = _snapshot(tmp_path, ["600001"])["600001"]

        assert verdict["hard_risk_flags"] == []
        assert verdict["unknown"] == []
        st_clear = [item for item in verdict["evidence"] if item["flag"] == "st" and item["status"] == "clear"]
        assert st_clear and st_clear[0]["source"] == "namechange"
        assert "可能滞后" in st_clear[0]["detail"]

    def test_historical_st_rows_never_hit_without_current_record(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "测试公司", "软件", "主板", "20000101", "", "L")])
        _write_namechange(
            tmp_path,
            [("600001.SH", "ST测试", "20180101", "20200101", "20180101", "实施ST")],
        )
        _write_suspend(tmp_path, "20261116", [])

        verdict = _snapshot(tmp_path, ["600001"])["600001"]

        assert "st" not in verdict["hard_risk_flags"]
        st_clear = [item for item in verdict["evidence"] if item["flag"] == "st" and item["status"] == "clear"]
        assert st_clear and "历史 ST" in st_clear[0]["detail"]

    def test_still_st_without_namechange_current_uses_stock_basic(self, tmp_path):
        _write_stock_basic(tmp_path, [("600001.SH", "*ST测试", "软件", "主板", "20000101", "", "L")])
        _write_namechange(
            tmp_path,
            [("600001.SH", "*ST测试", "20180101", "", "20180101", "实施ST")],
        )
        _write_suspend(tmp_path, "20261116", [])

        verdict = _snapshot(tmp_path, ["600001"])["600001"]

        assert "st" in verdict["hard_risk_flags"]


class TestSummaryAuditFields:
    def test_unknown_and_not_applicable_are_explicit(self):
        sheets = [
            FactSheet(
                code="600002",
                hard_risk_evidence=[{"flag": "st", "status": "unknown"}, {"flag": "delist", "status": "clear"}],
            ),
            FactSheet(
                code="127045",
                hard_risk_evidence=[
                    {"flag": "st", "status": "not_applicable"},
                    {"flag": "delist", "status": "not_applicable"},
                    {"flag": "suspended", "status": "not_applicable"},
                ],
            ),
        ]

        summary = summarize_hard_risk(sheets)

        assert summary["unknowns"] == [{"code": "600002", "flags": ["st"]}]
        assert summary["not_applicable_codes"] == [
            {"code": "127045", "flags": ["delist", "st", "suspended"]}
        ]
        assert summary["not_applicable"]["st"] == 1


class TestCompactFactSheet:
    def test_hard_risk_fields_survive_compaction(self):
        evidence = [{"flag": "st", "status": "clear", "source": "namechange", "as_of": "2020-01-02", "detail": "x"}]
        compact = _compact_fact_sheet(
            {
                "capital_direction": "inflow",
                "hard_risk_flags": ["st"],
                "hard_risk_evidence": evidence,
                "hard_risk_unknown": ["suspended"],
            }
        )

        assert compact["hard_risk_flags"] == ["st"]
        assert compact["hard_risk_evidence"] == evidence
        assert compact["hard_risk_unknown"] == ["suspended"]


class TestNonStockSeedFiltering:
    def test_recall_drops_convertible_bond_seed(self):
        pool = _fake_pool([_make_seed("127045"), _make_seed("600001")])
        snapshot = {"600001": {"hard_risk_flags": [], "evidence": [], "unknown": []}}
        with patch("src.agent.candidate_experts_v2.recall._load_daily_bars_batch", return_value={}):
            result = build_recall_pool(
                market="cn",
                seed_symbols=[],
                tool_registry=MagicMock(),
                prebuilt_pool=pool,
                hard_risk_snapshot=snapshot,
            )

        assert [row.code for row in result.all_rows] == ["600001"]
        filtered = [item for item in result.diagnostics if item.get("source") == "instrument_type_filter"]
        assert filtered and filtered[0]["detail"]["excluded_by_type"] == {"convertible_bond": 1}

    def test_committee_hard_exclusion_rejects_non_stock_keeps_beijing(self):
        from src.agent.candidate_experts_v2.committee import _passes_hard_exclusion

        keep, reason = _passes_hard_exclusion(_make_seed("127045"))
        assert keep is False
        assert reason == "non_stock_instrument:convertible_bond"

        keep, reason = _passes_hard_exclusion(_make_seed("920753"))
        assert keep is True, reason
