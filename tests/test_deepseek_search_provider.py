# -*- coding: utf-8 -*-
"""DeepSeek Search provider 的字段映射与重试策略测试。"""

from __future__ import annotations

import json

import pytest

from src import search_service
from src.search_service import DeepSeekSearchProvider, SearchService


class _FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _ok_body() -> dict:
    return {
        "results": [
            {
                "title": "贵州茅台发布三季报",
                "url": "https://example.com/news/1",
                "snippet": "公司前三季度营收同比增长。",
                "summary": "贵州茅台发布三季报，营收与净利润均实现同比增长，渠道库存保持健康。" * 5,
                "site_name": "示例财经",
                "date": "2026-11-25T08:30:00Z",
                "authority": 3,
                "rerank_score": 2.1,
            }
        ]
    }


def test_maps_deepseek_fields_and_passes_query_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(200, _ok_body())

    monkeypatch.setattr(search_service.requests, "post", fake_post)
    provider = DeepSeekSearchProvider(["sk-test"], base_url="https://api.deepseek.com/", timeout_seconds=9)

    query = "贵州茅台 600519 新闻 site:cls.cn after:2026-11-19 lang:zh"
    response = provider.search(query, max_results=5)

    assert response.success is True
    assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"] == {"query": query, "count": 5}
    assert captured["timeout"] == 9

    item = response.results[0]
    assert item.title == "贵州茅台发布三季报"
    assert item.snippet == "公司前三季度营收同比增长。"
    assert item.summary and len(item.summary) > 100
    assert item.source == "示例财经"
    assert item.site_name == "示例财经"
    assert item.published_date == "2026-11-25"
    assert item.authority == 3.0
    assert item.rerank_score == 2.1


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(url, *, headers, json, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(429, {"error": {"message": "rate limited"}})
        return _FakeResponse(200, _ok_body())

    monkeypatch.setattr(search_service.requests, "post", fake_post)
    monkeypatch.setattr(search_service.time, "sleep", lambda _seconds: None)

    response = DeepSeekSearchProvider(["sk-test"]).search("贵州茅台 新闻", max_results=3)

    assert response.success is True
    assert calls["count"] == 2


def test_does_not_retry_on_402(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(url, *, headers, json, timeout):
        calls["count"] += 1
        return _FakeResponse(402, {"error": {"message": "insufficient balance"}})

    monkeypatch.setattr(search_service.requests, "post", fake_post)

    response = DeepSeekSearchProvider(["sk-test"]).search("贵州茅台 新闻")

    assert response.success is False
    assert calls["count"] == 1
    assert "余额不足" in (response.error_message or "")


def test_does_not_retry_on_other_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(url, *, headers, json, timeout):
        calls["count"] += 1
        return _FakeResponse(400, {"error": {"message": "bad request"}})

    monkeypatch.setattr(search_service.requests, "post", fake_post)

    response = DeepSeekSearchProvider(["sk-test"]).search("贵州茅台 新闻")

    assert response.success is False
    assert calls["count"] == 1
    assert response.error_message == "bad request"


def test_query_is_truncated_and_count_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return _FakeResponse(200, {"results": []})

    monkeypatch.setattr(search_service.requests, "post", fake_post)
    provider = DeepSeekSearchProvider(["sk-test"])

    long_query = "甲" * 200
    response = provider.search(long_query, max_results=999)

    assert response.success is True
    assert len(captured["query"]) == 128
    assert captured["count"] == 50


def test_service_registers_deepseek_first_and_keeps_anysearch_fallback() -> None:
    service = SearchService(
        anysearch_api_key="any-key",
        deepseek_search_api_keys=["sk-deepseek"],
    )

    names = [provider.name for provider in service._providers]
    assert names[0] == "DeepSeek"
    assert "AnySearch" in names


def test_service_keeps_anysearch_when_no_deepseek_key() -> None:
    service = SearchService(anysearch_api_key="any-key", deepseek_search_api_keys=[])

    names = [provider.name for provider in service._providers]
    assert names == ["AnySearch"]
