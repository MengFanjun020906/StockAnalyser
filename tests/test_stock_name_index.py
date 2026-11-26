# -*- coding: utf-8 -*-
"""A 股扩展名称字典：加载、过滤、回退与匹配。"""

from __future__ import annotations

import pytest

from src.data import stock_name_index as index
from src.services.news_event_foundation import extract_security_refs, normalize_code

CSV_HEADER = "ts_code,name,industry,market,list_date,delist_date,list_status\n"
CSV_ROWS = (
    "600519.SH,贵州茅台,白酒,主板,20010827,,L\n"
    "000001.SZ,平安银行,银行,主板,19910403,,L\n"
    "300024.SZ,机器人,自动化设备,创业板,20091030,,L\n"
    "000004.SZ,国华网安,软件,主板,19940101,20200101,D\n"
    "600000.SH,浦发银行,银行,主板,19991110,,L\n"
    "000005.SZ,世纪,软件,主板,19940101,,L\n"
)


@pytest.fixture(autouse=True)
def _reset_cache():
    index.reset_stock_name_cache()
    yield
    index.reset_stock_name_cache()


def _write_csv(tmp_path, body: str = CSV_ROWS):
    path = tmp_path / "stock_basic_LD.csv"
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def test_load_excludes_stoplist_and_short_names(tmp_path):
    mapping = index.load_a_share_name_map(str(_write_csv(tmp_path)))
    assert mapping["600519"] == "贵州茅台"
    assert mapping["000001"] == "平安银行"
    # 含退市
    assert mapping["000004"] == "国华网安"
    assert "300024" not in mapping  # 通用词
    assert "000005" not in mapping  # 长度 < 3


def test_missing_file_falls_back_to_builtin_a_share_subset(tmp_path):
    mapping = index.load_a_share_name_map(str(tmp_path / "not_exists.csv"))
    assert mapping["600519"] == "贵州茅台"
    assert all(normalize_code(code) == code for code in mapping)


def test_matcher_uses_first_char_index_and_skips_stoplist(tmp_path):
    matcher = index.get_a_share_name_matcher(str(_write_csv(tmp_path)))
    found = dict(matcher.find("贵州茅台与平安银行发布公告，机器人概念走强"))
    assert found == {"600519": "贵州茅台", "000001": "平安银行"}
    assert matcher.name_map["600000"] == "浦发银行"


def test_matcher_dedupes_repeated_names(tmp_path):
    matcher = index.get_a_share_name_matcher(str(_write_csv(tmp_path)))
    matches = list(matcher.find("贵州茅台、贵州茅台、贵州茅台"))
    assert matches == [("600519", "贵州茅台")]


def test_reset_clears_cached_matcher(tmp_path):
    matcher = index.get_a_share_name_matcher(str(_write_csv(tmp_path)))
    assert matcher.name_map["600519"] == "贵州茅台"
    index.reset_stock_name_cache()
    assert index._MATCHER_CACHE == {}


def test_extract_security_refs_custom_matcher_and_default_unchanged():
    matcher = index.AStockNameMatcher({"300999": "宁德新材"})
    text = "宁德新材获批新建产能"

    custom = extract_security_refs(text=text, name_matcher=matcher.find)
    assert custom["scope"] == "single"
    assert custom["codes"] == ["300999"]

    # 不传 name_matcher 时保持旧行为：内置字典没有该名称，仍判 no_stock
    default = extract_security_refs(text=text)
    assert default["scope"] == "no_stock"
