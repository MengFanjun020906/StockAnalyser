# -*- coding: utf-8 -*-
"""FAC-03 估值分位观察值：分位计算、亏损排除、样本门槛、窗口与隔离守卫测试。"""

from __future__ import annotations

from pathlib import Path

from src.agent.candidate_experts_v2 import valuation_fact as vf
from src.agent.candidate_experts_v2.experts.desk_base import _fact_sheet_to_dict
from src.agent.candidate_experts_v2.schemas import FactSheet
from src.agent.candidate_experts_v2.seed_facts import (
    _compact_fact_sheet,
    _fact_sheet_to_dict as _seed_fact_sheet_to_dict,
)


class _Target:
    def __init__(self, code: str) -> None:
        self.code = code
        self.fact_sheet = FactSheet(code=code)


def _write_basic(cache: Path, day: str, rows: list[tuple[str, float]]) -> None:
    lines = ["ts_code,pe_ttm"]
    lines.extend(f"{code}.SH,{pe}" for code, pe in rows)
    (cache / f"basic_{day}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_industry(cache: Path, mapping: dict[str, str]) -> None:
    lines = ["l1_code,l1_name,ts_code,name,in_date,out_date,is_new"]
    for code, l1_code in mapping.items():
        lines.append(f"{l1_code},行业A,{code}.SH,示例,20200101,,Y")
    (cache / "industry_snapshot_sw2021.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _days(count: int) -> list[str]:
    return [f"202601{index + 1:02d}" if index < 31 else f"202602{index - 30:02d}" for index in range(count)]


def _setup(cache: Path, days: list[str], target_series: list[float], peers: dict[str, float]) -> None:
    for day, pe in zip(days, target_series):
        rows = [("600000", pe), *[(code, value) for code, value in peers.items()]]
        _write_basic(cache, day, rows)
    _write_industry(cache, {"600000": "801010", **{code: "801010" for code in peers}})


def _peers(count: int) -> dict[str, float]:
    return {f"6001{index:02d}": float(10 + index * 2) for index in range(count)}


def test_disabled_leaves_fact_sheet_untouched(tmp_path: Path) -> None:
    target = _Target("600000")
    summary = vf.attach_valuation_facts([target], enabled=False, cache_dir=str(tmp_path))
    assert summary == {"enabled": False}
    assert target.fact_sheet.valuation is None


def test_percentiles_and_20d_change(tmp_path: Path) -> None:
    days = _days(21)
    _setup(tmp_path, days, [float(10 + index) for index in range(21)], _peers(10))
    target = _Target("600000")

    summary = vf.attach_valuation_facts(
        [target], as_of_date="2026-01-21", enabled=True, cache_dir=str(tmp_path)
    )
    fact = target.fact_sheet.valuation

    assert summary["enabled"] is True
    assert summary["data_as_of_date"] == "2026-01-21"
    assert fact is not None
    assert fact.pe_ttm == 30.0
    assert fact.pe_negative is False
    # 10 个同业 PE 10..28 + 自身 30 => 中位 20，分位 1.0
    assert fact.industry_peer_count == 11
    assert fact.industry_pe_median == 20.0
    assert fact.industry_pe_percentile == 1.0
    # 自身 10..30 递增，当前为最大值
    assert fact.history_observations == 21
    assert fact.history_pe_percentile == 1.0
    assert fact.pe_change_20d == 200.0
    assert fact.status == "ok"
    assert fact.usage == "observation"
    assert fact.display_only is True


def test_loss_making_flagged_and_excluded(tmp_path: Path) -> None:
    days = _days(21)
    _setup(tmp_path, days, [-5.0] * 21, _peers(10))
    target = _Target("600000")

    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    fact = target.fact_sheet.valuation

    assert fact.pe_negative is True
    assert fact.industry_pe_median is None
    assert fact.industry_pe_percentile is None
    assert fact.history_pe_percentile is None
    assert fact.status == "partial"
    assert "pe_negative_excluded_from_percentiles" in fact.quality_notes


def test_industry_peers_below_minimum(tmp_path: Path) -> None:
    days = _days(21)
    _setup(tmp_path, days, [20.0] * 21, _peers(3))
    target = _Target("600000")

    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    fact = target.fact_sheet.valuation

    assert fact.industry_peer_count == 4
    assert fact.industry_pe_median is None
    assert fact.industry_pe_percentile is None
    assert fact.missing_reason == "industry_peers_below_minimum"


def test_history_observations_below_minimum(tmp_path: Path) -> None:
    days = _days(10)
    _setup(tmp_path, days, [20.0] * 10, _peers(10))
    target = _Target("600000")

    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    fact = target.fact_sheet.valuation

    assert fact.history_observations == 10
    assert fact.history_pe_percentile is None
    assert fact.missing_reason == "history_observations_below_minimum"


def test_history_window_capped_at_120_days(tmp_path: Path) -> None:
    days = _days(130)
    _setup(tmp_path, days, [20.0] * 130, _peers(10))
    target = _Target("600000")

    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    fact = target.fact_sheet.valuation

    assert fact.history_observations == vf.HISTORY_WINDOW_DAYS == 120
    assert fact.pe_ttm == 20.0


def test_20d_change_is_none_when_base_pe_not_positive(tmp_path: Path) -> None:
    days = _days(21)
    series = [-1.0] + [float(5 + index) for index in range(20)]
    _setup(tmp_path, days, series, _peers(10))
    target = _Target("600000")

    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    assert target.fact_sheet.valuation.pe_change_20d is None


def test_missing_cache_returns_none_fields(tmp_path: Path) -> None:
    target = _Target("600000")
    summary = vf.attach_valuation_facts(
        [target], enabled=True, cache_dir=str(tmp_path / "absent")
    )
    fact = target.fact_sheet.valuation

    assert summary["enabled"] is True
    assert summary["coverage"] == 0.0
    assert fact.pe_ttm is None
    assert fact.status == "missing"
    assert fact.missing_reason == "pe_ttm_missing"


def test_display_serialization_and_desk_isolation(tmp_path: Path) -> None:
    days = _days(21)
    _setup(tmp_path, days, [20.0] * 21, _peers(10))
    target = _Target("600000")
    vf.attach_valuation_facts([target], enabled=True, cache_dir=str(tmp_path))
    fact = target.fact_sheet.valuation

    payload = vf.serialize_for_display(fact)
    assert payload["display_only"] is True
    assert payload["usage"] == "observation"
    assert payload["industry_source"] == "sw2021_l1"
    assert payload["pe_ttm"] == 20.0

    # 仅展示：不得进入 desk 事实包与 seed 精简事实。
    assert "valuation" not in _fact_sheet_to_dict(target.fact_sheet)
    assert "valuation" not in _compact_fact_sheet(target.fact_sheet.model_dump())
    assert "valuation" not in _seed_fact_sheet_to_dict(target.fact_sheet)
