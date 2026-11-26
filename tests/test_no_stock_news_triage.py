# -*- coding: utf-8 -*-
"""无个股新闻分类与打分：权重可追溯、零权重、硬丢弃与展示隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.data.stock_name_index import AStockNameMatcher
from src.services.news_no_stock_triage import (
    EVIDENCE,
    MIN_T,
    T_MAX,
    score_from_t,
    triage_many,
    triage_news_item,
)

EXPECTED_SCORES = {
    "us_index_spx": 96,
    "us_index_ndx": 82,
    "us_index_dji": 90,
    "us_chain": 100,
    "us_tech_telecom": 94,
    "us_tech_computer": 69,
    "us_tech_power_equipment": 56,
    "jpkr_kospi_electronics": 41,
    "jpkr_n225_electronics": 41,
    "jpkr_n225_machinery": 50,
    "jpkr_n225_auto": 46,
}

ZERO_WEIGHT_KEYS = (
    "europe",
    "commodity_copper",
    "commodity_gold",
    "commodity_lithium",
    "commodity_rebar",
    "commodity_oil",
    "commodity_lag",
    "commodity_generic",
    "macro_lpr",
    "macro_pmi",
    "macro_cpi",
    "macro_credit",
    "macro_overseas",
    "industrial_policy",
    "major_macro",
    "sector_generic",
    "index_recap",
    "other",
)


def test_score_formula_bounds():
    assert T_MAX == 6.8 and MIN_T == 2.0
    assert score_from_t(6.8) == 100
    assert score_from_t(6.8 * 2) == 100
    assert score_from_t(1.99) == 0
    assert score_from_t(None) == 0


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_SCORES.items()))
def test_each_tier_score_traces_to_measured_t(key, expected):
    evidence = EVIDENCE[key]
    assert evidence.score == expected
    # 每一档都能追溯到具体 β / t / R² / 样本 / 出处
    assert evidence.beta is not None and evidence.t is not None
    assert evidence.r2 is not None and evidence.sample_n is not None
    assert evidence.source.startswith("报告 §")
    assert evidence.score == score_from_t(evidence.t)


@pytest.mark.parametrize("key", ZERO_WEIGHT_KEYS)
def test_zero_weight_categories_have_reason(key):
    evidence = EVIDENCE[key]
    assert evidence.score == 0
    assert evidence.zero_reason


def test_europe_is_zero_despite_significant_single_variable_t():
    # 单变量 t=4.0 本可得分，但控制标普后不显著，强制归零
    assert score_from_t(4.0) == 59
    assert EVIDENCE["europe"].score == 0
    assert "t=1.9" in EVIDENCE["europe"].zero_reason


def test_commodities_keep_same_day_beta_but_score_zero():
    for key in ("commodity_copper", "commodity_gold", "commodity_lithium", "commodity_rebar", "commodity_oil"):
        evidence = EVIDENCE[key]
        assert evidence.score == 0
        assert evidence.beta is not None and evidence.t is not None  # 同期 β/t 仅作参考保留
        assert "滞后一日" in evidence.zero_reason
    assert EVIDENCE["commodity_copper"].t == 6.9  # 不再是满分标尺


def test_routing_us_index_and_priority():
    result = triage_news_item({"title": "标普500隔夜大涨2%"})
    assert result.category == "us_index_spx"
    assert result.score == 96
    assert result.direction == 1
    assert result.priority == 96
    assert result.display_only is False


def test_routing_chain_before_broad_index():
    result = triage_news_item({"title": "纳指大涨，英伟达创新高"})
    assert result.category == "us_chain"
    assert result.mapped_sw_l1 == ("电子", "汽车")
    assert result.priority == 100


def test_routing_commodity_is_display_only_by_lag_non_significance():
    result = triage_news_item({"title": "沪铜价格创新高"})
    assert result.category == "commodity_copper"
    assert result.mapped_sw_l1 == ("有色金属",)
    assert result.score == 0
    assert result.priority == 0
    assert result.display_only is True
    assert result.evidence["beta"] == 0.577 and result.evidence["t"] == 6.9


def test_direction_gate_zeroes_priority_but_keeps_score():
    result = triage_news_item({"title": "标普500指数成分调整"})
    assert result.score == 96
    assert result.direction == 0
    assert result.priority == 0
    assert result.display_only is True


def test_lpr_and_europe_are_display_only_zero():
    lpr = triage_news_item({"title": "LPR下调10个基点"})
    assert lpr.category == "macro_lpr"
    assert lpr.score == 0 and lpr.priority == 0 and lpr.display_only is True
    assert lpr.keep is True

    europe = triage_news_item({"title": "欧洲股市下跌"})
    assert europe.category == "europe"
    assert europe.score == 0 and europe.priority == 0 and europe.display_only is True


def test_geopolitics_kept_only_with_energy_supply():
    dropped = triage_news_item({"title": "俄乌冲突升级"})
    assert dropped.category == "geopolitics"
    assert dropped.keep is False and dropped.drop_reason == "geopolitics"

    kept = triage_news_item({"title": "俄乌冲突导致原油供应中断，油价大涨"})
    assert kept.category == "commodity_oil"
    assert kept.keep is True and kept.score == 0 and kept.priority == 0


@pytest.mark.parametrize(
    "title,category",
    [
        ("这家公司业绩大涨", "anonymous"),
        ("【风口研报】某行业景气度上行", "column"),
        ("财经早知道：隔夜市场汇总", "digest"),
        ("台风登陆沿海地区", "nonmarket"),
        ("印度股市大涨", "other_region"),
    ],
)
def test_hard_drop_rules(title, category):
    result = triage_news_item({"title": title})
    assert result.category == category
    assert result.keep is False
    assert result.drop_reason


def test_triage_many_sorts_by_priority_desc():
    items = [
        {"title": "标普500指数成分调整"},        # score 96, priority 0（无方向）
        {"title": "标普500隔夜大涨2%"},          # priority 96
        {"title": "纳指大涨，英伟达创新高"},      # priority 100
    ]
    results = triage_many(items)
    assert [item.priority for item in results] == [100, 96, 0]
    assert results[0].category == "us_chain"


def test_mapped_stock_codes_and_dict_serialization():
    matcher = AStockNameMatcher({"600519": "贵州茅台"})
    result = triage_news_item({"title": "标普500大涨，贵州茅台ADR走高"}, name_matcher=matcher)
    assert result.mapped_stock_codes == ("600519",)
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "us_index_spx" in payload
    assert result.score == 96
    assert result.to_dict()["evidence"]["t"] == 6.5


def test_triage_is_not_consumed_by_agent_or_prompts():
    root = Path(__file__).resolve().parents[1] / "src" / "agent"
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".txt", ".jinja", ".j2"}:
            continue
        if "no_stock_triage" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
