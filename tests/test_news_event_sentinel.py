# -*- coding: utf-8 -*-
"""Behavior tests for the news event sentinel module."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.repositories.news_event_sentinel_repo import NewsEventSentinelRepository
from src.services.news_event_sentinel import ConfigWatchedUniverseProvider, NewsEventSentinel, NewsSignalCardProvider, WatchedSymbol, WatchedUniverse
from src.storage import DatabaseManager, PortfolioAccount, PortfolioPosition


class _FakeUniverseProvider:
    def load(self, *, now: datetime) -> WatchedUniverse:
        return WatchedUniverse(
            holdings=[WatchedSymbol(symbol="600519", name="贵州茅台", source="portfolio")],
            watchlist=[WatchedSymbol(symbol="300750", name="宁德时代", source="stock_list")],
            candidate_symbols=[],
            macro_queries=[],
            source_queries=["600519 news"],
            symbol_aliases={"600519": ["贵州茅台", "茅台"]},
            loaded_at=now,
        )


class _FakeCardProvider:
    def __init__(self, cards):
        self.cards = cards
        self.calls = 0

    def fetch_cards(self, *, universe: WatchedUniverse, now: datetime, limit: int):
        self.calls += 1
        return {
            "status": "ok",
            "fetched_count": len(self.cards),
            "unseen_count": len(self.cards),
            "raw_episode_count": len(self.cards),
            "card_count": len(self.cards),
            "cards": list(self.cards),
            "errors": [],
            "diagnostics": {"provider": "fake"},
        }


class _FakeNotifier:
    def __init__(self):
        self.envelopes = []

    def send(self, envelope):
        self.envelopes.append(envelope)
        return {"status": "sent", "channel": "fake"}


class _FailingCardProvider:
    def fetch_cards(self, *, universe: WatchedUniverse, now: datetime, limit: int):
        return {
            "status": "failed",
            "fetched_count": 0,
            "unseen_count": 0,
            "raw_episode_count": 0,
            "card_count": 0,
            "cards": [],
            "errors": [{"source": "fake", "error": "upstream unavailable"}],
            "diagnostics": {"provider": "failing_fake"},
        }


def _holding_risk_card(now: datetime):
    return {
        "card_id": "card:cls:600519:negative-guidance",
        "summary_short": "贵州茅台下调全年收入指引，渠道库存压力上升。",
        "news_tone": "negative",
        "status": "active",
        "signal_score": 86.0,
        "evidence_grade": "confirmed",
        "mapping_confidence": 0.92,
        "source_count": 2,
        "valid_from": now.isoformat(),
        "company_impacts": [
            {"symbol": "600519", "name": "贵州茅台", "direction": "negative", "impact": "earnings_pressure"}
        ],
        "extracted_events": [
            {
                "event_id": "evt:600519:guidance-cut",
                "event_type": "guidance_cut",
                "direction": "negative",
                "evidence_sentence": "贵州茅台下调全年收入指引。",
                "confidence": 0.91,
            }
        ],
        "source_chain": [
            {"title": "茅台下调全年收入指引", "url": "https://example.test/news/1", "source": "cls"}
        ],
        "transmission_paths": [
            {
                "path": "收入指引下调 -> 渠道库存压力 -> 盈利预期下修",
                "mechanism": "earnings_expectation_revision",
                "target": "600519",
            }
        ],
    }


def _positive_industry_card(now: datetime, *, score: float = 59.5):
    return {
        "card_id": "card:cls:positive-industry",
        "summary_short": "国产算力超节点落地，全国算力网加速建设，晶圆代工环节受益。",
        "news_tone": "positive",
        "status": "active",
        "signal_layer": "industry",
        "signal_score": score,
        "evidence_grade": "speculative",
        "mapping_confidence": 0.35,
        "source_count": 1,
        "valid_from": now.isoformat(),
        "primary_industries": ["国产算力"],
        "secondary_industries": ["半导体", "晶圆代工"],
        "company_impacts": [],
        "extracted_events": [
            {
                "event_id": "evt:positive-industry",
                "event_type": "industry_positive_signal",
                "direction": "positive",
                "evidence_sentence": "国产算力超节点落地，全国算力网加速建设。",
                "confidence": 0.68,
            }
        ],
        "source_chain": [
            {"title": "国产算力超节点落地", "url": "https://example.test/positive", "source": "cls"}
        ],
        "transmission_paths": [
            {
                "path": "国产算力建设 -> 晶圆代工需求 -> 半导体景气",
                "mechanism": "industry_positive_signal",
                "target": "国产算力",
            }
        ],
    }


def _negative_industry_card(now: datetime, *, score: float = 62.0):
    return {
        "card_id": "card:cls:negative-industry",
        "summary_short": "海外AI服务器订单延后，相关高估值硬件链短期承压。",
        "news_tone": "negative",
        "status": "active",
        "signal_layer": "industry",
        "signal_score": score,
        "evidence_grade": "speculative",
        "mapping_confidence": 0.2,
        "source_count": 1,
        "valid_from": now.isoformat(),
        "primary_industries": ["AI硬件"],
        "secondary_industries": ["服务器", "光模块"],
        "company_impacts": [],
        "extracted_events": [
            {
                "event_id": "evt:negative-industry",
                "event_type": "industry_negative_signal",
                "direction": "negative",
                "evidence_sentence": "海外AI服务器订单延后。",
                "confidence": 0.66,
            }
        ],
        "source_chain": [
            {"title": "海外AI服务器订单延后", "url": "https://example.test/negative", "source": "cls"}
        ],
        "transmission_paths": [
            {
                "path": "订单延后 -> 业绩兑现节奏放慢 -> 高估值硬件链承压",
                "mechanism": "risk_avoidance",
                "target": "AI硬件",
            }
        ],
    }


def _macro_card(now: datetime, *, card_id: str, summary: str, tone: str = "negative", score: float = 82.0):
    return {
        "card_id": card_id,
        "summary_short": summary,
        "news_tone": tone,
        "status": "active",
        "signal_layer": "macro",
        "signal_score": score,
        "evidence_grade": "plausible",
        "mapping_confidence": 0.0,
        "source_count": 2,
        "valid_from": now.isoformat(),
        "primary_industries": ["海外宏观"],
        "secondary_industries": [],
        "explicit_entities": [],
        "company_impacts": [],
        "extracted_events": [
            {
                "event_id": f"evt:{card_id}",
                "event_type": "macro_policy",
                "direction": tone,
                "evidence_sentence": summary,
                "confidence": 0.78,
            }
        ],
        "source_chain": [
            {"title": summary[:30], "url": "https://example.test/macro", "source": "macro"}
        ],
        "transmission_paths": [
            {
                "path": "宏观数据 -> 风险偏好 -> 指数波动",
                "mechanism": "macro_risk_appetite",
                "target": "macro",
            }
        ],
    }


class NewsEventSentinelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "sentinel.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519,300750",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                    "NEWS_EVENT_SENTINEL_ENABLED=true",
                    "NEWS_EVENT_SENTINEL_MIN_SEVERITY=mid",
                    "NEWS_EVENT_SENTINEL_COOLDOWN_MINUTES=120",
                    "NEWS_EVENT_SENTINEL_TRIGGER_MODE=notify_only",
                    "NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["STOCK_LIST"] = "600519,300750"
        os.environ["NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED"] = "false"
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.config = Config.get_instance()
        self.repo = NewsEventSentinelRepository(DatabaseManager.get_instance())

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("STOCK_LIST", None)
        os.environ.pop("NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED", None)
        self.temp_dir.cleanup()

    def test_high_severity_holding_event_records_trigger_and_notification_envelope(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_risk_card(now)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["suppressed_by_cooldown"], 0)
        self.assertEqual(len(notifier.envelopes), 1)
        self.assertEqual(notifier.envelopes[0].card_id, card["card_id"])
        self.assertEqual(notifier.envelopes[0].symbols, ["600519"])
        self.assertEqual(notifier.envelopes[0].severity, "high")
        self.assertIn("持仓命中", notifier.envelopes[0].why_triggered)
        self.assertEqual(
            notifier.envelopes[0].transmission_paths[0]["mechanism"],
            "earnings_expectation_revision",
        )

        runs = self.repo.list_runs(limit=5)
        triggers = self.repo.list_triggers(limit=5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(runs[0]["trigger_count"], 1)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["card_id"], card["card_id"])
        self.assertEqual(triggers[0]["canonical_symbol"], "600519")
        self.assertEqual(triggers[0]["notification_status"], "sent")

    def test_a_share_macro_event_triggers_without_company_match(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _macro_card(
            now,
            card_id="card:macro:a-share-liquidity",
            summary="人民银行开展5000亿元逆回购操作，A股流动性预期改善。",
            tone="positive",
        )
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "MACRO:A_SHARE")
        self.assertEqual(notifier.envelopes[0].symbols, ["MACRO:A_SHARE"])
        self.assertIn("宏观命中", notifier.envelopes[0].why_triggered)
        self.assertIn("A股宏观", notifier.envelopes[0].why_triggered)

    def test_us_macro_event_triggers_without_company_match(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _macro_card(
            now,
            card_id="card:macro:us-nfp",
            summary="美国6月非农数据超预期，美联储降息预期降温，美股风险资产波动加大。",
        )
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "MACRO:US")
        self.assertEqual(notifier.envelopes[0].symbols, ["MACRO:US"])
        self.assertIn("宏观命中", notifier.envelopes[0].why_triggered)
        self.assertIn("美股宏观", notifier.envelopes[0].why_triggered)

    def test_non_target_macro_event_does_not_trigger(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _macro_card(
            now,
            card_id="card:macro:ecb",
            summary="欧洲央行释放利率路径信号，欧元区资产波动加大。",
        )
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)

    def test_positive_industry_signal_triggers_without_company_match(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _positive_industry_card(now)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "SIGNAL:POSITIVE")
        self.assertEqual(notifier.envelopes[0].symbols, ["SIGNAL:POSITIVE"])
        self.assertIn("正向线索", notifier.envelopes[0].why_triggered)
        self.assertIn("主题=国产算力", notifier.envelopes[0].why_triggered)
        self.assertEqual(notifier.envelopes[0].severity, "mid")

    def test_negative_industry_signal_triggers_without_company_match(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _negative_industry_card(now)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "SIGNAL:NEGATIVE")
        self.assertEqual(result["triggers"][0]["direction"], "negative")
        self.assertEqual(notifier.envelopes[0].symbols, ["SIGNAL:NEGATIVE"])
        self.assertIn("负向避险线索", notifier.envelopes[0].why_triggered)
        self.assertIn("主题=AI硬件", notifier.envelopes[0].why_triggered)

    def test_low_score_negative_industry_signal_does_not_trigger(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _negative_industry_card(now, score=49.0)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)

    def test_low_score_positive_industry_signal_does_not_trigger(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _positive_industry_card(now, score=49.0)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)

    def test_stale_card_is_scanned_but_not_sent_as_market_trigger(self) -> None:
        now = datetime(2026, 7, 20, 14, 15, 0)
        self.config.news_event_sentinel_card_max_age_minutes = 30
        stale_card = _positive_industry_card(datetime(2026, 7, 20, 11, 6, 0))
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([stale_card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["cards_scanned"], 1)
        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)
        self.assertEqual(self.repo.list_runs(limit=1)[0]["diagnostics"]["stale_card_suppressed"], 1)

    def test_heartbeat_reports_stale_card_suppression(self) -> None:
        now = datetime(2026, 7, 20, 14, 15, 0)
        self.config.news_event_sentinel_card_max_age_minutes = 30
        self.config.news_event_sentinel_heartbeat_enabled = True
        self.config.news_event_sentinel_heartbeat_interval_minutes = 10
        stale_card = _positive_industry_card(datetime(2026, 7, 20, 11, 6, 0))
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([stale_card]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "SENTINEL:HEARTBEAT")
        self.assertIn("旧卡压制=1", notifier.envelopes[0].why_triggered)
        self.assertIn("1 张旧卡片超过新鲜度窗口", notifier.envelopes[0].summary)

    def test_heartbeat_sends_when_no_market_trigger_is_due(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        self.config.news_event_sentinel_heartbeat_enabled = True
        self.config.news_event_sentinel_heartbeat_interval_minutes = 10
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "SENTINEL:HEARTBEAT")
        self.assertEqual(result["triggers"][0]["event_type"], "heartbeat")
        self.assertEqual(result["triggers"][0]["notification_status"], "sent")
        self.assertEqual(notifier.envelopes[0].severity, "low")
        self.assertEqual(notifier.envelopes[0].direction, "neutral")
        self.assertIn("哨兵存活", notifier.envelopes[0].why_triggered)
        self.assertIn("本轮扫描 0 张新闻信号卡片", notifier.envelopes[0].summary)

    def test_heartbeat_respects_own_interval_cooldown(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        self.config.news_event_sentinel_heartbeat_enabled = True
        self.config.news_event_sentinel_heartbeat_interval_minutes = 10
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([]),
            notifier=notifier,
        )

        first = sentinel.run_once(now=now)
        second = sentinel.run_once(now=datetime(2026, 7, 20, 10, 20, 0))

        self.assertEqual(first["triggered"], 1)
        self.assertEqual(second["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 1)
        self.assertEqual(len(self.repo.list_triggers(limit=5)), 1)

    def test_default_feishu_notifier_sends_interactive_card_with_paths(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        self.config.news_event_sentinel_feishu_enabled = True
        self.config.feishu_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
        self.config.feishu_webhook_secret = "secret"
        self.config.feishu_webhook_keyword = "StockAnalyser"
        self.config.webhook_verify_ssl = False
        response = SimpleNamespace(
            status_code=200,
            text='{"code":0,"msg":"success"}',
            json=lambda: {"code": 0, "msg": "success"},
        )
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([_holding_risk_card(now)]),
        )

        with patch("src.services.news_event_sentinel.requests.post", return_value=response) as post:
            result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["notification_status"], "sent")
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 30)
        self.assertFalse(kwargs["verify"])
        payload = kwargs["json"]
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertIn("timestamp", payload)
        self.assertIn("sign", payload)
        card = payload["card"]
        self.assertEqual(card["header"]["template"], "red")
        markdown = card["elements"][0]["text"]["content"]
        self.assertIn("StockAnalyser", markdown)
        self.assertIn("关联传导路径", markdown)
        self.assertIn("earnings_expectation_revision", markdown)
        self.assertIn("[茅台下调全年收入指引](https://example.test/news/1)", markdown)

    def test_default_feishu_notifier_skips_when_webhook_missing(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        self.config.news_event_sentinel_feishu_enabled = True
        self.config.feishu_webhook_url = None
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([_holding_risk_card(now)]),
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["triggers"][0]["notification_status"], "skipped")
        diagnostics = self.repo.list_triggers(limit=1)[0]["diagnostics"]
        self.assertEqual(diagnostics["notification_result"]["reason"], "missing_webhook_url")

    def test_same_event_inside_cooldown_records_run_without_duplicate_trigger(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_risk_card(now)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
        )

        first = sentinel.run_once(now=now)
        second = sentinel.run_once(now=datetime(2026, 7, 20, 10, 45, 0))

        self.assertEqual(first["triggered"], 1)
        self.assertEqual(second["triggered"], 0)
        self.assertEqual(second["suppressed_by_cooldown"], 1)
        self.assertEqual(len(notifier.envelopes), 1)
        self.assertEqual(len(self.repo.list_runs(limit=5)), 2)
        self.assertEqual(len(self.repo.list_triggers(limit=5)), 1)

    def test_outside_active_window_records_skipped_run_without_fetching_sources(self) -> None:
        now = datetime(2026, 7, 20, 3, 30, 0)
        provider = _FakeCardProvider([_holding_risk_card(now)])
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=provider,
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["status"], "skipped_inactive_window")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)
        runs = self.repo.list_runs(limit=5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "skipped_inactive_window")

    def test_default_card_provider_reuses_ingestion_before_monitoring_cards(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        service = MagicMock()
        service.ingest_cls_incremental.return_value = {
            "status": "ok",
            "fetched_items": 2,
            "new_raw_episodes": 1,
            "errors": [],
        }
        service.list_cards.return_value = {"items": [_holding_risk_card(now)]}
        provider = NewsSignalCardProvider(config=self.config)

        with patch("src.services.news_signal_service.NewsSignalService", return_value=service) as service_cls:
            result = provider.fetch_cards(
                universe=_FakeUniverseProvider().load(now=now),
                now=now,
                limit=20,
            )

        service_cls.assert_called_once_with()
        service.ingest_cls_incremental.assert_called_once_with(limit=20)
        service.list_cards.assert_called_once_with(signal_date="2026-07-20", limit=20)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fetched_count"], 2)
        self.assertEqual(result["unseen_count"], 1)
        self.assertEqual(result["card_count"], 1)
        self.assertEqual(result["diagnostics"]["source"], "news_signal_cls_incremental")

    def test_default_card_provider_treats_empty_ingest_with_cards_as_ok(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        service = MagicMock()
        service.ingest_cls_incremental.return_value = {
            "status": "empty",
            "fetched_items": 50,
            "new_raw_episodes": 0,
            "errors": [],
        }
        service.list_cards.return_value = {"items": [_holding_risk_card(now)]}
        provider = NewsSignalCardProvider(config=self.config)

        with patch("src.services.news_signal_service.NewsSignalService", return_value=service):
            result = provider.fetch_cards(
                universe=_FakeUniverseProvider().load(now=now),
                now=now,
                limit=50,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fetched_count"], 50)
        self.assertEqual(result["card_count"], 1)

    def test_source_failure_marks_run_failed(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FailingCardProvider(),
            notifier=_FakeNotifier(),
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["triggered"], 0)
        runs = self.repo.list_runs(limit=5)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["errors"][0]["error"], "upstream unavailable")

    def test_disabled_trigger_mode_scans_without_creating_trigger(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        self.config.news_event_sentinel_trigger_mode = "disabled"
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([_holding_risk_card(now)]),
            notifier=notifier,
        )

        result = sentinel.run_once(now=now)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["cards_scanned"], 1)
        self.assertEqual(result["triggered"], 0)
        self.assertEqual(len(notifier.envelopes), 0)
        self.assertEqual(len(self.repo.list_triggers(limit=5)), 0)
        self.assertEqual(self.repo.list_runs(limit=5)[0]["diagnostics"]["trigger_mode"], "disabled")

    def test_config_universe_provider_includes_active_portfolio_positions(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        with DatabaseManager.get_instance().get_session() as session:
            account = PortfolioAccount(
                name="main",
                broker="manual",
                market="cn",
                base_currency="CNY",
                is_active=True,
            )
            inactive = PortfolioAccount(
                name="old",
                broker="manual",
                market="cn",
                base_currency="CNY",
                is_active=False,
            )
            session.add_all([account, inactive])
            session.flush()
            session.add(
                PortfolioPosition(
                    account_id=account.id,
                    cost_method="fifo",
                    symbol="688111",
                    market="cn",
                    currency="CNY",
                    quantity=10,
                )
            )
            session.add(
                PortfolioPosition(
                    account_id=inactive.id,
                    cost_method="fifo",
                    symbol="000001",
                    market="cn",
                    currency="CNY",
                    quantity=10,
                )
            )
            session.commit()

        universe = ConfigWatchedUniverseProvider(self.config).load(now=now)

        self.assertIn("688111", universe.holding_symbols)
        self.assertIn("688111", universe.watched_symbols)
        self.assertNotIn("000001", universe.holding_symbols)
        self.assertIn("600519", universe.watched_symbols)


if __name__ == "__main__":
    unittest.main()
