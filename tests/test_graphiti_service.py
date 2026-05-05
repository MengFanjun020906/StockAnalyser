# -*- coding: utf-8 -*-
"""Tests for the Graphiti integration wrapper."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.services.graphiti.graph_service import GraphitiService
from src.services.graphiti.ontology import DEFAULT_ENTITY_TYPES, validate_entity_types


class GraphitiServiceTestCase(unittest.TestCase):
    def test_disabled_service_search_returns_disabled_error(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            mock_get_config.return_value.graphiti_enabled = False
            service = GraphitiService()

        result = service.search_sync("贵州茅台")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Graphiti is disabled")

    def test_ingest_analysis_serializes_episode_body(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "single"
            service = GraphitiService()

        service.enabled = True
        service._client = AsyncMock()

        asyncio.run(service.ingest_analysis(
            code="600519",
            stock_name="贵州茅台",
            report_type="full",
            result={"decision_type": "hold"},
            context={"market": "cn"},
            news_context="news",
        ))

        kwargs = service._client.add_episode.await_args.kwargs
        self.assertEqual(kwargs["group_id"], "daily_stock_analysis")
        self.assertIsInstance(kwargs["episode_body"], str)
        self.assertIn("600519", kwargs["episode_body"])

    def test_market_group_id_uses_graphiti_safe_characters(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        self.assertEqual(service._resolve_group_id(market="cn/a"), "market_cn_a")

    def test_ingest_trace_serializes_episode_body(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = AsyncMock()

        asyncio.run(service.ingest_trace(
            session_id="trace-123",
            trace_type="single_stock_analysis",
            title="芯联集成",
            result={"success": True, "content": "ok"},
            context={"stock_code": "688469", "market": "cn"},
            artifact_dir="/tmp/trace",
            market="cn",
            user_id="42",
        ))

        kwargs = service._client.add_episode.await_args.kwargs
        self.assertEqual(kwargs["group_id"], "market_cn")
        self.assertEqual(kwargs["source_description"], "agent_trace")
        self.assertIn("trace-123", kwargs["episode_body"])

    def test_ingest_trace_initializes_indices_before_episode_write(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = AsyncMock()

        asyncio.run(service.ingest_trace(
            session_id="trace-idx",
            trace_type="single_stock_analysis",
            title="芯联集成",
            result={"success": True},
            context={"stock_code": "688469"},
        ))

        service._client.build_indices_and_constraints.assert_awaited_once()
        service._client.add_episode.assert_awaited_once()

    def test_search_initializes_indices_before_query(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        search_result = type("SearchResult", (), {"edges": [], "nodes": [], "episodes": []})()
        service.enabled = True
        service._client = AsyncMock()
        service._client.search_.return_value = search_result

        result = asyncio.run(service.search("芯联集成 688469"))

        self.assertTrue(result["success"])
        service._client.build_indices_and_constraints.assert_awaited_once()
        service._client.search_.assert_awaited_once()

    def test_default_entity_types_do_not_conflict_with_graphiti_reserved_fields(self):
        validate_entity_types(DEFAULT_ENTITY_TYPES)


if __name__ == "__main__":
    unittest.main()
