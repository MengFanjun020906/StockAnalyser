# -*- coding: utf-8 -*-
"""Tests for Agent search tool news persistence."""

import unittest
from types import SimpleNamespace
import sys
import types
from unittest.mock import MagicMock, patch

from src.agent.tools.search_tools import (
    _handle_get_cls_telegraph_news,
    _handle_get_macro_finance_news,
    _handle_get_xueqiu_hot_news,
    _handle_search_comprehensive_intel,
    _handle_search_openinvest_news,
    _handle_score_stock_news_sentiment,
    _handle_search_stock_prompt_intel,
    _handle_search_stock_news,
    get_cls_telegraph_news_tool,
    get_macro_finance_news_tool,
    get_xueqiu_hot_news_tool,
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
    def test_get_cls_telegraph_news_normalizes_orz_dailynews_response(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "200",
            "msg": "success",
            "data": [
                {
                    "title": "[电报解读] 三星第三季DRAM拟提价20%",
                    "url": "https://www.cls.cn/telegraph",
                    "content": "三星第三季DRAM拟提价20%，国产存储厂商关注升温。",
                    "source": "cls",
                    "publish_time": "2026-07-04 11:11:41",
                    "score": 997,
                    "rank": 4,
                }
            ],
        }

        with patch("src.agent.tools.search_tools.requests.get", return_value=response) as requests_get:
            result = _handle_get_cls_telegraph_news(limit=5, keyword="DRAM", timeout_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "orz.dailynews.cls")
        self.assertEqual(result["query_url"], "https://orz.ai/api/v1/dailynews/?platform=cls")
        self.assertEqual(result["results_count"], 1)
        item = result["results"][0]
        self.assertEqual(item["id"], "4")
        self.assertEqual(item["url"], "https://www.cls.cn/telegraph")
        self.assertEqual(item["published_at"], "2026-07-04T11:11:41+08:00")
        self.assertEqual(item["score"], 997.0)
        self.assertEqual(item["rank"], 4)
        self.assertTrue(item["is_important"])
        self.assertIn(get_cls_telegraph_news_tool, ALL_SEARCH_TOOLS)
        self.assertEqual(requests_get.call_args.kwargs["params"], {"platform": "cls"})

    def test_get_cls_telegraph_news_filters_important_items(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "200",
            "data": [
                {"title": "普通快讯", "content": "普通", "publish_time": "2026-07-04 10:00:00", "rank": 16, "score": 20},
                {"title": "重要快讯", "content": "重要", "publish_time": "2026-07-04 10:01:00", "rank": 2, "score": 998},
            ],
        }

        with patch("src.agent.tools.search_tools.requests.get", return_value=response):
            result = _handle_get_cls_telegraph_news(limit=5, important_only=True, timeout_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results_count"], 1)
        self.assertEqual(result["results"][0]["id"], "2")

    def test_get_xueqiu_hot_news_normalizes_orz_dailynews_response(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "200",
            "msg": "success",
            "data": [
                {
                    "title": "人型机器人商业化落地加速",
                    "url": "https://xueqiu.com/",
                    "content": "机器人利好催化密集，相关 ETF 盘中走强。",
                    "source": "xueqiu",
                    "publish_time": "2026-07-04 11:11:40",
                    "score": 17,
                    "rank": 5,
                },
                {
                    "title": "半导体材料板块走弱",
                    "url": "https://xueqiu.com/",
                    "content": "半导体材料板块震荡下挫。",
                    "source": "xueqiu",
                    "publish_time": "2026-07-04 11:11:40",
                    "score": 157,
                    "rank": 7,
                },
            ],
        }

        with patch("src.agent.tools.search_tools.requests.get", return_value=response) as requests_get:
            result = _handle_get_xueqiu_hot_news(limit=10, keyword="半导体", min_score=100, timeout_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "orz.dailynews.xueqiu")
        self.assertEqual(result["query_url"], "https://orz.ai/api/v1/dailynews/?platform=xueqiu")
        self.assertEqual(result["results_count"], 1)
        self.assertEqual(result["results"][0]["title"], "半导体材料板块走弱")
        self.assertEqual(result["results"][0]["source"], "雪球热榜")
        self.assertIn(get_xueqiu_hot_news_tool, ALL_SEARCH_TOOLS)
        self.assertEqual(requests_get.call_args.kwargs["params"], {"platform": "xueqiu"})

    def test_get_macro_finance_news_filters_macro_items_from_finance_platforms(self) -> None:
        sina_response = MagicMock()
        sina_response.raise_for_status.return_value = None
        sina_response.json.return_value = {
            "status": "200",
            "data": [
                {
                    "title": "美国6月非农数据超预期 美联储降息预期降温",
                    "content": "非农就业数据强于预期，美元指数走强。",
                    "source": "sina_finance",
                    "publish_time": "2026-07-04 09:10:00",
                    "rank": 1,
                },
                {
                    "title": "某公司发布新品",
                    "content": "新品发布。",
                    "source": "sina_finance",
                    "publish_time": "2026-07-04 09:09:00",
                    "rank": 2,
                },
                {
                    "title": "MiniMax M2.7 模型升级",
                    "content": "模型版本升级，推理效率提升。",
                    "source": "sina_finance",
                    "publish_time": "2026-07-04 09:08:00",
                    "rank": 3,
                },
            ],
        }
        eastmoney_response = MagicMock()
        eastmoney_response.raise_for_status.return_value = None
        eastmoney_response.json.return_value = {
            "status": "200",
            "data": [
                {
                    "title": "央行开展5000亿元逆回购操作",
                    "content": "公开市场净投放，维护流动性合理充裕。",
                    "source": "eastmoney",
                    "publish_time": "2026-07-04 09:05:00",
                    "rank": 1,
                },
                {
                    "title": "瀚银科技被罚没近7445万元",
                    "content": "中国人民银行上海市分行披露行政处罚决定。",
                    "source": "eastmoney",
                    "publish_time": "2026-07-04 09:04:00",
                    "rank": 2,
                }
            ],
        }

        with patch("src.agent.tools.search_tools.requests.get", side_effect=[sina_response, eastmoney_response]) as requests_get:
            result = _handle_get_macro_finance_news(limit=10, include_search_fallback=False, timeout_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "orz.dailynews.macro_finance")
        self.assertEqual(result["platforms"], ["sina_finance", "eastmoney"])
        self.assertEqual(result["results_count"], 2)
        self.assertEqual({item["provider"] for item in result["results"]}, {"orz.dailynews.sina_finance", "orz.dailynews.eastmoney"})
        self.assertTrue(all(item["is_macro"] for item in result["results"]))
        self.assertIn("非农", result["results"][0]["macro_keywords"])
        self.assertNotIn("MiniMax", " ".join(item["title"] for item in result["results"]))
        self.assertNotIn("瀚银科技", " ".join(item["title"] for item in result["results"]))
        self.assertIn(get_macro_finance_news_tool, ALL_SEARCH_TOOLS)
        self.assertEqual(requests_get.call_args_list[0].kwargs["params"], {"platform": "sina_finance"})
        self.assertEqual(requests_get.call_args_list[1].kwargs["params"], {"platform": "eastmoney"})

    def test_get_macro_finance_news_uses_search_fallback_when_dailynews_has_no_macro(self) -> None:
        empty_response = MagicMock()
        empty_response.raise_for_status.return_value = None
        empty_response.json.return_value = {"status": "200", "data": []}
        service = SimpleNamespace(
            is_available=True,
            search_general_news=MagicMock(side_effect=[
                SearchResponse(
                    query="美国 非农 就业数据 美联储 最新",
                    provider="UnitSearch",
                    success=True,
                    results=[
                        SearchResult(
                            title="美国6月非农就业数据超预期",
                            snippet="非农就业数据强于市场预期，美联储降息预期降温。",
                            url="https://example.test/nfp",
                            source="example",
                            published_date="2026-07-04",
                        )
                    ],
                ),
                SearchResponse(query="q2", provider="UnitSearch", success=True, results=[]),
                SearchResponse(query="q3", provider="UnitSearch", success=True, results=[]),
                SearchResponse(query="q4", provider="UnitSearch", success=True, results=[]),
            ]),
        )

        with patch("src.agent.tools.search_tools.requests.get", return_value=empty_response), \
             patch("src.agent.tools.search_tools._get_search_service", return_value=service):
            result = _handle_get_macro_finance_news(limit=5, timeout_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["include_search_fallback"])
        self.assertEqual(result["results_count"], 1)
        self.assertEqual(result["results"][0]["provider"], "search_general_news:UnitSearch")
        self.assertIn("非农", result["results"][0]["macro_keywords"])
        service.search_general_news.assert_called()

    def test_get_cls_telegraph_news_reports_structured_error(self) -> None:
        with patch(
            "src.agent.tools.search_tools._run_search_task_with_timeout",
            return_value=(None, "cls_telegraph timeout", 1000),
        ):
            result = _handle_get_cls_telegraph_news(limit=5, timeout_seconds=1)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["source_chain"][0]["result"], "error")
        self.assertEqual(result["errors"], ["cls_telegraph timeout"])

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
