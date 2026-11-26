# -*- coding: utf-8 -*-
"""Regression tests for pipeline-level related board enrichment."""

import unittest
from unittest.mock import MagicMock

from src.core.pipeline import StockAnalysisPipeline


class PipelineRelatedBoardsTestCase(unittest.TestCase):
    def test_attach_belong_boards_shallow_copies_context_before_injecting(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()
        pipeline.fetcher_manager.get_belong_boards.return_value = [{"name": "白酒", "type": "行业"}]

        cached_context = {
            "market": "cn",
            "status": "ok",
            "coverage": {"boards": "ok"},
            "boards": {"status": "ok", "data": {"top": [], "bottom": []}},
        }

        enriched = pipeline._attach_belong_boards_to_fundamental_context("600519", cached_context)

        self.assertIsNot(enriched, cached_context)
        self.assertNotIn("belong_boards", cached_context)
        self.assertEqual(enriched["belong_boards"], [{"name": "白酒", "type": "行业"}])

    def test_attach_belong_boards_copies_existing_board_list(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()

        existing_boards = [{"name": "白酒", "type": "行业"}]
        context = {
            "market": "cn",
            "status": "ok",
            "belong_boards": existing_boards,
            "coverage": {"boards": "ok"},
            "boards": {"status": "ok", "data": {"top": [], "bottom": []}},
        }

        enriched = pipeline._attach_belong_boards_to_fundamental_context("600519", context)

        self.assertIsNot(enriched, context)
        self.assertEqual(enriched["belong_boards"], existing_boards)
        self.assertIsNot(enriched["belong_boards"], existing_boards)
        pipeline.fetcher_manager.get_belong_boards.assert_not_called()

    def test_attach_belong_boards_skips_provider_for_non_cn(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()

        context = {"market": "us", "status": "not_supported"}
        enriched = pipeline._attach_belong_boards_to_fundamental_context("AAPL", context)

        self.assertEqual(enriched["belong_boards"], [])
        pipeline.fetcher_manager.get_belong_boards.assert_not_called()

    def test_attach_belong_boards_skips_provider_when_board_block_not_supported(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()

        context = {
            "market": "cn",
            "status": "partial",
            "coverage": {"boards": "not_supported"},
            "boards": {"status": "not_supported", "data": {}},
            "errors": ["etf not fully supported"],
        }

        enriched = pipeline._attach_belong_boards_to_fundamental_context("159915", context)

        self.assertEqual(enriched["belong_boards"], [])
        pipeline.fetcher_manager.get_belong_boards.assert_not_called()

    def test_attach_belong_boards_skips_provider_when_pipeline_disabled_payload(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()

        context = {
            "market": "cn",
            "status": "not_supported",
            "coverage": {"boards": "not_supported"},
            "boards": {"status": "not_supported", "data": {}},
            "errors": ["fundamental pipeline disabled"],
        }

        enriched = pipeline._attach_belong_boards_to_fundamental_context("600519", context)

        self.assertEqual(enriched["belong_boards"], [])
        pipeline.fetcher_manager.get_belong_boards.assert_not_called()

    def test_attach_belong_boards_uses_normalized_a_share_code_when_market_missing(self) -> None:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.fetcher_manager = MagicMock()
        pipeline.fetcher_manager.get_belong_boards.return_value = [{"name": "白酒"}]

        context = {
            "status": "ok",
            "coverage": {"boards": "ok"},
            "boards": {"status": "ok", "data": {"top": [], "bottom": []}},
        }

        enriched = pipeline._attach_belong_boards_to_fundamental_context("SH600519", context)

        self.assertEqual(enriched["belong_boards"], [{"name": "白酒"}])
        pipeline.fetcher_manager.get_belong_boards.assert_called_once_with("SH600519")

if __name__ == "__main__":
    unittest.main()


class _FailingBoardFetcher:
    name = "efinance"

    def get_belong_board(self, code):
        raise ConnectionError("('Connection aborted.', RemoteDisconnected(...))")


class BelongBoardsTushareFallbackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from data_provider.base import _TUSHARE_INDUSTRY_BOARD_CACHE

        _TUSHARE_INDUSTRY_BOARD_CACHE.clear()

    def test_falls_back_to_tushare_industry_when_all_fetchers_fail(self) -> None:
        import pandas as pd
        from unittest.mock import patch

        from data_provider.base import DataFetcherManager

        manager = DataFetcherManager.__new__(DataFetcherManager)
        manager._fetchers = [_FailingBoardFetcher()]
        df = pd.DataFrame([{"ts_code": "603986.SH", "name": "兆易创新", "industry": "半导体"}])

        with patch("data_provider.tushare_client.query_tushare_api", return_value=df) as mock_query:
            boards = manager.get_belong_boards("603986")

        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0]["name"], "半导体")
        self.assertTrue(boards[0]["degraded"])
        self.assertEqual(boards[0]["source"], "tushare:stock_basic.industry")
        self.assertEqual(mock_query.call_args.args[0], "stock_basic")

    def test_prefers_primary_boards_when_available(self) -> None:
        from unittest.mock import patch

        from data_provider.base import DataFetcherManager

        class _OkFetcher:
            name = "efinance"

            def get_belong_board(self, code):
                return [{"name": "半导体", "type": "industry"}]

        manager = DataFetcherManager.__new__(DataFetcherManager)
        manager._fetchers = [_OkFetcher()]
        with patch("data_provider.tushare_client.query_tushare_api") as mock_query:
            boards = manager.get_belong_boards("603501")

        self.assertEqual(boards, [{"name": "半导体", "type": "industry"}])
        self.assertFalse(any(b.get("degraded") for b in boards))

    def test_returns_empty_when_tushare_industry_missing(self) -> None:
        import pandas as pd
        from unittest.mock import patch

        from data_provider.base import DataFetcherManager

        manager = DataFetcherManager.__new__(DataFetcherManager)
        manager._fetchers = [_FailingBoardFetcher()]
        df = pd.DataFrame([{"ts_code": "600519.SH", "name": "贵州茅台", "industry": ""}])

        with patch("data_provider.tushare_client.query_tushare_api", return_value=df):
            boards = manager.get_belong_boards("600519")

        self.assertEqual(boards, [])
