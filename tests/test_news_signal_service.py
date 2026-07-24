# -*- coding: utf-8 -*-
"""News signal card service tests."""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import func, select

from src.config import Config
from src.search_service import SearchResponse, SearchResult
from src.services.news_signal_service import NewsEventLLMExtractor, NewsSignalService
from src.storage import (
    DatabaseManager,
    NewsSignalEdge,
    PortfolioAccount,
    PortfolioPosition,
    RawNewsEpisode,
)


class NewsSignalServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "news_signal.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=603019",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.service = NewsSignalService()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("NEWS_EVENT_EXTRACTOR_MODE", None)
        os.environ.pop("NEWS_EVENT_EXTRACTOR_MODEL", None)
        self.temp_dir.cleanup()

    def test_cjzc_card_upsert_is_idempotent_and_maps_company(self) -> None:
        payload = _cjzc_payload()
        raw, cards = self.service._build_from_cjzc(payload, date(2026, 7, 1))

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)
        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)

        listed = self.service.list_cards(signal_date="2026-07-01")
        self.assertEqual(listed["total"], 1)
        card = listed["items"][0]
        self.assertEqual(card["mapping_status"], "mapped")
        self.assertEqual(card["signal_layer"], "company")
        self.assertEqual(card["primary_industries"], ["AI服务器"])
        self.assertEqual(card["company_impacts"][0]["symbol"], "603019")

        with DatabaseManager.get_instance().get_session() as session:
            raw_count = session.execute(select(func.count(RawNewsEpisode.id))).scalar()
        self.assertEqual(raw_count, 1)

    def test_seed_evidence_only_returns_actionable_cards_for_existing_codes(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        eligible = {
            **cards[0],
            "valid_until": datetime(2099, 12, 31, 23, 59, 59),
        }
        speculative = {
            **eligible,
            "card_id": "card:speculative:603019",
            "evidence_grade": "speculative",
            "signal_score": 99.0,
        }
        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards([eligible, speculative])

        result = self.service.seed_evidence_for_codes(
            ["603019", "000001"],
            signal_date="2026-07-01",
        )

        self.assertEqual(result["matched_codes"], 1)
        self.assertEqual(result["attached_cards"], 1)
        self.assertEqual(list(result["items_by_code"]), ["603019"])
        evidence = result["items_by_code"]["603019"][0]
        self.assertEqual(evidence["card_id"], eligible["card_id"])
        self.assertEqual(evidence["gate_result"], "matched_existing_seed")
        self.assertEqual(evidence["company_direction"], "benefit")

    def test_cjzc_builds_theme_level_raw_episode_and_actionable_chain(self) -> None:
        payload = _cjzc_payload()
        payload["themes"] = [
            {
                "theme": "存储器",
                "keywords": ["DRAM", "存储芯片"],
                "polarity": "positive",
                "evidence": "三星拟将三季度DRAM平均售价环比提高20%，江波龙半年报业绩预告大幅增长。",
                "mapped_stocks": [{"code": "301308", "name": "江波龙", "role": "memory_module"}],
                "related_boards": ["存储芯片", "半导体", "HBM"],
                "theme_score": 40,
            },
            {
                "theme": "黄金",
                "keywords": ["黄金"],
                "polarity": "positive",
                "evidence": "摩根大通预计金价后续仍有上行空间。",
                "mapped_stocks": [],
                "related_boards": ["贵金属"],
                "theme_score": 20,
            },
        ]
        payload["article_sections"] = [
            {"section": "热点", "text": "三星拟将三季度DRAM平均售价环比提高20%。"},
            {"section": "海外", "text": "摩根大通预计金价后续仍有上行空间。"},
        ]

        raw, cards = self.service._build_from_cjzc(payload, date(2026, 7, 1))
        storage_raw = next(item for item in raw if item["subjects"][0] == "存储器")
        storage_card = next(item for item in cards if item["primary_industries"] == ["存储器"])
        storage_event = storage_card["extracted_events"][0]
        path = storage_card["transmission_paths"][0]

        self.assertEqual(len(raw), 2)
        self.assertIn("DRAM", storage_raw["normalized_content"])
        self.assertNotIn("摩根大通", storage_raw["normalized_content"])
        self.assertEqual(storage_event["event_type"], "价格/供需")
        self.assertEqual(storage_event["extractor"], "rule_fallback")
        self.assertIn("source", storage_event["verification_status"])
        self.assertIn("江波龙", [item.get("name") for item in storage_event["entity_links"]])
        self.assertEqual(path["event_category"], "价格/供需")
        self.assertEqual(path["event_id"], storage_event["event_id"])
        self.assertGreater(path["event_score"], 60)
        self.assertEqual(path["chain_steps"][0]["label"], "价格/供需")
        self.assertIn("江波龙", path["chain_steps"][2]["text"])

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_extracted_events([event for card in cards for event in card["extracted_events"]])
        saved_cards = self.service.repo.upsert_cards(cards)
        detail = self.service.get_card(saved_cards[0]["card_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["extracted_events"][0]["event_type"], "价格/供需")
        self.assertEqual(detail["extracted_events"][0]["raw_episode_id"], storage_raw["episode_id"])

    def test_backfill_extracted_events_uses_existing_relational_sources(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        for card in cards:
            card["extracted_events"] = []
            card["diagnostics"].pop("event_extraction", None)
        self.service.repo.upsert_raw_episodes(raw)
        saved_cards = self.service.repo.upsert_cards(cards)

        result = self.service.backfill_extracted_events(
            signal_date="2026-07-01",
            limit=20,
        )
        detail = self.service.get_card(saved_cards[0]["card_id"])

        self.assertEqual(result["cards_scanned"], 1)
        self.assertEqual(result["cards_updated"], 1)
        self.assertGreater(result["events_upserted"], 0)
        self.assertGreater(len(detail["extracted_events"]), 0)
        self.assertEqual(detail["transmission_paths"][0]["event_id"], detail["extracted_events"][0]["event_id"])

    def test_llm_event_extractor_normalizes_json_events_when_enabled(self) -> None:
        os.environ["NEWS_EVENT_EXTRACTOR_MODE"] = "llm"
        os.environ["NEWS_EVENT_EXTRACTOR_MODEL"] = "deepseek/deepseek-v4-flash"
        Config.reset_instance()
        service = NewsSignalService()
        payload = _cjzc_payload()
        llm_payload = json.dumps(
            {
                "events": [
                    {
                        "event_type": "大客户/订单",
                        "trigger": "客户验证加速",
                        "subject": "AI服务器",
                        "object": "算力产业链",
                        "direction": "benefit",
                        "metric_value": "",
                        "evidence_sentence": "AI服务器订单增长，客户验证加速。",
                        "entity_links": [
                            {"entity_type": "industry", "name": "AI服务器", "confidence": 0.86},
                            {"entity_type": "company", "name": "中科曙光", "symbol": "603019", "confidence": 0.82},
                        ],
                        "confidence": 0.84,
                        "verification_status": "source_only",
                    }
                ]
            },
            ensure_ascii=False,
        )

        with patch.object(
            NewsEventLLMExtractor,
            "_call_llm",
            return_value=(llm_payload, "deepseek/deepseek-v4-flash", {"total_tokens": 120}),
        ):
            raw, cards = service._build_from_cjzc(payload, date(2026, 7, 1))

        event = cards[0]["extracted_events"][0]
        path = cards[0]["transmission_paths"][0]
        self.assertEqual(event["event_type"], "大客户/订单")
        self.assertEqual(event["extractor"], "llm_json:deepseek/deepseek-v4-flash")
        self.assertEqual(event["confidence"], 0.84)
        self.assertEqual(event["diagnostics"]["llm_extraction"]["status"], "ok")
        self.assertEqual(event["diagnostics"]["llm_extraction"]["usage"]["total_tokens"], 120)
        self.assertEqual(path["event_category"], "大客户/订单")
        self.assertEqual(path["event_id"], event["event_id"])

    def test_llm_event_extractor_failure_keeps_rule_fallback(self) -> None:
        os.environ["NEWS_EVENT_EXTRACTOR_MODE"] = "llm"
        Config.reset_instance()
        service = NewsSignalService()

        with patch.object(NewsEventLLMExtractor, "_call_llm", side_effect=RuntimeError("provider timeout")):
            raw, cards = service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))

        event = cards[0]["extracted_events"][0]
        self.assertEqual(event["extractor"], "rule_fallback")
        self.assertEqual(event["event_type"], "大客户/订单")
        self.assertEqual(event["diagnostics"]["llm_extraction"]["status"], "failed")
        self.assertEqual(event["diagnostics"]["llm_extraction"]["reason"], "RuntimeError")

    def test_raw_episode_upsert_uses_episode_id_when_dedup_key_changes(self) -> None:
        first = {
            "episode_id": "raw:cls:same",
            "dedup_key": "dedup:cls:old",
            "source": "cls_telegraph",
            "provider": "orz.dailynews.cls",
            "source_id": "1",
            "url": "https://www.cls.cn/telegraph",
            "title": "同一条财联社新闻",
            "summary": "旧摘要",
            "content": "旧正文",
            "published_at": datetime(2026, 7, 4, 15, 11, 40),
            "signal_date": date(2026, 7, 4),
            "session": "post_close",
        }
        second = {
            **first,
            "dedup_key": "dedup:cls:new",
            "summary": "新摘要",
            "content": "新正文",
        }

        self.service.repo.upsert_raw_episodes([first])
        saved = self.service.repo.upsert_raw_episodes([second])

        self.assertEqual(saved[0]["episode_id"], "raw:cls:same")
        self.assertEqual(saved[0]["dedup_key"], "dedup:cls:new")
        self.assertEqual(saved[0]["summary"], "新摘要")
        with DatabaseManager.get_instance().get_session() as session:
            raw_count = session.execute(select(func.count(RawNewsEpisode.id))).scalar()
        self.assertEqual(raw_count, 1)

    def test_cls_incremental_ingest_only_persists_unseen_feed_items(self) -> None:
        first_payload = _dailynews_payload(
            title="存储芯片报价上调",
            content="存储芯片报价上调，产业链关注度提升。",
            published_at="2026-07-11T09:10:00+08:00",
            provider="orz.dailynews.cls",
        )
        second_payload = _dailynews_payload(
            title="服务器订单新增",
            content="服务器订单新增，相关公司进入交付阶段。",
            published_at="2026-07-11T09:16:00+08:00",
            provider="orz.dailynews.cls",
        )
        second_payload["results"][0]["id"] = "2"
        second_payload["results"][0]["url"] = "https://example.test/news/2"
        second_payload["results"][0]["published_ts"] = 1783135060
        combined = {
            **first_payload,
            "results": [*first_payload["results"], *second_payload["results"]],
        }

        with patch.object(self.service, "_fetch_cls", return_value=(first_payload, None)):
            first = self.service.ingest_cls_incremental(limit=50)
        with patch.object(self.service, "_fetch_cls", return_value=(combined, None)):
            second = self.service.ingest_cls_incremental(limit=50)
            repeated = self.service.ingest_cls_incremental(limit=50)

        self.assertEqual(first["new_raw_episodes"], 1)
        self.assertEqual(second["new_raw_episodes"], 1)
        self.assertEqual(repeated["new_raw_episodes"], 0)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["source"], "cls_telegraph")
        self.assertGreaterEqual(second["outbox_enqueued"], 2)
        self.assertIsNotNone(second["cursor"]["published_at"])

    def test_portfolio_anysearch_ingest_keeps_actionable_holding_news_idempotently(self) -> None:
        with DatabaseManager.get_instance().get_session() as session:
            active = PortfolioAccount(name="A股主账户", market="cn", is_active=True)
            inactive = PortfolioAccount(name="停用账户", market="cn", is_active=False)
            session.add_all([active, inactive])
            session.flush()
            session.add_all(
                [
                    PortfolioPosition(
                        account_id=active.id,
                        symbol="600487",
                        market="cn",
                        currency="CNY",
                        quantity=300,
                    ),
                    PortfolioPosition(
                        account_id=active.id,
                        symbol="600667",
                        market="cn",
                        currency="CNY",
                        quantity=100,
                    ),
                    PortfolioPosition(
                        account_id=inactive.id,
                        symbol="002050",
                        market="cn",
                        currency="CNY",
                        quantity=100,
                    ),
                ]
            )
            session.commit()

        search = _FakePortfolioAnySearch()
        names = {"600487": "亨通光电", "600667": "太极实业"}

        first = self.service.ingest_portfolio_anysearch_news(
            search_client=search,
            name_resolver=lambda symbol, market: names.get(symbol, ""),
            max_results_per_stock=5,
            max_age_days=14,
            now=datetime(2026, 7, 21, 12, 0, 0),
        )
        second = self.service.ingest_portfolio_anysearch_news(
            search_client=search,
            name_resolver=lambda symbol, market: names.get(symbol, ""),
            max_results_per_stock=5,
            max_age_days=14,
            now=datetime(2026, 7, 21, 12, 5, 0),
        )

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["holding_count"], 2)
        self.assertEqual(first["searched_holdings"], 2)
        self.assertEqual(first["fetched_items"], 4)
        self.assertEqual(first["accepted_items"], 2)
        self.assertEqual(first["filtered_items"], 2)
        self.assertEqual(first["new_raw_episodes"], 2)
        self.assertEqual(first["cards_upserted"], 2)
        self.assertEqual(second["new_raw_episodes"], 0)
        self.assertEqual(second["cards_upserted"], 0)
        self.assertEqual(
            search.queries[:2],
            [
                "亨通光电 600487 最新消息 公告 业绩 风险",
                "太极实业 600667 最新消息 公告 业绩 风险",
            ],
        )

        cards = self.service.list_cards(limit=10)
        self.assertEqual(cards["total"], 2)
        titles = {card["summary_short"] for card in cards["items"]}
        self.assertTrue(any("业绩预增" in title for title in titles))
        self.assertTrue(any("减持" in title for title in titles))
        self.assertFalse(any("最新价格" in title for title in titles))

        detail = self.service.get_card(first["cards"][0]["card_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["diagnostics"]["source"], "portfolio_anysearch")
        self.assertEqual(detail["mapping_status"], "mapped")
        self.assertEqual(detail["company_impacts"][0]["symbol"], "600487")
        self.assertEqual(detail["source_chain"][0]["provider"], "AnySearch")
        self.assertEqual(detail["source_chain"][0]["published_at"], "2026-07-14T00:00:00")
        self.assertEqual(detail["source_chain"][0]["url"], "https://example.test/600487-performance.pdf")

        by_title = {card["summary_short"]: card for card in cards["items"]}
        performance_card = next(card for title, card in by_title.items() if "业绩预增" in title)
        reduction_card = next(card for title, card in by_title.items() if "减持" in title)
        self.assertEqual(performance_card["news_tone"], "positive")
        self.assertEqual(performance_card["company_impacts"][0]["direction"], "benefit")
        self.assertEqual(reduction_card["news_tone"], "negative")
        self.assertEqual(reduction_card["company_impacts"][0]["direction"], "harm")

    def test_portfolio_anysearch_ingest_filters_old_and_missing_publish_dates(self) -> None:
        with DatabaseManager.get_instance().get_session() as session:
            account = PortfolioAccount(name="A股主账户", market="cn", is_active=True)
            session.add(account)
            session.flush()
            session.add(
                PortfolioPosition(
                    account_id=account.id,
                    symbol="600367",
                    market="cn",
                    currency="CNY",
                    quantity=100,
                )
            )
            session.commit()

        class _StalePortfolioAnySearch:
            def search(self, query: str, max_results: int = 5, days: int = 3):
                del max_results, days
                return SearchResponse(
                    query=query,
                    provider="AnySearch",
                    success=True,
                    results=[
                        SearchResult(
                            title="红星发展(600367) - 股票交易异常波动暨风险提示公告",
                            snippet="公司股票短期内存在市场情绪过热、非理性炒作风险。",
                            url="https://example.test/600367-risk-old",
                            source="example.test",
                            published_date="2026-07-06",
                        ),
                        SearchResult(
                            title="红星发展(600367) - 业绩预告公告",
                            snippet="红星发展公告称预计净利润同比变动。",
                            url="https://example.test/600367-missing-date",
                            source="example.test",
                            published_date=None,
                        ),
                    ],
                )

        result = self.service.ingest_portfolio_anysearch_news(
            search_client=_StalePortfolioAnySearch(),
            name_resolver=lambda symbol, market: "红星发展",
            max_results_per_stock=5,
            max_age_days=3,
            now=datetime(2026, 7, 22, 16, 30, 0),
        )

        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["fetched_items"], 2)
        self.assertEqual(result["accepted_items"], 0)
        self.assertEqual(result["filtered_items"], 2)
        self.assertEqual(result["new_raw_episodes"], 0)
        self.assertEqual(result["cards_upserted"], 0)
        self.assertEqual(self.service.list_cards(limit=10)["items"], [])

    def test_positive_signal_quality_marks_unlock_earnings_as_risk_disguised_positive(self) -> None:
        payload = _dailynews_payload(
            title="测试股份：限售股解禁前披露半年度业绩预增公告",
            content="测试股份(600001)控股股东限售股即将解禁，公司预计2026年半年度净利润同比增长220%。",
            published_at="2026-07-22T10:00:00+08:00",
        )
        payload["results"][0]["stocks"] = [{"code": "600001", "name": "测试股份"}]

        _raw, cards = self.service._build_from_cls(payload)
        card = cards[0]
        quality = card["diagnostics"]["positive_signal_quality"]

        self.assertEqual(quality["category"], "risk_disguised_positive")
        self.assertEqual(quality["label"], "利空式利好")
        self.assertEqual(card["news_tone"], "negative")
        self.assertEqual(card["company_impacts"][0]["direction"], "harm")
        self.assertEqual(card["evidence_grade"], "speculative")
        self.assertLessEqual(card["signal_score"], 45.0)

    def test_positive_signal_quality_marks_framework_or_domestic_contract_as_one_day_positive(self) -> None:
        payload = _dailynews_payload(
            title="测试股份：与国内客户签署战略合作框架协议",
            content="测试股份(600002)与国内客户签署战略合作框架协议，双方正布局新材料应用并推进送样验证。",
            published_at="2026-07-22T10:05:00+08:00",
        )
        payload["results"][0]["stocks"] = [{"code": "600002", "name": "测试股份"}]

        _raw, cards = self.service._build_from_cls(payload)
        card = cards[0]
        quality = card["diagnostics"]["positive_signal_quality"]

        self.assertEqual(quality["category"], "one_day_positive")
        self.assertEqual(quality["label"], "一日游式利好")
        self.assertIn("随时可撤的框架/意向协议", quality["matched_rules"])
        self.assertEqual(card["news_tone"], "positive")
        self.assertEqual(card["company_impacts"][0]["direction"], "benefit")
        self.assertEqual(card["evidence_grade"], "speculative")
        self.assertLessEqual(card["signal_score"], 49.0)

    def test_positive_signal_quality_marks_foreign_contract_and_batch_supply_as_true_positive(self) -> None:
        payload = _dailynews_payload(
            title="测试股份：已与海外大厂英伟达签订批量供货合同",
            content="测试股份(600003)公告，已与海外大厂英伟达签订长期供货合同，相关产品进入批量供货阶段。",
            published_at="2026-07-22T10:10:00+08:00",
        )
        payload["results"][0]["stocks"] = [{"code": "600003", "name": "测试股份"}]

        _raw, cards = self.service._build_from_cls(payload)
        card = cards[0]
        quality = card["diagnostics"]["positive_signal_quality"]

        self.assertEqual(quality["category"], "true_positive")
        self.assertEqual(quality["label"], "真利好")
        self.assertIn("已与国外大厂签合同", quality["matched_rules"])
        self.assertIn("批量供货", quality["matched_rules"])
        self.assertEqual(card["news_tone"], "positive")
        self.assertEqual(card["company_impacts"][0]["direction"], "benefit")
        self.assertEqual(card["evidence_grade"], "confirmed")
        self.assertGreaterEqual(card["signal_score"], 85.0)

    def test_positive_signal_quality_marks_company_share_buyback_as_true_positive(self) -> None:
        payload = _dailynews_payload(
            title="佰维存储：董事长提议回购2亿元-2.5亿元股份 全部用于注销并减少注册资本",
            content=(
                "佰维存储(688525.SH)公告称，公司控股股东、实际控制人、董事长提议公司"
                "以集中竞价方式回购股份，回购资金总额为2亿元至2.5亿元，"
                "回购股份将全部用于注销并减少注册资本。"
            ),
            published_at="2026-07-22T19:23:07+08:00",
        )
        payload["results"][0]["stocks"] = [{"code": "688525", "name": "佰维存储"}]

        _raw, cards = self.service._build_from_cls(payload)
        card = cards[0]
        quality = card["diagnostics"]["positive_signal_quality"]

        self.assertEqual(quality["category"], "true_positive")
        self.assertEqual(quality["label"], "真利好")
        self.assertIn("公司股份回购", quality["matched_rules"])
        self.assertEqual(card["news_tone"], "positive")
        self.assertEqual(card["market_impact"], "positive")
        self.assertEqual(card["company_impacts"][0]["direction"], "benefit")
        self.assertGreaterEqual(card["signal_score"], 85.0)

    def test_dailynews_normalizes_raw_content_and_marks_low_quality_without_deleting(self) -> None:
        raw, cards = self.service._build_from_cls(
            _dailynews_payload(
                title="<b>早报</b>",
                content="早报",
                published_at="2026-07-04T11:11:41+08:00",
            )
        )

        self.assertEqual(raw[0]["normalized_content"], "早报")
        self.assertEqual(raw[0]["quality_grade"], "low")
        self.assertEqual(raw[0]["status"], "low_quality")
        self.assertIn("weak_signal_terms", raw[0]["quality_flags"])
        self.assertEqual(cards[0]["status"], "low_quality")
        self.assertLess(cards[0]["signal_score"], 36)
        self.assertEqual(cards[0]["diagnostics"]["quality_gate"]["status"], "low_quality")

        saved_raw = self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)
        self.assertEqual(saved_raw[0]["quality_grade"], "low")
        self.assertEqual(saved_raw[0]["normalized_content"], "早报")
        listed = self.service.list_cards(signal_date="2026-07-04")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["status"], "low_quality")
        detail = self.service.get_card(listed["items"][0]["card_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["raw_episodes"][0]["quality_grade"], "low")

    def test_generic_company_teaser_does_not_expand_theme_mapping_to_companies(self) -> None:
        self.service._concept_mapping = {
            "存储芯片": {
                "keywords": ["存储芯片", "先进封装", "华为"],
                "related_boards": ["半导体"],
                "mapped_stocks": [
                    {"code": "301308", "name": "江波龙", "role": "存储模组"},
                    {"code": "688525", "name": "佰维存储", "role": "存储芯片"},
                ],
            }
        }
        payload = _dailynews_payload(
            title="存储芯片+先进封装+AI应用+华为！这家公司推出相关平台",
            content="公司上半年净利同比最高预增超2倍，算力芯片产品已在多个领域应用。",
            published_at="2026-07-11T09:20:00+08:00",
        )

        _raw, cards = self.service._build_from_cls(payload)

        self.assertEqual(cards[0]["company_impacts"], [])
        self.assertEqual(cards[0]["mapping_status"], "industry_only")
        self.assertEqual(cards[0]["signal_layer"], "industry")
        self.assertEqual(cards[0]["status"], "suppressed")
        self.assertLessEqual(cards[0]["signal_score"], 35.0)
        self.assertEqual(cards[0]["diagnostics"]["company_mapping_gate"]["status"], "blocked_no_explicit_company")

    def test_generic_company_teaser_without_mapping_candidates_is_suppressed(self) -> None:
        raw, cards = self.service._build_from_cls(
            _dailynews_payload(
                title="AI编程市场高速增长，这家公司平台已实现快速开发落地",
                content="工信部发布安全风险提示，行业市场年均复合增长率达38%。",
                published_at="2026-07-11T09:22:00+08:00",
            )
        )

        self.assertEqual(len(raw), 1)
        self.assertEqual(cards[0]["company_impacts"], [])
        self.assertEqual(cards[0]["diagnostics"]["company_mapping_gate"]["status"], "no_company_candidates")
        self.assertEqual(cards[0]["status"], "suppressed")
        self.assertEqual(cards[0]["evidence_grade"], "speculative")
        self.assertLessEqual(cards[0]["signal_score"], 35.0)

    def test_theme_mapping_keeps_only_company_explicitly_named_in_news_text(self) -> None:
        self.service._concept_mapping = {
            "存储芯片": {
                "keywords": ["存储芯片"],
                "related_boards": ["半导体"],
                "mapped_stocks": [
                    {"code": "301308", "name": "江波龙", "role": "存储模组"},
                    {"code": "688525", "name": "佰维存储", "role": "存储芯片"},
                ],
            }
        }
        payload = _dailynews_payload(
            title="江波龙存储芯片订单增长",
            content="江波龙披露存储芯片相关业务收入增长。",
            published_at="2026-07-11T09:25:00+08:00",
        )

        _raw, cards = self.service._build_from_cls(payload)

        self.assertEqual([item["symbol"] for item in cards[0]["company_impacts"]], ["301308"])
        self.assertEqual(cards[0]["mapping_status"], "mapped")

    def test_company_mapping_repair_removes_legacy_theme_expansion(self) -> None:
        raw = {
            "episode_id": "raw:legacy:generic-company",
            "dedup_key": "dedup:legacy:generic-company",
            "source": "cls_telegraph",
            "title": "存储芯片+先进封装！这家公司净利预增",
            "summary": "公司上半年净利同比预增超2倍。",
            "content": "公司上半年净利同比预增超2倍，相关平台支持异构集成封装设计。",
            "normalized_content": "存储芯片+先进封装！这家公司净利预增。公司上半年净利同比预增超2倍。",
            "published_at": datetime(2026, 7, 11, 9, 30),
            "signal_date": date(2026, 7, 11),
            "session": "intraday",
        }
        card = _edge_card(
            "card:legacy:generic-company",
            "存储芯片+先进封装！这家公司净利预增",
            "存储芯片",
            "301308",
            "江波龙",
        )
        card["signal_date"] = date(2026, 7, 11)
        card["raw_episode_ids"] = [raw["episode_id"]]
        card["company_impacts"].append(
            {
                "symbol": "688525",
                "name": "佰维存储",
                "direction": "benefit",
                "confidence": 0.78,
                "mapping_status": "mapped",
                "rationale": "旧主题词典扩散",
            }
        )
        self.service.repo.upsert_raw_episodes([raw])
        self.service.repo.upsert_cards([card])

        result = self.service.repair_company_mapping_gates(signal_date="2026-07-11")
        repaired = self.service.get_card(card["card_id"])

        self.assertEqual(result["cards_updated"], 1)
        self.assertEqual(result["companies_removed"], 2)
        self.assertEqual(repaired["company_impacts"], [])
        self.assertEqual(repaired["mapping_status"], "industry_only")
        self.assertEqual(repaired["signal_layer"], "industry")
        self.assertEqual(repaired["status"], "suppressed")
        self.assertLessEqual(repaired["signal_score"], 35.0)
        self.assertEqual(repaired["diagnostics"]["company_mapping_gate"]["status"], "blocked_no_explicit_company")

    def test_company_mapping_repair_suppresses_legacy_teaser_without_existing_impacts(self) -> None:
        raw = {
            "episode_id": "raw:legacy:generic-company-no-impacts",
            "dedup_key": "dedup:legacy:generic-company-no-impacts",
            "source": "cls_telegraph",
            "title": "AI应用加速落地，这家公司平台已实现快速开发",
            "summary": "行业市场年均复合增长率达38%。",
            "content": "该公司平台已实现应用快速开发落地，但正文未披露公司名称。",
            "normalized_content": "AI应用加速落地，这家公司平台已实现快速开发。",
            "published_at": datetime(2026, 7, 11, 9, 31),
            "signal_date": date(2026, 7, 11),
            "session": "intraday",
        }
        card = _edge_card(
            "card:legacy:generic-company-no-impacts",
            "AI应用加速落地，这家公司平台已实现快速开发",
            "AI应用",
            "",
            "",
        )
        card["signal_date"] = date(2026, 7, 11)
        card["raw_episode_ids"] = [raw["episode_id"]]
        card["company_impacts"] = []
        self.service.repo.upsert_raw_episodes([raw])
        self.service.repo.upsert_cards([card])

        result = self.service.repair_company_mapping_gates(signal_date="2026-07-11")
        repaired = self.service.get_card(card["card_id"])

        self.assertEqual(result["cards_updated"], 1)
        self.assertEqual(result["companies_removed"], 0)
        self.assertEqual(repaired["company_impacts"], [])
        self.assertEqual(repaired["status"], "suppressed")
        self.assertEqual(repaired["evidence_grade"], "speculative")
        self.assertLessEqual(repaired["signal_score"], 35.0)
        self.assertEqual(
            repaired["diagnostics"]["company_mapping_gate"]["suppression_reason"],
            "generic_company_teaser_without_explicit_company",
        )

    def test_evidence_adapter_and_feedback_read_path(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        self.service.repo.upsert_raw_episodes(raw)
        saved = self.service.repo.upsert_cards(cards)
        card_id = saved[0]["card_id"]

        evidence = self.service.evidence_card_for(card_id, symbol="603019", name="中科曙光")
        self.assertEqual(evidence["dimension"], "news_event")
        self.assertEqual(evidence["stock"]["code"], "603019")
        self.assertTrue(evidence["signals"])
        self.assertEqual(evidence["expiry"]["window"], "2w")

        feedback = self.service.add_feedback(card_id=card_id, feedback_type="wrong", note="映射过强")
        self.assertEqual(feedback["feedback_type"], "wrong")
        listed = self.service.list_cards(signal_date="2026-07-01")
        self.assertEqual(listed["items"][0]["status"], "suppressed")
        self.assertLess(listed["items"][0]["adjusted_signal_score"], listed["items"][0]["signal_score"])

    def test_feedback_controls_remove_company_duplicate_and_metrics(self) -> None:
        first = _edge_card(
            "card:feedback:a",
            "机器人订单增长，产业链客户验证加速",
            "机器人",
            "300024",
            "机器人公司",
        )
        second = _edge_card(
            "card:feedback:b",
            "机器人订单增长，客户验证进度更新",
            "机器人",
            "300024",
            "机器人公司",
        )
        self.service.repo.upsert_cards([first, second])
        self.service.rebuild_edges(signal_date="2026-07-04")
        self.assertTrue(self.service.repo.list_edges(card_id=second["card_id"], limit=20))

        duplicate = self.service.add_feedback(card_id=second["card_id"], feedback_type="duplicate", note="同一事件重复卡")
        remove_company = self.service.add_feedback(
            card_id=first["card_id"],
            feedback_type="remove_company",
            note="机器人公司",
            payload={"symbol": "300024"},
        )

        repaired = self.service.get_card(first["card_id"])
        duplicate_card = self.service.get_card(second["card_id"])
        metrics = self.service.metrics(signal_date="2026-07-04")

        self.assertEqual(duplicate["effect"], "suppress_card_and_remove_edges")
        self.assertEqual(remove_company["effect"], "remove_company_mapping_and_rebuild_edges")
        self.assertEqual(duplicate_card["status"], "suppressed")
        self.assertEqual(repaired["company_impacts"], [])
        self.assertEqual(repaired["mapping_status"], "industry_only")
        self.assertEqual(repaired["signal_layer"], "industry")
        self.assertLessEqual(repaired["mapping_confidence"], 0.35)
        self.assertEqual(repaired["diagnostics"]["feedback_controls"][-1]["feedback_type"], "remove_company")
        self.assertEqual(metrics["feedback_counts"]["duplicate"], 1)
        self.assertEqual(metrics["feedback_counts"]["remove_company"], 1)
        self.assertEqual(metrics["feedback_quality"]["negative_feedback_total"], 2)
        self.assertEqual(metrics["feedback_quality"]["control_rule_counts"]["remove_company"], 1)
        self.assertIn("raw_quality", metrics)
        self.assertIn("edge_quality", metrics)
        self.assertIn("isolated_card_ratio", metrics["edge_quality"])

    def test_foreign_supply_chain_and_domestic_substitution_template(self) -> None:
        self.service._concept_mapping = {
            "MLCC": {
                "aliases": ["MLCC", "片式多层陶瓷电容"],
                "related_boards": ["被动元件", "电子元件"],
                "mapped_stocks": [],
            }
        }
        raw, cards = self.service._build_from_cls(
            _dailynews_payload(
                title="日本MLCC厂商出口受限，国产替代窗口打开",
                content="日本MLCC厂商因出口管制供应受限，下游客户加速导入国产替代和二供认证。",
                published_at="2026-07-11T09:20:00+08:00",
            )
        )

        event = cards[0]["extracted_events"][0]
        path = cards[0]["transmission_paths"][0]

        self.assertEqual(len(raw), 1)
        self.assertEqual(event["event_type"], "供应链/替代")
        self.assertEqual(path["event_category"], "供应链/替代")
        self.assertEqual(path["template_id"], "foreign_supply_to_domestic_substitution")
        self.assertEqual(path["evidence_template"]["confidence_rule"], "industry_level_until_company_product_customer_evidence_is_explicit")
        self.assertEqual(path["chain_steps"][0]["label"], "海外供给/出口线索")
        self.assertEqual(path["chain_steps"][1]["label"], "国产替代验证")

    def test_dailynews_macro_card_uses_macro_layer(self) -> None:
        raw, cards = self.service._build_from_cls(
            _dailynews_payload(
                title="美国6月非农数据超预期，美联储降息预期降温",
                content="美国6月非农数据超预期，美元指数走强，全球风险资产波动加大。",
                published_at="2026-07-04T11:11:41+08:00",
            )
        )

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)

        listed = self.service.list_cards(signal_date="2026-07-04", signal_layer="macro")
        self.assertEqual(listed["total"], 1)
        card = listed["items"][0]
        self.assertEqual(card["signal_layer"], "macro")
        self.assertEqual(card["primary_industries"], ["海外宏观"])
        self.assertEqual(card["company_impacts"], [])

    def test_xueqiu_hot_news_builds_cards_and_layer_counts(self) -> None:
        raw, cards = self.service._build_from_xueqiu(
            _dailynews_payload(
                title="人型机器人商业化落地加速",
                content="机器人利好催化密集，相关产业链关注升温。",
                published_at="2026-07-04T11:11:40+08:00",
                provider="orz.dailynews.xueqiu",
            )
        )

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)

        listed = self.service.list_cards(signal_date="2026-07-04")
        self.assertEqual(listed["total"], 1)
        card = listed["items"][0]
        self.assertTrue(card["card_id"].startswith("card:xueqiu:"))
        self.assertIn(card["signal_layer"], {"industry", "company"})
        self.assertEqual(card["diagnostics"]["source"], "xueqiu_hot")
        self.assertEqual(listed["summary"]["layer_counts"][card["signal_layer"]], 1)

    def test_macro_finance_source_builds_macro_layer_card(self) -> None:
        raw, cards = self.service._build_from_macro_finance(
            _dailynews_payload(
                title="央行开展5000亿元逆回购操作",
                content="人民银行公开市场净投放，维护银行体系流动性合理充裕。",
                published_at="2026-07-04T09:05:00+08:00",
                provider="orz.dailynews.macro_finance",
            )
        )

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)

        listed = self.service.list_cards(signal_date="2026-07-04", signal_layer="macro")
        self.assertEqual(listed["total"], 1)
        card = listed["items"][0]
        self.assertEqual(card["signal_layer"], "macro")
        self.assertEqual(card["primary_industries"], ["国内流动性"])
        self.assertEqual(card["company_impacts"], [])
        self.assertEqual(card["diagnostics"]["source"], "macro_finance")
        self.assertNotEqual(card["diagnostics"]["positive_signal_quality"]["category"], "true_positive")

    def test_people_bank_penalty_is_not_classified_as_macro_liquidity(self) -> None:
        raw, cards = self.service._build_from_macro_finance(
            _dailynews_payload(
                title="瀚银科技被罚没近7445万元",
                content="中国人民银行上海市分行披露行政处罚决定。",
                published_at="2026-07-04T09:04:00+08:00",
                provider="orz.dailynews.macro_finance",
            )
        )

        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)

        listed = self.service.list_cards(signal_date="2026-07-04")
        self.assertEqual(listed["total"], 1)
        self.assertNotEqual(listed["items"][0]["signal_layer"], "macro")

    def test_sync_graphiti_marks_card_synced_and_passes_raw_episodes(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        self.service.repo.upsert_raw_episodes(raw)
        saved = self.service.repo.upsert_cards(cards)
        card_id = saved[0]["card_id"]
        graphiti = MagicMock()
        graphiti.is_available.return_value = True
        graphiti.ingest_news_signal_card_sync.return_value = {"status": "synced", "episode_name": "news_signal:test"}
        graphiti.sync_news_signal_edges_sync.return_value = {"status": "ok", "edges": 3}

        with patch("src.services.graphiti.graph_service.get_graphiti_service", return_value=graphiti):
            result = self.service.sync_graphiti(signal_date="2026-07-01")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["synced"], 1)
        synced_card = self.service.get_card(card_id)
        self.assertIsNotNone(synced_card)
        self.assertEqual(synced_card["graph_sync_status"], "synced")
        call_kwargs = graphiti.ingest_news_signal_card_sync.call_args.kwargs
        self.assertEqual(call_kwargs["market"], "cn")
        self.assertEqual(call_kwargs["card"]["card_id"], card_id)
        self.assertEqual(call_kwargs["card"]["raw_episodes"][0]["episode_id"], raw[0]["episode_id"])

    def test_sync_graphiti_can_project_edges_without_slow_episode_ingestion(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)
        graphiti = MagicMock()
        graphiti.is_available.return_value = True
        graphiti.sync_news_signal_edges_sync.return_value = {"status": "ok", "edges": 3}

        with patch("src.services.graphiti.graph_service.get_graphiti_service", return_value=graphiti):
            result = self.service.sync_graphiti(
                signal_date="2026-07-01",
                include_episodes=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["episode_sync"]["reason"], "disabled_by_request")
        graphiti.ingest_news_signal_card_sync.assert_not_called()
        graphiti.sync_news_signal_edges_sync.assert_called_once()

    def test_sync_graphiti_reports_partial_when_edge_projection_fails(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        self.service.repo.upsert_raw_episodes(raw)
        self.service.repo.upsert_cards(cards)
        graphiti = MagicMock()
        graphiti.is_available.return_value = True
        graphiti.sync_news_signal_edges_sync.return_value = {
            "status": "failed",
            "error": "neo4j unavailable",
            "edges": 0,
        }

        with patch("src.services.graphiti.graph_service.get_graphiti_service", return_value=graphiti):
            result = self.service.sync_graphiti(
                signal_date="2026-07-01",
                include_episodes=False,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["graph_edge_sync"]["status"], "failed")

    def test_projection_only_without_date_rebuilds_all_active_card_dates(self) -> None:
        for target in (date(2026, 7, 1), date(2026, 7, 2)):
            raw, cards = self.service._build_from_cjzc(_cjzc_payload(), target)
            self.service.repo.upsert_raw_episodes(raw)
            self.service.repo.upsert_cards(cards)
        graphiti = MagicMock()
        graphiti.is_available.return_value = True
        graphiti.sync_news_signal_edges_sync.return_value = {"status": "ok", "edges": 6}

        with patch("src.services.graphiti.graph_service.get_graphiti_service", return_value=graphiti):
            result = self.service.sync_graphiti(
                include_episodes=False,
                limit=500,
            )

        self.assertEqual(result["edge_sync"]["cards"], 2)
        projected_cards = graphiti.sync_news_signal_edges_sync.call_args.kwargs["cards"]
        self.assertEqual(len(projected_cards), 2)

    def test_sync_graphiti_disabled_keeps_card_pending(self) -> None:
        raw, cards = self.service._build_from_cjzc(_cjzc_payload(), date(2026, 7, 1))
        self.service.repo.upsert_raw_episodes(raw)
        saved = self.service.repo.upsert_cards(cards)
        card_id = saved[0]["card_id"]
        graphiti = MagicMock()
        graphiti.is_available.return_value = False

        with patch("src.services.graphiti.graph_service.get_graphiti_service", return_value=graphiti):
            result = self.service.sync_graphiti(signal_date="2026-07-01")

        self.assertEqual(result["status"], "disabled")
        pending_card = self.service.get_card(card_id)
        self.assertIsNotNone(pending_card)
        self.assertEqual(pending_card["graph_sync_status"], "pending")

    def test_rebuild_edges_creates_typed_and_event_clue_edges_idempotently(self) -> None:
        cards = [
            _edge_card("card:test:a", "机器人订单增长，产业链客户验证加速", "机器人", "300024", "机器人公司"),
            _edge_card("card:test:b", "机器人核心零部件涨价，机器人公司受益", "机器人", "300024", "机器人公司"),
        ]
        cards[1]["transmission_paths"] = [
            {
                "event_category": "价格/供需",
                "mechanism": "核心零部件涨价传导",
                "target": "机器人产业链公司",
                "conclusion": "涨价需要订单和利润率继续验证",
                "chain_steps": [
                    {"label": "事件", "text": "核心零部件涨价"},
                    {"label": "传导", "text": "产业链成本与议价能力变化"},
                ],
            }
        ]
        self.service.repo.upsert_cards(cards)

        first = self.service.rebuild_edges(signal_date="2026-07-04")
        second = self.service.rebuild_edges(signal_date="2026-07-04")
        edges = self.service.list_edges(signal_date="2026-07-04", limit=100)["items"]
        edge_types = {item["edge_type"] for item in edges}

        self.assertEqual(first["cards"], 2)
        self.assertEqual(second["edges_upserted"], len(edges))
        self.assertIn("impacts_industry", edge_types)
        self.assertIn("impacts_company", edge_types)
        self.assertIn("same_company", edge_types)
        self.assertTrue(all(item["edge_quality"] > 0 for item in edges))
        self.assertIn("high", {item["quality_grade"] for item in edges})
        graph = self.service.card_graph("card:test:a")
        self.assertGreaterEqual(graph["summary"]["edge_count"], 3)
        self.assertIn("edge_quality_counts", graph["summary"])
        related_node = next(node for node in graph["nodes"] if node["id"] == "card:test:b")
        self.assertEqual(related_node["label"], "机器人核心零部件涨价，机器人公司受益")
        self.assertEqual(related_node["transmission_paths"][0]["event_category"], "价格/供需")
        related_edge = next(edge for edge in graph["edges"] if edge.get("target_card_id") == "card:test:b")
        self.assertEqual(related_edge["target_label"], "机器人核心零部件涨价，机器人公司受益")
        self.assertEqual(related_edge["target_signal_date"], "2026-07-04")

        with DatabaseManager.get_instance().get_session() as session:
            edge_count = session.execute(select(func.count(NewsSignalEdge.id))).scalar()
        self.assertEqual(edge_count, len(edges))

    def test_rebuild_edges_can_create_semantic_similarity_from_vectors(self) -> None:
        cards = [
            _edge_card("card:test:macro-a", "美国非农超预期，美联储降息预期降温", "海外宏观", "", ""),
            _edge_card("card:test:macro-b", "就业数据强劲，美元指数上行", "海外宏观", "", ""),
            _edge_card("card:test:industry-c", "消费电子新品订单改善", "消费电子", "", ""),
        ]
        self.service.repo.upsert_cards(cards)

        result = self.service.rebuild_edges(
            signal_date="2026-07-04",
            include_semantic=True,
            semantic_vectors={
                "card:test:macro-a": [1.0, 0.0],
                "card:test:macro-b": [0.98, 0.08],
                "card:test:industry-c": [0.0, 1.0],
            },
        )

        self.assertEqual(result["semantic"]["status"], "ok")
        edges = self.service.list_edges(signal_date="2026-07-04", edge_class="semantic_similarity", limit=20)["items"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["edge_type"], "semantic_similarity")
        self.assertGreater(edges[0]["weight"], 0.76)
        self.assertGreaterEqual(edges[0]["edge_quality"], 55)
        self.assertIn("semantic_not_causal", edges[0]["quality_flags"])
        self.assertEqual(result["semantic"]["top_k_per_card"], 4)

    def test_rebuild_edges_distinguishes_same_event_from_same_theme(self) -> None:
        first = _edge_card(
            "card:event:a",
            "江波龙披露存储芯片订单增长并进入客户交付",
            "存储芯片",
            "301308",
            "江波龙",
        )
        second = _edge_card(
            "card:event:b",
            "江波龙存储芯片订单增长，客户交付进度更新",
            "存储芯片",
            "301308",
            "江波龙",
        )
        theme_only = _edge_card(
            "card:event:c",
            "存储芯片行业报价变化，关注供需拐点",
            "存储芯片",
            "688525",
            "佰维存储",
        )
        for card in (first, second, theme_only):
            card["transmission_paths"] = [{"event_category": "大客户/订单"}]
        theme_only["transmission_paths"] = [{"event_category": "价格/供需"}]
        self.service.repo.upsert_cards([first, second, theme_only])

        self.service.rebuild_edges(signal_date="2026-07-04")
        edges = self.service.list_edges(signal_date="2026-07-04", limit=100)["items"]
        by_pair = {
            frozenset((edge["source_card_id"], edge.get("target_card_id"))): edge
            for edge in edges
            if edge.get("target_type") == "card"
        }

        self.assertEqual(by_pair[frozenset(("card:event:a", "card:event:b"))]["edge_type"], "same_event")
        self.assertEqual(by_pair[frozenset(("card:event:a", "card:event:c"))]["edge_type"], "same_theme")

    def test_rebuild_edges_does_not_connect_unrelated_cards_through_generic_fallback_theme(self) -> None:
        payment = _edge_card(
            "card:generic:payment",
            "第三方支付机构因清算违规被处罚",
            "实时快讯",
            "",
            "",
        )
        semiconductor = _edge_card(
            "card:generic:semiconductor",
            "电子特气板块调整，半导体材料走弱",
            "实时快讯",
            "",
            "",
        )
        self.service.repo.upsert_cards([payment, semiconductor])

        self.service.rebuild_edges(signal_date="2026-07-04")
        event_edges = self.service.list_edges(
            signal_date="2026-07-04",
            edge_class="event_clue",
            limit=100,
        )["items"]
        all_edges = self.service.list_edges(signal_date="2026-07-04", limit=100)["items"]

        self.assertEqual(event_edges, [])
        self.assertNotIn("industry:实时快讯", {edge["target_id"] for edge in all_edges})

    def test_rebuild_edges_removes_relations_for_cards_that_are_no_longer_active(self) -> None:
        active = _edge_card(
            "card:cleanup:active",
            "商业航天订单增长",
            "商业航天",
            "",
            "",
        )
        removed = _edge_card(
            "card:cleanup:removed",
            "商业航天融资推进",
            "商业航天",
            "",
            "",
        )
        self.service.repo.upsert_cards([active, removed])
        self.service.rebuild_edges(signal_date="2026-07-04")
        self.assertTrue(self.service.repo.list_edges(card_id=removed["card_id"], limit=20))

        removed["status"] = "suppressed"
        self.service.repo.upsert_cards([removed])
        self.service.rebuild_edges(signal_date="2026-07-04")

        self.assertEqual(self.service.repo.list_edges(card_id=removed["card_id"], limit=20), [])

    def test_same_event_reconciliation_merges_sources_and_suppresses_duplicates(self) -> None:
        first = _edge_card(
            "card:merge:a",
            "江波龙披露存储芯片订单增长并进入客户交付",
            "存储芯片",
            "301308",
            "江波龙",
        )
        second = _edge_card(
            "card:merge:b",
            "江波龙存储芯片订单增长，客户交付进度更新",
            "存储芯片",
            "301308",
            "江波龙",
        )
        first["signal_score"] = 85.0
        second["signal_score"] = 75.0
        for index, card in enumerate((first, second), start=1):
            raw = {
                "episode_id": f"raw:merge:{index}",
                "dedup_key": f"dedup:merge:{index}",
                "source": "cls_telegraph",
                "title": card["summary_short"],
                "published_at": datetime(2026, 7, 4, 10, index),
                "signal_date": date(2026, 7, 4),
                "session": "intraday",
            }
            self.service.repo.upsert_raw_episodes([raw])
            card["raw_episode_ids"] = [raw["episode_id"]]
            card["transmission_paths"] = [{"event_category": "大客户/订单"}]
        self.service.repo.upsert_cards([first, second])

        result = self.service.reconcile_same_event_clusters(signal_date="2026-07-04")
        active = self.service.list_cards(signal_date="2026-07-04", status="active")["items"]
        canonical = self.service.get_card("card:merge:a")
        duplicate = self.service.get_card("card:merge:b")

        self.assertEqual(result["clusters_merged"], 1)
        self.assertEqual(result["cards_suppressed"], 1)
        self.assertEqual([item["card_id"] for item in active], ["card:merge:a"])
        self.assertEqual(len(canonical["raw_episode_ids"]), 2)
        self.assertEqual(canonical["source_count"], 2)
        self.assertEqual(duplicate["status"], "suppressed")
        self.assertEqual(duplicate["diagnostics"]["event_cluster"]["merged_into_card_id"], "card:merge:a")

    def test_semantic_similarity_edges_are_limited_by_quality_top_k(self) -> None:
        cards = [
            _edge_card(f"card:test:semantic-{idx}", f"机器人产业链事件 {idx}", "机器人", "", "")
            for idx in range(7)
        ]
        self.service.repo.upsert_cards(cards)
        vectors = {
            card["card_id"]: [1.0, 0.01 * idx]
            for idx, card in enumerate(cards)
        }

        result = self.service.rebuild_edges(
            signal_date="2026-07-04",
            include_semantic=True,
            semantic_vectors=vectors,
        )
        edges = self.service.list_edges(signal_date="2026-07-04", edge_class="semantic_similarity", limit=100)["items"]
        degree: dict[str, int] = {}
        for edge in edges:
            source = str(edge["source_card_id"])
            target = str(edge["target_card_id"])
            degree[source] = degree.get(source, 0) + 1
            degree[target] = degree.get(target, 0) + 1

        self.assertEqual(result["semantic"]["status"], "ok")
        self.assertLess(len(edges), 21)
        self.assertTrue(edges)
        self.assertTrue(all(count <= 4 for count in degree.values()))
        self.assertTrue(all(edge["edge_quality"] >= 45 for edge in edges))


def _cjzc_payload() -> dict:
    return {
        "status": "ok",
        "matched_publish_date": "2026-07-01",
        "title": "财经早餐：AI服务器订单增长",
        "summary": "中科曙光AI服务器订单增长，算力产业链景气。",
        "publish_time": "2026-07-01 06:00:00",
        "link": "https://example.test/cjzc",
        "themes": [
            {
                "theme": "AI服务器",
                "keywords": ["AI服务器", "算力"],
                "polarity": "positive",
                "evidence": "中科曙光AI服务器订单增长，客户验证加速。",
                "mapped_stocks": [{"code": "603019", "name": "中科曙光", "role": "算力服务器"}],
                "related_boards": ["算力"],
                "theme_score": 30,
            }
        ],
        "article_sections": [{"section": "热点题材", "text": "中科曙光AI服务器订单增长，客户验证加速。"}],
        "errors": [],
    }


def _dailynews_payload(
    *,
    title: str,
    content: str,
    published_at: str,
    provider: str = "orz.dailynews.cls",
) -> dict:
    return {
        "status": "ok",
        "provider": provider,
        "results": [
            {
                "id": "1",
                "title": title,
                "content": content,
                "snippet": content,
                "url": "https://example.test/news",
                "published_at": published_at,
                "published_ts": 1783134701,
                "score": 1000.0,
                "rank": 1,
                "is_important": True,
                "subjects": [],
                "subject_names": [],
                "stocks": [],
            }
        ],
        "source_chain": [{"provider": provider, "result": "ok"}],
        "errors": [],
    }


class _FakePortfolioAnySearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5, days: int = 7):
        del max_results, days
        self.queries.append(query)
        if "600487" in query:
            return SearchResponse(
                query=query,
                provider="AnySearch",
                success=True,
                results=[
                    SearchResult(
                        title="江苏亨通光电股份有限公司2026年半年度业绩预增公告",
                        snippet="证券代码：600487 证券简称：亨通光电，预计2026年上半年归母净利润同比增长。",
                        url="https://example.test/600487-performance.pdf",
                        source="example.test",
                        published_date="2026-07-14",
                    ),
                    SearchResult(
                        title="亨通光电(600487)_最新价格_行情_走势图",
                        snippet="行情中心页面，展示最新价、成交额和盘口。",
                        url="https://example.test/600487-quote",
                        source="example.test",
                        published_date="2026-07-21",
                    ),
                ],
            )
        if "600667" in query:
            return SearchResponse(
                query=query,
                provider="AnySearch",
                success=True,
                results=[
                    SearchResult(
                        title="A股异动丨太极实业跌超6%，董事王毅勃拟减持5.03万股",
                        snippet="太极实业600667公告称，董事拟减持公司股份，短期风险偏好承压。",
                        url="https://example.test/600667-reduce",
                        source="example.test",
                        published_date="2026-07-14",
                    ),
                    SearchResult(
                        title="太极实业(600667)公司公告",
                        snippet="新浪财经公司公告列表页。",
                        url="https://example.test/600667-notice-list",
                        source="example.test",
                        published_date=None,
                    ),
                ],
            )
        return SearchResponse(query=query, provider="AnySearch", success=True, results=[])


