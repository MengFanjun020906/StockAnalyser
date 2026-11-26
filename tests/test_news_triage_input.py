# -*- coding: utf-8 -*-
"""实时搜索新闻接入 triage：排序、报告字段与当日摘要。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.services import news_triage_input as nti


def _items() -> list[dict]:
    return [
        {"title": "某公司发布日常经营公告", "snippet": "公司与客户签订框架协议", "source": "测试"},
        {"title": "纳斯达克100指数收涨1.2%", "snippet": "科技股走强", "source": "测试"},
        {"title": "英伟达发布新一代AI芯片", "snippet": "产业链订单增长", "source": "测试"},
        {"title": "标普500指数创新高", "snippet": "美股三大指数收涨", "source": "测试"},
    ]


def test_sort_disabled_keeps_order_and_payload() -> None:
    items = _items()
    result = nti.annotate_and_sort_items(items, enabled=False)

    assert result == items
    assert all("news_triage" not in item for item in result)
    # 关闭时返回原对象，调用方顺序与内容完全不变
    assert result[0] is items[0]


def test_sort_enabled_is_stable_and_sinks_zero_scores() -> None:
    result = nti.annotate_and_sort_items(_items(), enabled=True)

    assert len(result) == 4
    keys = [
        (item["news_triage"]["priority"], item["news_triage"]["score"])
        for item in result
    ]
    assert keys == sorted(keys, reverse=True)
    assert result[-1]["news_triage"]["score"] == 0
    assert result[-1]["title"].startswith("某公司")
    # 原始输入不被修改
    assert all("news_triage" not in item for item in _items())


def test_report_and_summary_carry_channel_score() -> None:
    result = nti.annotate_and_sort_items(_items(), enabled=True)
    report = nti.build_news_report(result)
    summary = nti.build_triage_summary(result)

    assert "us_chain/100" in report
    assert "us_chain/100" in summary
    assert report.splitlines()[0].startswith("1. [")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "MAX(signal_date)" in sql:
            return _FakeResult([("2026-11-26",)])
        return _FakeResult(self._rows)


def test_market_env_brief_groups_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ("英伟达发布新一代AI芯片", json.dumps({"channel": "us_chain", "priority": 100})),
        ("AMD涨超6%", json.dumps({"channel": "us_chain", "priority": 100})),
        ("纳斯达克100指数收涨", json.dumps({"channel": "us_index", "priority": 82})),
    ]

    class _FakeManager:
        def get_session(self):
            return _FakeSession(rows)

    import src.storage as storage

    monkeypatch.setattr(storage.DatabaseManager, "get_instance", classmethod(lambda cls: _FakeManager()))

    brief = nti.build_market_env_brief()

    assert "2026-11-26" in brief
    assert "no_stock 高优先级 3 条" in brief
    assert "- us_chain(2)：" in brief
    assert "不参与评分、阈值和点位" in brief


def test_handler_off_is_unchanged_and_on_populates_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import search_tools

    response = SimpleNamespace(
        success=True,
        results=[
            SimpleNamespace(title="某公司日常公告", published_date="2026-11-26", source="测试", snippet=""),
            SimpleNamespace(title="英伟达发布AI芯片", published_date="2026-11-26", source="测试", snippet=""),
        ],
    )
    intel_results = {"latest_news": response}

    monkeypatch.setattr(
        search_tools,
        "_get_search_service",
        lambda: SimpleNamespace(is_available=True, search_comprehensive_intel=lambda **kwargs: intel_results),
    )
    monkeypatch.setattr(search_tools, "_persist_news_response", lambda **kwargs: None)
    monkeypatch.setattr(
        search_tools,
        "_preprocess_intel_with_llm",
        lambda items, name, timeout_seconds=4.0: {
            "items": [{"title": item["title"]} for item in items],
            "key_signals": [],
            "overall_sentiment": "unknown",
        },
    )

    monkeypatch.setattr(search_tools, "_news_triage_sort_enabled", lambda: False)
    off = search_tools._handle_search_comprehensive_intel("600519", "贵州茅台")
    assert "report" not in off and "summary" not in off
    assert [item["title"] for item in off["intel"]["items"]] == ["某公司日常公告", "英伟达发布AI芯片"]

    monkeypatch.setattr(search_tools, "_news_triage_sort_enabled", lambda: True)
    on = search_tools._handle_search_comprehensive_intel("600519", "贵州茅台")
    assert on["news_triage_sorted"] is True
    assert "英伟达" in on["report"]
    assert "us_chain/100" in on["report"]
    assert on["intel"]["items"][0]["title"] == "英伟达发布AI芯片"
