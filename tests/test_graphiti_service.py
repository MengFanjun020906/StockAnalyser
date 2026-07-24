# -*- coding: utf-8 -*-
"""Tests for the Graphiti integration wrapper."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from graphiti_core.llm_client.config import ModelSize
from graphiti_core.prompts.extract_nodes import ExtractedEntities
from graphiti_core.prompts.models import Message
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF

from src.services.graphiti.graph_service import GraphitiService
from src.services.graphiti.litellm_client import LiteLLMGraphitiClient
from src.services.graphiti.litellm_embedder import LiteLLMGraphitiEmbedder
from src.services.graphiti.ontology import DEFAULT_ENTITY_TYPES, validate_entity_types
from src.services.graphiti.reranker import DeterministicGraphitiReranker


class GraphitiServiceTestCase(unittest.TestCase):
    def test_disabled_service_search_uses_relational_fallback(self):
        fallback = MagicMock()
        fallback.search.return_value = {
            "success": True,
            "source": "relational_fallback",
            "degraded": True,
            "query": "贵州茅台",
            "edges": [],
            "nodes": [],
            "episodes": [{"type": "analysis_history", "code": "600519"}],
        }
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            mock_get_config.return_value.graphiti_enabled = False
            service = GraphitiService(fallback_search=fallback)

        result = service.search_sync("贵州茅台")

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["source"], "relational_fallback")
        fallback.search.assert_called_once_with("贵州茅台", market=None, limit=10, reason="graphiti_disabled")

    def test_litellm_client_uses_json_schema_and_recovers_schema_echo(self):
        seen = {}

        class _Message:
            content = json.dumps(ExtractedEntities.model_json_schema())

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        async def fake_acompletion(**kwargs):
            seen.update(kwargs)
            return _Response()

        with patch("src.services.graphiti.litellm_client.get_config") as mock_get_config, \
                patch("src.services.graphiti.litellm_client.litellm.acompletion", side_effect=fake_acompletion):
            cfg = mock_get_config.return_value
            cfg.graphiti_llm_model = "ollama/qwen3:8b"
            cfg.litellm_model = ""
            cfg.litellm_fallback_models = []
            cfg.llm_temperature = 0.0
            client = LiteLLMGraphitiClient()

            result = asyncio.run(client._generate_response(
                [Message(role="user", content="extract entities")],
                response_model=ExtractedEntities,
                model_size=ModelSize.small,
            ))

        self.assertEqual(result, {"extracted_entities": []})
        self.assertIs(seen["think"], False)
        self.assertEqual(seen["response_format"]["type"], "json_schema")
        self.assertIn("extracted_entities", seen["response_format"]["json_schema"]["schema"]["properties"])
        self.assertIn("Do not return the schema itself", seen["messages"][-1]["content"])

    def test_embedder_falls_back_to_local_hash_without_external_key(self):
        with patch("src.services.graphiti.litellm_embedder.get_config") as mock_get_config, \
                patch.dict("os.environ", {}, clear=True):
            cfg = mock_get_config.return_value
            cfg.graphiti_embedding_model = ""
            cfg.graphiti_embedding_base_url = None
            cfg.graphiti_embedding_api_key = None

            embedder = LiteLLMGraphitiEmbedder()
            first = asyncio.run(embedder.create("储能 出货 增长"))
            second = asyncio.run(embedder.create("储能 出货 增长"))

        self.assertEqual(embedder.model, "local/hash-embedding")
        self.assertEqual(len(first), embedder.config.embedding_dim)
        self.assertEqual(first, second)
        self.assertGreater(sum(abs(item) for item in first), 0)

    def test_enabled_service_uses_deterministic_reranker_without_openai_key(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config, \
                patch("src.services.graphiti.graph_service._can_open_tcp", return_value=(True, "")), \
                patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False), \
                patch("graphiti_core.graphiti.Graphiti") as mock_graphiti:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = True
            cfg.graphiti_neo4j_uri = "bolt://localhost:7687"
            cfg.graphiti_neo4j_user = "neo4j"
            cfg.graphiti_neo4j_password = "password"
            cfg.graphiti_group_strategy = "market"
            cfg.graphiti_llm_model = "deepseek/deepseek-v4-flash"
            cfg.litellm_model = "deepseek/deepseek-v4-flash"
            cfg.litellm_fallback_models = []
            cfg.llm_temperature = 0.0
            cfg.graphiti_embedding_model = "openai/text-embedding-3-small"
            cfg.graphiti_embedding_base_url = None
            cfg.graphiti_embedding_api_key = None

            service = GraphitiService()

        self.assertTrue(service.is_available())
        self.assertIsInstance(
            mock_graphiti.call_args.kwargs["cross_encoder"],
            DeterministicGraphitiReranker,
        )

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
        self.assertEqual(kwargs["group_id"], "StockAnalyser")
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

    def test_ingest_news_signal_card_serializes_event_episode(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = AsyncMock()

        result = asyncio.run(service.ingest_news_signal_card(
            card={
                "card_id": "card:macro:1",
                "signal_date": "2026-07-04",
                "signal_layer": "macro",
                "summary_short": "美国非农超预期，美联储降息预期降温",
                "news_tone": "negative",
                "market_impact": "negative",
                "impact_horizon": "short",
                "valid_from": "2026-07-04T09:05:00+08:00",
                "evidence_grade": "plausible",
                "inference_level": "explicit",
                "mapping_status": "industry_only",
                "signal_score": 42.0,
                "status": "active",
                "primary_industries": ["海外宏观"],
                "company_impacts": [],
                "transmission_paths": [{"path": ["非农", "美联储降息预期", "风险偏好"]}],
                "raw_episode_ids": ["raw:macro:1"],
                "raw_episodes": [
                    {
                        "episode_id": "raw:macro:1",
                        "source": "macro_finance",
                        "provider": "search_general_news:Bocha",
                        "title": "美国6月非农就业新增5.7万人",
                        "content": "非农就业数据强于预期。",
                        "published_at": "2026-07-04T09:05:00+08:00",
                    }
                ],
            },
            market="cn",
        ))

        self.assertEqual(result["status"], "synced")
        kwargs = service._client.add_episode.await_args.kwargs
        self.assertEqual(kwargs["group_id"], "market_cn")
        self.assertEqual(kwargs["source_description"], "news_signal_card")
        self.assertEqual(kwargs["name"], "news_signal:card_macro_1")
        body = kwargs["episode_body"]
        self.assertIn("# 新闻信号卡片", body)
        self.assertIn("卡片 ID: card:macro:1", body)
        self.assertIn("美国非农", body)
        self.assertIn("原始消息: raw:macro:1", body)
        self.assertNotIn('"schema_version"', body)
        self.assertNotIn('{"card"', body)

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

    def test_remove_news_signal_card_deletes_matching_group_episode(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = AsyncMock()
        service._client.driver.execute_query.return_value = ([{"uuid": "episode-1"}], None, None)

        result = asyncio.run(service.remove_news_signal_card(card_id="card:cls:bad", market="cn"))

        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["removed"], 1)
        service._client.remove_episode.assert_awaited_once_with("episode-1")
        query_kwargs = service._client.driver.execute_query.await_args.kwargs
        self.assertEqual(query_kwargs["name"], "news_signal:card_cls_bad")
        self.assertEqual(query_kwargs["group_id"], "market_cn")

    def test_sync_ingest_reuses_event_loop_for_loop_bound_client(self):
        class LoopBoundClient:
            def __init__(self):
                self.loop = None
                self.add_count = 0

            async def build_indices_and_constraints(self):
                self._check_loop()

            async def add_episode(self, **_kwargs):
                self._check_loop()
                self.add_count += 1

            def _check_loop(self):
                loop = asyncio.get_running_loop()
                if self.loop is None:
                    self.loop = loop
                    return
                if self.loop is not loop:
                    raise RuntimeError("loop changed")

        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        client = LoopBoundClient()
        service.enabled = True
        service._client = client
        card = {
            "card_id": "card:macro:loop",
            "signal_date": "2026-07-04",
            "signal_layer": "macro",
            "summary_short": "美国非农超预期",
            "status": "active",
            "primary_industries": ["海外宏观"],
        }

        first = service.ingest_news_signal_card_sync(card=card, market="cn")
        second = service.ingest_news_signal_card_sync(card={**card, "card_id": "card:macro:loop2"}, market="cn")

        self.assertEqual(first["status"], "synced")
        self.assertEqual(second["status"], "synced")
        self.assertEqual(client.add_count, 2)

    def test_sync_ingest_wrappers_forward_timeout(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = MagicMock()
        def _consume(coro, **_kwargs):
            coro.close()
            return None

        with patch.object(service, "_run_sync", side_effect=_consume) as run_sync:
            service.ingest_analysis_sync(
                code="600519",
                stock_name="贵州茅台",
                report_type="daily",
                result={},
                context={},
                timeout_seconds=42,
            )

        self.assertEqual(run_sync.call_args.kwargs["timeout_seconds"], 42)

    def test_news_signal_sync_ingest_timeout_returns_structured_failure(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        service.enabled = True
        service._client = MagicMock()

        with patch.object(service, "_run_sync", side_effect=TimeoutError):
            result = service.ingest_news_signal_card_sync(
                card={"card_id": "card:timeout"},
                market="cn",
                timeout_seconds=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "graphiti_news_signal_ingest_timeout")
        self.assertEqual(result["card_id"], "card:timeout")

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
        self.assertIs(
            service._client.search_.await_args.kwargs["config"],
            COMBINED_HYBRID_SEARCH_RRF,
        )

    def test_search_timeout_uses_relational_fallback(self):
        fallback = MagicMock()
        fallback.search.return_value = {
            "success": True,
            "source": "relational_fallback",
            "degraded": True,
            "query": "半导体 相关事件",
            "edges": [],
            "nodes": [],
            "episodes": [{"type": "news_signal_card", "card_id": "card:1"}],
        }
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config:
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService(fallback_search=fallback)

        service.enabled = True
        service._client = AsyncMock()
        service._client.search_.side_effect = TimeoutError("reranker timeout")

        result = asyncio.run(service.search("半导体 相关事件", market="cn", limit=5))

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertIn("reranker timeout", result["graphiti_error"])
        fallback.search.assert_called_once_with(
            "半导体 相关事件",
            market="cn",
            limit=5,
            reason="graphiti_search_failed",
        )

    def test_sync_news_signal_edges_projects_explicit_neo4j_relationships(self):
        class FakeResult:
            def consume(self):
                return None

        class FakeSession:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **params):
                self.calls.append((query, params))
                return FakeResult()

        class FakeDriver:
            def __init__(self):
                self.session_obj = FakeSession()
                self.closed = False

            def session(self):
                return self.session_obj

            def close(self):
                self.closed = True

        fake_driver = FakeDriver()
        fake_graph_database = types.SimpleNamespace(driver=lambda *_args, **_kwargs: fake_driver)
        fake_neo4j = types.SimpleNamespace(GraphDatabase=fake_graph_database)

        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config, \
                patch.dict("sys.modules", {"neo4j": fake_neo4j}):
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = False
            cfg.graphiti_group_strategy = "market"
            cfg.graphiti_neo4j_uri = "bolt://localhost:7687"
            cfg.graphiti_neo4j_user = "neo4j"
            cfg.graphiti_neo4j_password = "password"
            service = GraphitiService()

            service.config.graphiti_enabled = True
            result = service.sync_news_signal_edges_sync(
                cards=[
                    {"card_id": "card:a", "summary_short": "机器人订单增长", "signal_date": "2026-07-04"},
                    {"card_id": "card:b", "summary_short": "机器人零部件涨价", "signal_date": "2026-07-04"},
                    {"card_id": "card:c", "summary_short": "模糊公司导语", "signal_date": "2026-07-04", "status": "suppressed"},
                ],
                edges=[
                    {
                        "edge_id": "edge:typed",
                        "source_card_id": "card:a",
                        "target_type": "industry",
                        "target_id": "industry:机器人",
                        "edge_class": "typed_relation",
                        "edge_type": "impacts_industry",
                        "weight": 0.82,
                        "method": "rule",
                    },
                    {
                        "edge_id": "edge:semantic",
                        "source_card_id": "card:a",
                        "target_card_id": "card:b",
                        "target_type": "card",
                        "target_id": "card:b",
                        "edge_class": "semantic_similarity",
                        "edge_type": "semantic_similarity",
                        "weight": 0.91,
                        "method": "embedding",
                    },
                    {
                        "edge_id": "edge:suppressed",
                        "source_card_id": "card:c",
                        "target_type": "industry",
                        "target_id": "industry:机器人",
                        "edge_class": "typed_relation",
                        "edge_type": "impacts_industry",
                        "weight": 0.72,
                        "method": "rule",
                    },
                ],
                market="cn",
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["edges"], 2)
        self.assertEqual(result["input_edges"], 3)
        self.assertEqual(result["skipped_inactive_edges"], 1)
        self.assertEqual(result["inactive_cards_pruned"], 1)
        self.assertTrue(fake_driver.closed)
        queries = "\n".join(query for query, _params in fake_driver.session_obj.calls)
        self.assertIn("NEWS_SIGNAL_TYPED_RELATION", queries)
        self.assertIn("NEWS_SIGNAL_SEMANTIC_SIMILARITY", queries)
        self.assertIn("NewsSignalTarget", queries)
        self.assertIn("inactive_card_ids", str(fake_driver.session_obj.calls))

    def test_enabled_graphiti_disables_when_neo4j_unreachable(self):
        with patch("src.services.graphiti.graph_service.get_config") as mock_get_config, \
                patch("src.services.graphiti.graph_service._can_open_tcp", return_value=(False, "connection refused")):
            cfg = mock_get_config.return_value
            cfg.graphiti_enabled = True
            cfg.graphiti_neo4j_uri = "bolt://localhost:7687"
            cfg.graphiti_group_strategy = "market"
            service = GraphitiService()

        self.assertFalse(service.is_available())

    def test_default_entity_types_do_not_conflict_with_graphiti_reserved_fields(self):
        validate_entity_types(DEFAULT_ENTITY_TYPES)


if __name__ == "__main__":
    unittest.main()
