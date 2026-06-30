# -*- coding: utf-8 -*-
"""Tests for Agent search tool news persistence."""

import unittest
from types import SimpleNamespace
import sys
import types
from unittest.mock import MagicMock, patch

from src.agent.tools.search_tools import (
    _handle_search_comprehensive_intel,
    _handle_search_openinvest_news,
    _handle_score_stock_news_sentiment,
    _handle_search_stock_prompt_intel,
    _handle_search_stock_news,
    search_stock_prompt_intel_tool,
    ALL_SEARCH_TOOLS,
)
from src.search_service import SearchResponse, SearchResult


def _response(query: str, *, success: bool = True) -> SearchResponse:
    return SearchResponse(
        query=query,
        provider="UnitSearch",
        success=success,
        error_message=None if success else "search failed",
        results=[
            SearchResult(
                title="新闻标题",
                snippet="新闻摘要",
                url="https://example.com/news",
                source="example.com",
                published_date="2026-04-24",
            )
        ] if success else [],
    )


class SearchToolsPersistenceTest(unittest.TestCase):
    def test_search_stock_news_persists_successful_response(self) -> None:
        response = _response("贵州茅台 600519 latest news")
        service = SimpleNamespace(
            is_available=True,
            search_stock_news=MagicMock(return_value=response),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(return_value=1))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_news("600519", "贵州茅台")

        self.assertTrue(result["success"])
        db.save_news_intel.assert_called_once_with(
            code="600519",
            name="贵州茅台",
            dimension="latest_news",
            query=response.query,
            response=response,
            query_context=None,
        )

    def test_search_openinvest_news_normalizes_yfinance_items(self) -> None:
        item = SimpleNamespace(
            title="Apple supplier news",
            snippet="Ticker-linked Yahoo item",
            url="https://finance.yahoo.com/news/apple",
            src_name="yfinance:Yahoo Finance",
            published_at="2026-06-12T01:00:00Z",
            fetched_at="2026-06-12T01:00:01Z",
            raw_meta={"symbol": "AAPL"},
        )

        fetch_all = MagicMock(return_value=[item])
        news_sources = types.ModuleType("services.news_sources")
        news_sources.fetch_all = fetch_all
        rss_feed = types.ModuleType("services.news_sources.rss_feed")
        rss_feed.load_default_feeds = MagicMock(return_value=[])
        with patch.dict(sys.modules, {
            "services": types.ModuleType("services"),
            "services.news_sources": news_sources,
            "services.news_sources.rss_feed": rss_feed,
        }), patch("src.agent.tools.search_tools._ensure_openinvest_import_path", return_value=object()):
            result = _handle_search_openinvest_news(
                stock_code="AAPL",
                stock_name="Apple",
                max_results=5,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["results_count"], 1)
        self.assertEqual(result["results"][0]["source"], "yfinance:Yahoo Finance")
        self.assertEqual(fetch_all.call_args.kwargs["symbols"], ["AAPL"])

    def test_search_openinvest_news_reports_missing_ddgs_dependency_without_failing(self) -> None:
        fetch_all = MagicMock(return_value=[])
        news_sources = types.ModuleType("services.news_sources")
        news_sources.fetch_all = fetch_all
        rss_feed = types.ModuleType("services.news_sources.rss_feed")
        rss_feed.load_default_feeds = MagicMock(return_value=[])
        with patch.dict(sys.modules, {
            "services": types.ModuleType("services"),
            "services.news_sources": news_sources,
            "services.news_sources.rss_feed": rss_feed,
        }), patch("src.agent.tools.search_tools._ensure_openinvest_import_path", return_value=object()):
            result = _handle_search_openinvest_news(
                stock_code="600519",
                stock_name="贵州茅台",
                include_ddgs=True,
                include_yfinance=False,
                max_results=3,
            )

        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["symbol"], "600519.SS")
        self.assertEqual(fetch_all.call_args.kwargs["queries"], [])
        self.assertTrue(
            any(item.get("result") == "missing_dependency" for item in result["source_chain"])
        )

    def test_search_stock_prompt_intel_searches_user_prompt_with_stock_anchor(self) -> None:
        response = _response("沪硅产业 688126 有什么公告 走势 公告 投资者关系 年报")
        service = SimpleNamespace(
            is_available=True,
            search_general_news=MagicMock(return_value=response),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(return_value=1))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_prompt_intel(
                "688126",
                "沪硅产业",
                "有什么公告，你看看走势",
                days=45,
                max_results=4,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results_count"], 1)
        service.search_general_news.assert_called_once()
        query_arg = service.search_general_news.call_args.args[0]
        self.assertIn("沪硅产业", query_arg)
        self.assertIn("688126", query_arg)
        self.assertIn("公告", query_arg)
        self.assertIn("走势", query_arg)
        self.assertEqual(service.search_general_news.call_args.kwargs["days"], 45)
        self.assertEqual(service.search_general_news.call_args.kwargs["max_results"], 4)
        db.save_news_intel.assert_called_once_with(
            code="688126",
            name="沪硅产业",
            dimension="prompt_intel",
            query=response.query,
            response=response,
            query_context=None,
        )
        self.assertIn(search_stock_prompt_intel_tool, ALL_SEARCH_TOOLS)

    def test_search_stock_prompt_intel_reports_unavailable_search(self) -> None:
        service = SimpleNamespace(is_available=False)
        db = SimpleNamespace(save_news_intel=MagicMock())

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_prompt_intel(
                "688126",
                "沪硅产业",
                "有没有投资者关系活动记录表",
            )

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["results"], [])
        self.assertIn("No search engine", result["errors"][0])
        db.save_news_intel.assert_not_called()

    def test_search_stock_prompt_intel_reports_failed_search_without_persisting(self) -> None:
        response = _response("沪硅产业 公告", success=False)
        service = SimpleNamespace(
            is_available=True,
            search_general_news=MagicMock(return_value=response),
        )
        db = SimpleNamespace(save_news_intel=MagicMock())

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_prompt_intel("688126", "沪硅产业", "有什么公告")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results_count"], 0)
        self.assertEqual(result["errors"], ["search failed"])
        db.save_news_intel.assert_not_called()

    def test_search_comprehensive_intel_persists_successful_dimensions_only(self) -> None:
        latest = _response("latest")
        failed = _response("risk", success=False)
        service = SimpleNamespace(
            is_available=True,
            search_comprehensive_intel=MagicMock(
                return_value={"latest_news": latest, "risk_check": failed}
            ),
            format_intel_report=MagicMock(return_value="report"),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(return_value=1))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_comprehensive_intel("600519", "贵州茅台")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dimensions_searched"], ["latest_news"])
        db.save_news_intel.assert_called_once_with(
            code="600519",
            name="贵州茅台",
            dimension="latest_news",
            query=latest.query,
            response=latest,
            query_context=None,
        )

    def test_search_comprehensive_intel_agent_tool_uses_bounded_dimensions(self) -> None:
        latest = _response("latest")
        service = SimpleNamespace(
            is_available=True,
            search_comprehensive_intel=MagicMock(return_value={"latest_news": latest}),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(return_value=1))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db), \
             patch(
                 "src.agent.tools.search_tools._preprocess_intel_with_llm",
                 return_value={"items": [], "key_signals": [], "overall_sentiment": "unknown"},
             ):
            result = _handle_search_comprehensive_intel("600519", "贵州茅台")

        service.search_comprehensive_intel.assert_called_once_with(
            stock_code="600519",
            stock_name="贵州茅台",
            max_searches=2,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_chain"][0]["max_searches"], 2)

    def test_persistence_failure_keeps_search_result(self) -> None:
        response = _response("贵州茅台 600519 latest news")
        service = SimpleNamespace(
            is_available=True,
            search_stock_news=MagicMock(return_value=response),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(side_effect=RuntimeError("db locked")))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_news("600519", "贵州茅台")

        self.assertTrue(result["success"])
        self.assertEqual(result["results_count"], 1)

    def test_unavailable_or_failed_search_does_not_persist(self) -> None:
        unavailable = SimpleNamespace(is_available=False)
        db = SimpleNamespace(save_news_intel=MagicMock())
        with patch("src.agent.tools.search_tools._get_search_service", return_value=unavailable), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_news("600519", "贵州茅台")

        self.assertIn("error", result)
        db.save_news_intel.assert_not_called()

        failed = SimpleNamespace(
            is_available=True,
            search_stock_news=MagicMock(return_value=_response("latest", success=False)),
        )
        with patch("src.agent.tools.search_tools._get_search_service", return_value=failed), \
             patch("src.agent.tools.search_tools._get_db", return_value=db):
            result = _handle_search_stock_news("600519", "贵州茅台")

        self.assertFalse(result["success"])
        db.save_news_intel.assert_not_called()

    def test_score_stock_news_sentiment_classifies_company_events(self) -> None:
        response = SearchResponse(
            query="测试股份 600001 公告",
            provider="UnitSearch",
            success=True,
            results=[
                SearchResult(
                    title="测试股份签订重大合同并获得客户定点通知",
                    snippet="公司公告称本次大订单将提升未来收入可见度。",
                    url="https://example.com/order",
                    source="example.com",
                    published_date="2026-05-14",
                ),
                SearchResult(
                    title="测试股份提示股价异动风险",
                    snippet="公司称相关传闻尚未证实。",
                    url="https://example.com/risk",
                    source="example.com",
                    published_date="2026-05-14",
                ),
            ],
        )
        service = SimpleNamespace(
            is_available=True,
            search_stock_news=MagicMock(return_value=response),
        )
        db = SimpleNamespace(save_news_intel=MagicMock(return_value=1))

        with patch("src.agent.tools.search_tools._get_search_service", return_value=service), \
             patch("src.agent.tools.search_tools._get_db", return_value=db), \
             patch(
                 "src.agent.tools.search_tools._fetch_tushare_announcements_result",
                 return_value={
                     "status": "empty",
                     "items": [],
                     "provider": "Tushare.anns_d",
                     "date_window": {"start_date": "20260511", "end_date": "20260514"},
                 },
             ):
            result = _handle_score_stock_news_sentiment("600001", "测试股份")

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["message_score"], 58)
        self.assertEqual(result["positive_count"], 1)
        self.assertEqual(result["uncertain_count"], 2)
        self.assertIn("contract_order", result["event_tags"])
        self.assertIn("rumor_high_heat", result["event_tags"])
        self.assertEqual(result["sources"]["announcements"]["status"], "empty")
        self.assertEqual(result["sources"]["announcements"]["date_window"]["start_date"], "20260511")
        db.save_news_intel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
