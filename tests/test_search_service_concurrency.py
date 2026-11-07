# -*- coding: utf-8 -*-
"""Concurrency regression tests for search service shared state."""

import sys
import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Mock newspaper before search_service import (optional dependency)
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

from src.search_service import (
    BaseSearchProvider,
    SearchResponse,
    SearchResult,
    SearchService,
    get_search_service,
    reset_search_service,
)


class _ThreadUnsafeCycle:
    def __init__(self, values):
        self._values = list(values)
        self._index = 0
        self._active = False

    def __next__(self):
        if self._active:
            raise AssertionError("concurrent cycle access")
        self._active = True
        try:
            time.sleep(0.05)
            value = self._values[self._index % len(self._values)]
            self._index += 1
            return value
        finally:
            self._active = False


class _DummyProvider(BaseSearchProvider):
    def __init__(self, api_keys):
        super().__init__(api_keys, "DummyProvider")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        return SearchResponse(
            query=query,
            results=[
                SearchResult(
                    title=f"{api_key}:{query}",
                    snippet="snippet",
                    url=f"https://example.com/{api_key}",
                    source="example.com",
                    published_date=datetime.now().date().isoformat(),
                )
            ],
            provider=self.name,
            success=True,
        )


class SearchServiceConcurrencyTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        reset_search_service()

    def test_get_cached_or_reserve_prefers_cached_response(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        cache_key = "cached-query|3|3"
        response = SearchResponse(
            query="cached-query",
            results=[
                SearchResult(
                    title="cached-news",
                    snippet="snippet",
                    url="https://example.com/cached-news",
                    source="example.com",
                    published_date=datetime.now().date().isoformat(),
                )
            ],
            provider="Cache",
            success=True,
        )
        service._put_cache(cache_key, response)

        cached, owner, event = service._get_cached_or_reserve(cache_key)

        self.assertIs(cached, response)
        self.assertFalse(owner)
        self.assertIsNone(event)
        self.assertNotIn(cache_key, service._cache_inflight)

    def test_provider_key_rotation_is_serialized(self):
        provider = _DummyProvider(["key-1", "key-2"])
        provider._key_cycle = _ThreadUnsafeCycle(["key-1", "key-2"])

        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=1)
                provider.search("query", max_results=1)
            except Exception as exc:  # pragma: no cover - thread collection
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(sum(provider._key_usage.values()), 2)

    def test_search_stock_news_coalesces_concurrent_cache_fill(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )

        call_count = 0
        call_lock = threading.Lock()

        def provider_search(query, max_results, days=7, **_kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
            time.sleep(0.05)
            return SearchResponse(
                query=query,
                results=[
                    SearchResult(
                        title="fresh-news",
                        snippet="贵州茅台最新消息",
                        url="https://example.com/fresh-news",
                        source="example.com",
                        published_date=datetime.now().date().isoformat(),
                    )
                ],
                provider="MockProvider",
                success=True,
            )

        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(side_effect=provider_search),
        )
        service._providers = [provider]

        barrier = threading.Barrier(4)
        errors = []
        responses = []

        def worker():
            try:
                barrier.wait(timeout=1)
                responses.append(service.search_stock_news("600519", "贵州茅台", max_results=3))
            except Exception as exc:  # pragma: no cover - thread collection
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(call_count, 1)
        self.assertEqual(len(responses), 4)
        for response in responses:
            self.assertTrue(response.success)
            self.assertEqual([item.title for item in response.results], ["fresh-news"])

    def test_search_stock_news_rechecks_cache_after_wait_before_provider_search(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        search_days = 30
        cache_key = service._cache_key(
            "贵州茅台 600519 新闻 公告 业绩 经营 监管|news_pref=zh|adaptive=7,14,30|relevance=v2",
            3,
            search_days,
        )
        cached_response = SearchResponse(
            query="贵州茅台 600519 股票 最新消息",
            results=[
                SearchResult(
                        title="cached-after-wait",
                        snippet="贵州茅台最新消息",
                    url="https://example.com/cached-after-wait",
                    source="example.com",
                    published_date=datetime.now().date().isoformat(),
                )
            ],
            provider="Cache",
            success=True,
        )
        service._cache_inflight[cache_key] = threading.Event()
        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(side_effect=AssertionError("provider search should not run after cache fills")),
        )
        service._providers = [provider]

        def wait_for_cached(key, _event):
            self.assertEqual(key, cache_key)
            service._put_cache(cache_key, cached_response)
            return None

        with patch.object(service, "_wait_for_cached", side_effect=wait_for_cached):
            response = service.search_stock_news("600519", "贵州茅台", max_results=3)

        self.assertIs(response, cached_response)
        provider.search.assert_not_called()

    def test_search_stock_news_expands_verified_window_from_7_to_14_days(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        today = datetime.now().date()

        def provider_search(query, max_results, days=7, **_kwargs):
            published = today if days == 14 else today.replace(year=today.year - 1)
            return SearchResponse(
                query=query,
                results=[
                    SearchResult(
                        title=f"window-{days}",
                        snippet="贵州茅台公司新闻",
                        url=f"https://example.com/{days}",
                        source="example.com",
                        published_date=published.isoformat(),
                    )
                ],
                provider="MockProvider",
                success=True,
            )

        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(side_effect=provider_search),
        )
        service._providers = [provider]

        response = service.search_stock_news("600519", "贵州茅台", max_results=3)

        self.assertTrue(response.success)
        self.assertEqual(response.search_window_days, 14)
        self.assertEqual([item.title for item in response.results], ["window-14"])
        self.assertEqual(
            [call.kwargs["days"] for call in provider.search.call_args_list],
            [7, 14],
        )
        self.assertEqual([item["window_days"] for item in response.window_attempts], [7, 14])

    def test_search_stock_news_drops_results_unrelated_to_target_stock(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        today = datetime.now().date().isoformat()
        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(return_value=SearchResponse(
                query="天通股份 600330 股票 最新消息",
                results=[
                    SearchResult(
                        title="天通股份获融资买入",
                        snippet="天通股份交易活跃。",
                        url="https://example.com/relevant",
                        source="example.com",
                        published_date=today,
                    ),
                    SearchResult(
                        title="另一家公司控制权拟变更",
                        snippet="证券代码300333。",
                        url="https://example.com/unrelated",
                        source="example.com",
                        published_date=today,
                    ),
                ],
                provider="MockProvider",
                success=True,
            )),
        )
        service._providers = [provider]

        response = service.search_stock_news("600330", "天通股份", max_results=5)

        self.assertEqual([item.title for item in response.results], ["天通股份获融资买入"])
        self.assertEqual(response.window_attempts[0]["dropped_irrelevant_count"], 1)

    def test_search_stock_news_skips_static_pages_and_expands_window(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        today = datetime.now().date().isoformat()

        def provider_search(query, max_results, days=7, **_kwargs):
            title = (
                "天通股份(600330)股票股价, 市值, 实时行情, 走势图, 财报"
                if days == 7
                else "天通股份获融资净买入1.28亿元"
            )
            return SearchResponse(
                query=query,
                results=[
                    SearchResult(
                        title=title,
                        snippet="天通股份600330相关内容",
                        url=f"https://example.com/{days}",
                        source="example.com",
                        published_date=today,
                    ),
                    *(
                        [SearchResult(
                            title="天通股份(SH600330)股票股价",
                            snippet="天通股份600330行情入口",
                            url="https://example.com/quote",
                            source="example.com",
                            published_date=today,
                        )]
                        if days == 7
                        else []
                    ),
                ],
                provider="MockProvider",
                success=True,
            )

        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(side_effect=provider_search),
        )
        service._providers = [provider]

        response = service.search_stock_news("600330", "天通股份", max_results=5)

        self.assertEqual(response.search_window_days, 14)
        self.assertEqual([item.title for item in response.results], ["天通股份获融资净买入1.28亿元"])
        self.assertEqual(response.window_attempts[0]["dropped_non_news_count"], 2)
        self.assertEqual([call.kwargs["days"] for call in provider.search.call_args_list], [7, 14])

    def test_search_stock_news_preserves_undated_candidates_as_unverified(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
        )
        provider = SimpleNamespace(
            is_available=True,
            name="MockProvider",
            search=MagicMock(return_value=SearchResponse(
                query="测试股份 新闻",
                results=[
                    SearchResult(
                        title="测试股份发布经营进展",
                        snippet="搜索结果没有提供可解析的发布日期。",
                        url="https://example.com/undated",
                        source="example.com",
                        published_date=None,
                    )
                ],
                provider="MockProvider",
                success=True,
            )),
        )
        service._providers = [provider]

        response = service.search_stock_news("600001", "测试股份", max_results=3)

        self.assertTrue(response.success)
        self.assertEqual(response.result_state, "date_unverified")
        self.assertEqual(response.results, [])
        self.assertEqual(len(response.unverified_results), 1)
        self.assertEqual([item["window_days"] for item in response.window_attempts], [7, 14, 30])

    def test_get_search_service_initializes_singleton_once(self):
        reset_search_service()
        config = SimpleNamespace(
            bocha_api_keys=[],
            tavily_api_keys=[],
            brave_api_keys=[],
            serpapi_keys=[],
            minimax_api_keys=[],
            searxng_base_urls=[],
            searxng_public_instances_enabled=False,
            news_max_age_days=3,
            news_strategy_profile="short",
            anspire_api_keys=[],
        )

        created = []

        def build_service(**kwargs):
            time.sleep(0.05)
            service = SimpleNamespace(kwargs=kwargs)
            created.append(service)
            return service

        barrier = threading.Barrier(4)
        errors = []
        services = []

        with patch("src.search_service.SearchService", side_effect=build_service) as mock_cls:
            with patch("src.config.get_config", return_value=config):
                def worker():
                    try:
                        barrier.wait(timeout=1)
                        services.append(get_search_service())
                    except Exception as exc:  # pragma: no cover - thread collection
                        errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(mock_cls.call_count, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(len({id(service) for service in services}), 1)


if __name__ == "__main__":
    unittest.main()
