# -*- coding: utf-8 -*-
"""FAC-01b 板块资金流观察值：计算、映射、快照与隔离守卫测试。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.agent.candidate_experts_v2 import sector_flow as sf
from src.agent.candidate_experts_v2.fact_sheet import build_fact_sheet
from src.agent.candidate_experts_v2.schemas import FactSheet, SectorFlowFact

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeSource:
    def __init__(self, catalog, histories, rank5=None, fail=(), constituents=None):
        self.catalog = {item["bk_code"]: item for item in catalog}
        self.histories = histories
        self.rank5 = rank5 or {}
        self.fail = set(fail)
        self.constituents = constituents or {}
        self.history_calls = 0

    def board_catalog(self):
        return [dict(item) for item in self.catalog.values()]

    def flow_rank(self, indicator="5日"):
        return dict(self.rank5)

    def board_history(self, board_name, limit):
        self.history_calls += 1
        if board_name in self.fail:
            raise TimeoutError("provider timeout")
        return self.histories.get(board_name, [])[-limit:]

    def board_constituents(self, board_name):
        return self.constituents.get(board_name, [])


class FakeTarget:
    def __init__(self, code):
        self.code = code
        self.fact_sheet = build_fact_sheet(code)


def _row(day, amount, pct=1.0):
    return {
        "date": day,
        "main_amount": amount,
        "main_amount_pct": pct,
        "super_big_amount": amount / 2,
        "big_amount": amount / 2,
    }


def _catalog(*items):
    return [
        {"bk_code": code, "bk_name": name, "member_count": size}
        for code, name, size in items
    ]


DAY1, DAY2, DAY3, DAY4, DAY5 = (
    "2026-11-12",
    "2026-11-13",
    "2026-11-14",
    "2026-11-17",
    "2026-11-18",
)


# ---------------------------------------------------------------------------
# pure calculations
# ---------------------------------------------------------------------------


def test_percentile_rank_with_ties():
    values = [1.0, 2.0, 2.0, 3.0]
    assert sf.compute_percentile(values, 1.0) == 0.125
    assert sf.compute_percentile(values, 2.0) == 0.5
    assert sf.compute_percentile(values, 3.0) == 0.875
    assert sf.compute_percentile(values, None) is None
    assert sf.compute_percentile([], 1.0) is None


@pytest.mark.parametrize(
    "sector_return,expected",
    [(7.9, 0.0), (8.0, 0.0), (13.4, 0.18), (20.0, 0.18)],
)
def test_overheat_penalty_boundaries(sector_return, expected):
    penalty, status = sf.compute_overheat(sector_return, sf.load_settings())
    assert penalty == expected
    assert status == "ok"


def test_overheat_missing_does_not_penalize():
    penalty, status = sf.compute_overheat(None, sf.load_settings())
    assert penalty is None
    assert status == "missing"
    metrics = {"net_inflow_sum": 100.0, "positive_inflow_days": 3}
    # 过热缺失时按 0 处理，不影响分数
    assert sf.compute_score(metrics, 0.6, penalty) == 0.7


def test_zero_is_not_missing():
    metrics = sf.compute_board_metrics([_row(DAY5, 0.0)], 5, expected_dates=[DAY5])
    assert metrics["sample_days"] == 1
    assert metrics["net_inflow_sum"] == 0.0
    assert metrics["positive_inflow_days"] == 0
    assert metrics["latest_net_inflow"] == 0.0
    assert metrics["missing_dates"] == []


def test_missing_middle_day_skips_and_counts_sample_days():
    rows = [_row(DAY1, 10.0), _row(DAY2, 20.0), _row(DAY4, 30.0), _row(DAY5, 40.0)]
    metrics = sf.compute_board_metrics(rows, 5, expected_dates=[DAY1, DAY2, DAY3, DAY4, DAY5])
    assert metrics["sample_days"] == 4
    assert metrics["missing_dates"] == [DAY3]
    assert metrics["positive_inflow_days"] == 4
    assert metrics["net_inflow_sum"] == 100.0


def test_partial_window_reweights_composite():
    metrics = sf.compute_board_metrics(
        [_row(DAY1, 10.0, 1.0), _row(DAY2, -5.0, -0.5)], 5, expected_dates=[DAY1, DAY2]
    )
    percentiles = {"net_inflow_sum": 0.9, "main_inflow_pct_avg": 0.7}
    weights = sf.load_settings()["weights"]
    composite = sf.compute_composite(metrics, percentiles, weights, 5)
    # 只有 4 个分项可用（net / pct / positive_days / large_days），权重需重归一到 1
    expected = (
        0.32 * 0.9 + 0.20 * 0.7 + 0.18 * (1 / 5) + 0.08 * (1 / 5)
    ) / (0.32 + 0.20 + 0.18 + 0.08)
    assert composite == pytest.approx(expected, abs=1e-4)


def test_score_outflow_branch():
    metrics = {"net_inflow_sum": -100.0, "positive_inflow_days": 1}
    assert sf.compute_score(metrics, 0.8, 0.0) == pytest.approx(0.1, abs=1e-6)
    assert sf.compute_score({"net_inflow_sum": -100.0}, None, 0.0) is None


def test_map_state_branches():
    thresholds = sf.load_settings()["state_thresholds"]
    assert sf.map_state(0.7, {"net_inflow_sum": 10, "positive_inflow_days": 4}, thresholds) == "persistent_inflow"
    assert sf.map_state(0.5, {"net_inflow_sum": 10, "positive_inflow_days": 2}, thresholds) == "net_inflow"
    assert sf.map_state(0.35, {"net_inflow_sum": 10, "positive_inflow_days": 2}, thresholds) == "neutral"
    assert sf.map_state(0.1, {"net_inflow_sum": -10, "positive_inflow_days": 1}, thresholds) == "outflow"
    assert sf.map_state(None, {"net_inflow_sum": 10}, thresholds) == "unknown"


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def test_code_match_preferred_over_alias():
    catalog = {"BK1283": {"bk_code": "BK1283", "bk_name": "银行", "member_count": 40}}
    aliases = sf.load_aliases()
    boards = [{"code": "BK1283", "name": "银行", "type": "行业"}]
    result = sf.resolve_board("000001", boards, catalog, aliases)
    assert result["mapping_method"] == "code"
    assert result["mapping_confidence"] == "high"
    assert result["industry_key"] == "bank"


def test_alias_exact_match_rejects_substring():
    catalog = {"BK1283": {"bk_code": "BK1283", "bk_name": "银行", "member_count": 40}}
    aliases = sf.load_aliases()
    # 「银行股」不是任何别名，禁止子串匹配 → 不能映射到银行
    boards = [{"code": "BKXXXX", "name": "银行股", "type": "行业"}]
    result = sf.resolve_board("000001", boards, catalog, aliases)
    assert result["mapping_method"] == "unmapped"
    assert result["missing_reason"] == "unmapped_industry"


def test_alias_ambiguity_unmapped():
    catalog = {"BK1283": {"bk_code": "BK1283", "bk_name": "银行", "member_count": 40}}
    aliases = {
        "canonical": {
            "bank": {"key": "bank", "display_name": "银行", "bk_code": "BK1283", "aliases": ["银行"]},
            "bank2": {"key": "bank2", "display_name": "银行2", "bk_code": "BK1283", "aliases": ["银行"]},
        },
        "alias_index": {"银行": ["bank", "bank2"]},
    }
    boards = [{"code": "BKXXXX", "name": "银行", "type": "行业"}]
    result = sf.resolve_board("000001", boards, catalog, aliases)
    assert result["mapping_method"] == "unmapped"
    assert any("alias_ambiguous" in note for note in result["quality_notes"])


def test_multi_board_prefers_most_specific():
    catalog = {
        "BK0438": {"bk_code": "BK0438", "bk_name": "食品饮料", "member_count": 130},
        "BK1277": {"bk_code": "BK1277", "bk_name": "白酒Ⅱ", "member_count": 20},
        "BK1575": {"bk_code": "BK1575", "bk_name": "白酒Ⅲ", "member_count": 20},
    }
    boards = [
        {"code": "BK0438", "name": "食品饮料"},
        {"code": "BK1575", "name": "白酒Ⅲ"},
        {"code": "BK1277", "name": "白酒Ⅱ"},
    ]
    result = sf.resolve_board("600519", boards, catalog, sf.load_aliases())
    # 成分股最少者优先，并列按 bk_code 升序
    assert result["selected"]["bk_code"] == "BK1277"
    assert len(result["candidate_boards"]) == 3


def test_no_boards_reports_no_bk_code():
    result = sf.resolve_board("600519", [], {}, sf.load_aliases())
    assert result["mapping_method"] == "unmapped"
    assert result["missing_reason"] == "no_bk_code"


def test_constituent_fallback_when_enabled():
    catalog = {"BK1283": {"bk_code": "BK1283", "bk_name": "银行", "member_count": 40}}
    result = sf.resolve_board(
        "000001", [], catalog, sf.load_aliases(), constituent_index={"000001": ["BK1283"]}
    )
    assert result["mapping_method"] == "constituent"
    assert result["mapping_confidence"] == "medium"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def _snapshot(source, codes, as_of=None, settings=None):
    catalog = {item["bk_code"]: item for item in source.board_catalog()}
    return sf._build_snapshot_payload(
        source,
        catalog=catalog,
        rank5=source.flow_rank("5日"),
        universe_codes=codes,
        as_of_date=as_of,
        settings=settings or sf.load_settings(),
    )


def test_missing_history_0_1_days_is_missing():
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {"板块一": [], "板块二": [_row(DAY5, 10.0)]},
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": 1.0}},
    )
    snapshot = _snapshot(source, ["BK0001", "BK0002"])
    assert snapshot["boards"]["BK0001"]["status"] == "missing"
    assert snapshot["boards"]["BK0001"]["missing_reason"] == "insufficient_history"
    assert snapshot["boards"]["BK0002"]["status"] == "missing"


def test_partial_window_marks_score_partial():
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {
            "板块一": [_row(DAY1, 10.0), _row(DAY2, 20.0), _row(DAY3, 30.0)],
            "板块二": [_row(DAY1, -10.0), _row(DAY2, -20.0), _row(DAY3, -30.0)],
        },
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": -1.0}},
    )
    snapshot = _snapshot(source, ["BK0001", "BK0002"])
    assert snapshot["boards"]["BK0001"]["status"] == "partial"

    resolution = {
        "selected": {"bk_code": "BK0001", "bk_name": "板块一"},
        "mapping_method": "code",
        "mapping_confidence": "high",
        "candidate_boards": [],
    }
    fact = sf.build_sector_flow_fact("600519", resolution, snapshot)
    assert fact.status == "partial"
    assert fact.score_partial is True
    assert fact.usage == "observation"
    assert fact.display_only is True
    assert any("sample_days=3/5" in note for note in fact.quality_notes)


def test_provider_timeout_degrades_without_raising():
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {"板块二": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)]},
        rank5={"板块二": {"return_pct": 1.0}},
        fail={"板块一"},
    )
    snapshot = _snapshot(source, ["BK0001", "BK0002"])
    assert snapshot["boards"]["BK0001"]["status"] == "missing"
    assert snapshot["boards"]["BK0001"]["missing_reason"] == "provider_failed"
    assert snapshot["boards"]["BK0002"]["status"] == "ok"
    assert "BK0001" in snapshot["errors"]


def test_snapshot_roundtrip_and_date_key(tmp_path):
    catalog = _catalog(("BK0001", "板块一", 10))
    source = FakeSource(
        catalog,
        {"板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)]},
        rank5={"板块一": {"return_pct": 1.0}},
    )
    snapshot = sf.build_or_load_snapshot(
        None,
        universe_codes=["BK0001"],
        cache_dir=str(tmp_path),
        force_refresh=True,
        source=source,
        settings=sf.load_settings(),
    )
    assert snapshot["as_of_date"] == DAY5
    assert (tmp_path / f"sector_flow_snapshot_{DAY5}.json").exists()
    loaded, lag = sf.load_snapshot_for_date(DAY5, cache_dir=str(tmp_path))
    assert lag == 0
    assert loaded["as_of_date"] == DAY5


def test_snapshot_cached_not_refetched(tmp_path):
    catalog = _catalog(("BK0001", "板块一", 10))
    source = FakeSource(
        catalog,
        {"板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)]},
        rank5={"板块一": {"return_pct": 1.0}},
    )
    sf.build_or_load_snapshot(
        None, universe_codes=["BK0001"], cache_dir=str(tmp_path), force_refresh=True, source=source
    )
    calls_after_first = source.history_calls
    cached = sf.build_or_load_snapshot(
        DAY5, universe_codes=["BK0001"], cache_dir=str(tmp_path), force_refresh=False, source=source
    )
    assert source.history_calls == calls_after_first
    assert cached["as_of_date"] == DAY5


def test_select_universe_full_mode_covers_catalog():
    catalog = {
        "BK0003": {"bk_code": "BK0003", "bk_name": "板块三", "member_count": 10},
        "BK0001": {"bk_code": "BK0001", "bk_name": "板块一", "member_count": 10},
        "BK0002": {"bk_code": "BK0002", "bk_name": "板块二", "member_count": 10},
    }
    # limit=0 → 全量；映射板块优先，其余按 bk_code 升序，顺序可复现
    assert sf._select_universe(["BK0002"], catalog, {}, 0) == ["BK0002", "BK0001", "BK0003"]
    # limit>0 且已覆盖映射板块时保持映射顺序
    assert sf._select_universe(["BK0002"], catalog, {}, 1) == ["BK0002"]


def test_full_universe_marks_complete():
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {
            "板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
            "板块二": [_row(day, -10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
        },
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": -1.0}},
    )
    snapshot = _snapshot(source, ["BK0001", "BK0002"])
    assert snapshot["percentile_universe"] == "complete"
    assert snapshot["universe_coverage"] == 1.0
    assert snapshot["percentile_sample_size"] == 2
    assert snapshot["failed_boards"] == []


def test_failed_board_keeps_partial_and_lists_reason():
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {"板块二": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)]},
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": 1.0}},
        fail={"板块一"},
    )
    snapshot = _snapshot(source, ["BK0001", "BK0002"])
    # 覆盖率是全量发起比例，样本数是真正进入分位的板块数，两者分开记录
    assert snapshot["universe_coverage"] == 1.0
    assert snapshot["percentile_sample_size"] == 1
    assert snapshot["percentile_universe"] == "partial"
    assert snapshot["failed_boards"] == [
        {"bk_code": "BK0001", "bk_name": "板块一", "error": "TimeoutError: provider timeout"}
    ]


def test_retry_only_refetches_failed_boards(tmp_path):
    catalog = _catalog(
        ("BK0001", "板块一", 10), ("BK0002", "板块二", 10), ("BK0003", "板块三", 10)
    )
    histories = {
        name: [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)]
        for name in ("板块一", "板块二", "板块三")
    }
    source = FakeSource(
        catalog,
        histories,
        rank5={name: {"return_pct": 1.0} for name in histories},
        fail={"板块二"},
    )
    first = sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), force_refresh=True, source=source, settings=sf.load_settings()
    )
    assert first["percentile_universe"] == "partial"
    assert source.history_calls == 4  # 板块二按 retry=1 请求两次，其余各一次

    calls_before = source.history_calls
    source.fail.clear()
    retried = sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), retry_failed=True, source=source, settings=sf.load_settings()
    )
    assert retried["boards"]["BK0002"]["status"] == "ok"
    assert retried["errors"] == {}
    assert retried["percentile_universe"] == "complete"
    assert source.history_calls - calls_before == 1  # 只重取失败板块


def test_retry_respects_cooldown(tmp_path):
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {
            "板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
            "板块二": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
        },
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": 1.0}},
        fail={"板块二"},
    )
    sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), force_refresh=True, source=source, settings=sf.load_settings()
    )
    calls_before = source.history_calls
    source.fail.clear()
    path = tmp_path / f"sector_flow_snapshot_{DAY5}.json"

    # 冷却内：直接命中缓存，不重试
    snapshot = sf._read_json(path)
    snapshot["last_error_retry_at"] = sf._now_iso()
    sf._write_json_atomic(path, snapshot)
    cached = sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), retry_failed=True, source=source, settings=sf.load_settings()
    )
    assert source.history_calls == calls_before
    assert "BK0002" in cached["errors"]

    # 冷却已过：只重试失败板块
    snapshot["last_error_retry_at"] = (
        datetime.now() - timedelta(minutes=30)
    ).replace(microsecond=0).isoformat()
    sf._write_json_atomic(path, snapshot)
    retried = sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), retry_failed=True, source=source, settings=sf.load_settings()
    )
    assert source.history_calls == calls_before + 1
    assert retried["percentile_universe"] == "complete"


def test_full_mode_rebuilds_truncated_cached_snapshot(tmp_path):
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {
            "板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
            "板块二": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
        },
        rank5={"板块一": {"return_pct": 1.0}, "板块二": {"return_pct": 1.0}},
    )
    # 模拟 FAC-01b 遗留的截断快照：catalog 2 个板块，只抓了 1 个
    truncated = sf._build_snapshot_payload(
        source,
        catalog={item["bk_code"]: item for item in catalog},
        rank5=source.flow_rank("5日"),
        universe_codes=["BK0001"],
        as_of_date=DAY5,
        settings=sf.load_settings(),
    )
    sf._write_json_atomic(tmp_path / f"sector_flow_snapshot_{DAY5}.json", truncated)
    assert truncated["universe_size"] == 1

    calls_before = source.history_calls
    rebuilt = sf.build_or_load_snapshot(
        DAY5, cache_dir=str(tmp_path), source=source, settings=sf.load_settings()
    )
    assert rebuilt["universe_size"] == 2
    assert rebuilt["percentile_universe"] == "complete"
    assert source.history_calls > calls_before


def test_attach_disabled_returns_immediately():
    targets = [FakeTarget("600519")]
    assert sf.attach_sector_flow(targets, enabled=False) == {"enabled": False}
    assert targets[0].fact_sheet.sector_flow is None


def test_attach_does_not_change_candidate_order(tmp_path):
    catalog = _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10))
    source = FakeSource(
        catalog,
        {
            "板块一": [_row(day, 10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
            "板块二": [_row(day, -10.0) for day in (DAY1, DAY2, DAY3, DAY4, DAY5)],
        },
        rank5={
            "板块一": {"return_pct": 1.0},
            "板块二": {"return_pct": -1.0},
        },
    )
    targets = [FakeTarget("600519"), FakeTarget("000001")]
    boards = {
        "600519": [{"code": "BK0002", "name": "板块二"}],
        "000001": [{"code": "BK0001", "name": "板块一"}],
    }
    summary = sf.attach_sector_flow(
        targets,
        enabled=True,
        cache_dir=str(tmp_path),
        source=source,
        boards_by_code=boards,
        settings=sf._deep_merge(sf.load_settings(), {"universe_limit": 10}),
    )
    assert [t.code for t in targets] == ["600519", "000001"]
    assert summary["coverage"] == 1.0
    assert summary["status_counts"] == {"ok": 2}
    assert targets[0].fact_sheet.sector_flow.state == "outflow"
    assert targets[1].fact_sheet.sector_flow.state == "persistent_inflow"


# ---------------------------------------------------------------------------
# isolation guards
# ---------------------------------------------------------------------------


def test_aggregator_and_experts_do_not_read_sector_flow_score_state():
    pattern = re.compile(r"sector_flow\s*(?:\.|\[|\()")
    paths = [_REPO_ROOT / "src/agent/candidate_experts_v2/aggregator.py"]
    paths.extend(sorted((_REPO_ROOT / "src/agent/candidate_experts_v2/experts").glob("*.py")))
    offenders = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == []


def test_compact_fact_sheet_excludes_sector_flow():
    from src.agent.candidate_experts_v2.seed_facts import _compact_fact_sheet

    fact_sheet = FactSheet(
        code="600519",
        sector_flow=SectorFlowFact(score=0.9, state="persistent_inflow"),
    )
    compact = _compact_fact_sheet(fact_sheet.model_dump())
    assert "sector_flow" not in compact


def test_desk_fact_sheet_dict_excludes_sector_flow():
    from src.agent.candidate_experts_v2.experts.desk_base import _fact_sheet_to_dict

    fact_sheet = FactSheet(
        code="600519",
        sector_flow=SectorFlowFact(score=0.9, state="persistent_inflow"),
    )
    assert "sector_flow" not in _fact_sheet_to_dict(fact_sheet)


class _ConnectionErrorSource(FakeSource):
    def board_history(self, board_name, limit):
        self.history_calls += 1
        time.sleep(0.03)
        raise ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
        )


def test_connection_failures_trip_circuit_breaker():
    catalog = _catalog(*[(f"BK{i:04d}", f"板块{i}", 10) for i in range(1, 13)])
    source = _ConnectionErrorSource(catalog, {})
    settings = dict(sf.load_settings())
    settings["concurrency"] = 1
    settings["retry"] = 0

    snapshot = _snapshot(source, [item["bk_code"] for item in catalog], settings=settings)

    assert snapshot.get("provider_unreachable") is True
    breaker = [msg for msg in snapshot["errors"].values() if msg.startswith("provider_unreachable")]
    assert breaker
    # 熔断后剩余板块不再逐个调用上游
    assert source.history_calls < len(catalog)
    assert len(breaker) == len(catalog) - sf._FAILURE_STREAK_LIMIT
