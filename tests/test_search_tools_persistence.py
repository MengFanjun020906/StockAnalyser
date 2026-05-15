# -*- coding: utf-8 -*-
"""Tests for Agent search tool news persistence."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.agent.tools.search_tools import (
    _handle_search_comprehensive_intel,
    _handle_score_stock_news_sentiment,
    _handle_search_stock_news,
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

        self.assertEqual(result["report"], "report")
        self.assertEqual(list(result["dimensions"].keys()), ["latest_news"])
        db.save_news_intel.assert_called_once_with(
            code="600519",
            name="贵州茅台",
            dimension="latest_news",
            query=latest.query,
            response=latest,
            query_context=None,
        )

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
             patch("src.agent.tools.search_tools._fetch_tushare_announcements", return_value=[]):
            result = _handle_score_stock_news_sentiment("600001", "测试股份")

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["message_score"], 58)
        self.assertEqual(result["positive_count"], 1)
        self.assertEqual(result["uncertain_count"], 2)
        self.assertIn("contract_order", result["event_tags"])
        self.assertIn("rumor_high_heat", result["event_tags"])
        db.save_news_intel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