def _edge_card(card_id: str, summary: str, industry: str, symbol: str, name: str) -> dict:
    company_impacts = []
    if symbol or name:
        company_impacts.append(
            {
                "symbol": symbol,
                "name": name or symbol,
                "direction": "benefit",
                "confidence": 0.9,
                "mapping_status": "mapped",
                "role": "test",
                "rationale": "测试映射",
            }
        )
    return {
        "card_id": card_id,
        "signal_date": date(2026, 7, 4),
        "session": "intraday",
        "signal_layer": "company" if company_impacts else "industry",
        "summary_short": summary,
        "news_tone": "positive",
        "market_impact": "positive",
        "impact_horizon": "medium",
        "valid_from": datetime(2026, 7, 4, 10, 0, 0),
        "valid_until": datetime(2026, 7, 18, 10, 0, 0),
        "decay_rule": "2w",
        "evidence_grade": "confirmed",
        "inference_level": "explicit",
        "mapping_status": "mapped" if company_impacts else "industry_only",
        "mapping_confidence": 0.9 if company_impacts else 0.35,
        "signal_score": 80.0,
        "status": "active",
        "primary_industries": [industry],
        "secondary_industries": [],
        "explicit_entities": [name] if name else [],
        "industry_impacts": [{"industry": industry, "direction": "benefit", "strength": "medium", "rationale": summary}],
        "company_impacts": company_impacts,
        "transmission_paths": [],
        "raw_episode_ids": [],
        "source_chain": [],
        "diagnostics": {"source": "test"},
        "source_count": 1,
        "graph_sync_status": "pending",
    }
