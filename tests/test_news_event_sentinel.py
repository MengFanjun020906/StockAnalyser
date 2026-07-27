# -*- coding: utf-8 -*-
"""Behavior tests for the news event sentinel module."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.repositories.news_event_sentinel_repo import NewsEventSentinelRepository
from src.services.news_event_sentinel import (
    ConfigWatchedUniverseProvider,
    FeishuSentinelNotifier,
    GraphitiSentinelTraceProvider,
    NewsEventSentinel,
    NewsSignalCardProvider,
    WatchedSymbol,
    WatchedUniverse,
    _build_envelope,
)
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


class _FakeTraceProvider:
    def __init__(self, trace):
        self.payload = trace
        self.calls = []

    def trace(self, card, decision):
        self.calls.append((card, decision))
        return dict(self.payload)


class _FakeGraphitiSearch:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def search_sync(self, query, *, market=None, limit=10, timeout_seconds=None):
        self.calls.append({
            "query": query,
            "market": market,
            "limit": limit,
            "timeout_seconds": timeout_seconds,
        })
        return dict(self.result)


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


def _holding_harm_card(now: datetime):
    card = _holding_risk_card(now)
    card["card_id"] = "card:portfolio-anysearch:600519:loss"
    card["summary_short"] = "贵州茅台公告称预计阶段性净利润同比下降，经营压力上升。"
    card["news_tone"] = "negative"
    card["company_impacts"] = [
        {"symbol": "600519", "name": "贵州茅台", "direction": "harm", "impact": "earnings_pressure"}
    ]
    card["extracted_events"] = [
        {
            "event_id": "evt:600519:loss-warning",
            "event_type": "业绩验证",
            "direction": "harm",
            "evidence_sentence": "贵州茅台预计阶段性净利润同比下降。",
            "confidence": 0.88,
        }
    ]
    return card


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


def _turnover_signal_card(now: datetime, *, card_id: str, summary: str):
    card = _positive_industry_card(now)
    card["card_id"] = card_id
    card["summary_short"] = summary
    card["primary_industries"] = ["盘面直播"]
    card["secondary_industries"] = []
    card["source_chain"] = [{"title": summary, "url": f"https://example.test/{card_id}", "source": "cls"}]
    card["transmission_paths"] = [
        {
            "path": "市场成交额变化 -> 风险偏好 -> 盘面交易活跃度",
            "mechanism": "market_turnover",
            "target": "A股",
        }
    ]
    return card


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

    def test_graphiti_trace_is_attached_to_trigger_and_feishu_markdown(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_risk_card(now)
        notifier = _FakeNotifier()
        trace_provider = _FakeTraceProvider(
            {
                "status": "linked",
                "trace_id": "trace:test:600519",
                "source": "graphiti",
                "edge_count": 2,
                "episode_count": 2,
                "node_count": 2,
                "edges": [
                    {"name": "MENTIONS", "fact": "贵州茅台 -> 收入指引下调 -> 盈利预期下修"},
                    {"name": "MENTIONS"},
                ],
                "episodes": [
                    {"name": "trace:trace-b88763a2f848433dae8d20eca7f2f4dd", "source_description": "agent_trace"},
                    {"summary_short": "渠道库存压力上升"},
                ],
                "nodes": [
                    {"id": "stock:600519", "labels": ["Entity", "Stock"], "name": "贵州茅台"},
                    {"name": "国轩高科", "labels": ["Entity", "MarketEvent"], "summary": "储能订单增长带动电池链关注"},
                ],
            }
        )
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
            trace_provider=trace_provider,
        )

        result = sentinel.run_once(now=now)
        envelope = notifier.envelopes[0]
        markdown = FeishuSentinelNotifier(self.config)._build_markdown(envelope)

        self.assertEqual(result["triggers"][0]["trace_status"], "linked")
        self.assertEqual(result["triggers"][0]["notification_payload"]["trace_id"], "trace:test:600519")
        self.assertEqual(envelope.trace_id, "trace:test:600519")
        self.assertEqual(trace_provider.calls[0][0]["card_id"], card["card_id"])
        self.assertIn("Graphiti 事件跟踪", markdown)
        self.assertIn("状态：已关联 Graphiti", markdown)
        self.assertIn("关联线索", markdown)
        self.assertIn("贵州茅台 -> 收入指引下调", markdown)
        self.assertIn("渠道库存压力上升", markdown)
        self.assertIn("国轩高科（事件）", markdown)
        self.assertNotIn("trace:trace-b88763a2f848433dae8d20eca7f2f4dd", markdown)
        self.assertNotIn("MENTIONS", markdown)
        self.assertNotIn("['Entity'", markdown)

    def test_graphiti_trace_filters_weak_hits_for_turnover_signal_card(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _turnover_signal_card(
            now,
            card_id="card:cls:514782be2569bf49cf0757f4",
            summary="【沪深两市成交额突破2万亿 较上一日此时缩量超3100亿】财联社7月22日电，沪深两市成交额连续第72个交易日突破2万亿，较上一日此时缩量超3100亿。",
        )
        graphiti = _FakeGraphitiSearch(
            {
                "success": True,
                "source": "graphiti",
                "edges": [
                    {"name": "MENTIONS", "fact": "中华网新闻频道提到出口额同比增长199.5%至448.2亿美元"},
                    {"name": "MENTIONS", "fact": "航油价格大幅上涨推高成本，导致交通运输行业成本增加"},
                ],
                "episodes": [
                    {"summary_short": "澎湃新闻报道三大航上半年预亏至少73亿元"},
                    {
                        "name": "trace:trace-b88763a2f848433dae8d20eca7f2f4dd",
                        "source_description": "agent_trace",
                        "content": "沪深两市成交额突破2万亿，但这是旧分析 trace，不应进哨兵卡片。",
                    },
                ],
                "nodes": [
                    {"name": "算力", "labels": ["Entity", "ImpactVariable"], "summary": "政策利好算力"},
                    {"name": "交通运输行业", "labels": ["Sector"], "summary": "航油价格推高成本"},
                ],
            }
        )
        trace_provider = GraphitiSentinelTraceProvider(self.config, graphiti=graphiti)
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=_FakeCardProvider([card]),
            notifier=notifier,
            trace_provider=trace_provider,
        )

        result = sentinel.run_once(now=now)
        envelope = notifier.envelopes[0]
        markdown = FeishuSentinelNotifier(self.config)._build_markdown(envelope)

        self.assertEqual(result["triggers"][0]["trace_status"], "empty")
        self.assertEqual(envelope.graph_trace["fallback_reason"], "filtered_weak_relevance")
        self.assertEqual(envelope.graph_trace["filtered_out_count"], 6)
        self.assertIn("状态：Graphiti 未找到强关联", markdown)
        self.assertIn("已过滤 6 条弱相关命中", markdown)
        self.assertNotIn("出口额同比增长", markdown)
        self.assertNotIn("航油价格", markdown)
        self.assertNotIn("trace-b88763a2f848433dae8d20eca7f2f4dd", markdown)
        self.assertNotIn("算力", markdown)
        self.assertNotIn("交通运输行业", markdown)

    def test_graphiti_trace_filters_polluted_event_watch_episode_for_tax_news(self) -> None:
        now = datetime(2026, 7, 22, 15, 10, 0)
        card = _positive_industry_card(now)
        card.update(
            {
                "card_id": "card:cls:5319db1db574090f8347a10f",
                "summary_short": "【财政部：上半年证券交易印花税1549亿元 同比增长97.3%】财联社7月22日电，财政部发布2026年上半年财政收支情况，上半年，印花税2752亿元，同比增长40.9%。其中，证券交易印花税1549亿元，同比增长97.3%。",
                "primary_industries": ["实时快讯"],
                "secondary_industries": [],
                "explicit_entities": [],
                "transmission_paths": [
                    {
                        "mechanism": "业绩预告验证景气兑现，重点观察实时快讯链条中收入、利润和订单弹性更高的产业链标的。",
                        "target": "实时快讯",
                    }
                ],
            }
        )
        graphiti = _FakeGraphitiSearch(
            {
                "success": True,
                "source": "graphiti",
                "edges": [
                    {"name": "MENTIONS", "fact": "中华网新闻频道提到韩国6月半导体出口额同比增长199.5%至448.2亿美元"},
                    {"name": "MENTIONS", "fact": "中华网新闻频道提到出口额同比增长199.5%至448.2亿美元"},
                    {"name": "MENTIONS", "fact": "澎湃新闻-The Paper报道了航油价格大幅上涨推高成本，导致三大航上半年预亏至少73亿元"},
                ],
                "episodes": [
                    {
                        "name": "market_event:WBG_3_21___-:2026-07-04",
                        "source_description": "event_impact_candidate_discovery",
                        "content": '{"title":"WBG闫盼盼单手解罩3分21视频 单手解内衣为了出名-百度|安全直达官网","snippet":"欧盟议员批准拖延已久的美欧贸易协定。"}',
                    }
                ],
                "nodes": [
                    {"name": "出口", "labels": ["Entity", "ThemeWatch"], "summary": "中华网新闻频道提到出口额同比增长199.5%至448.2亿美元"},
                    {"name": "中华网新闻频道", "labels": ["Entity", "MarketEvent"], "summary": "半导体出口额同比增长"},
                ],
            }
        )
        trace = GraphitiSentinelTraceProvider(self.config, graphiti=graphiti).trace(
            card,
            {
                "symbols": ["SIGNAL:POSITIVE"],
                "event_type": "industry_positive_signal",
                "direction": "positive",
                "severity": "mid",
            },
        )
        envelope = _build_envelope(
            card,
            {
                "symbol": "SIGNAL:POSITIVE",
                "symbols": ["SIGNAL:POSITIVE"],
                "event_type": "industry_positive_signal",
                "direction": "positive",
                "severity": "mid",
                "event_id": "",
                "why_triggered": ["正向线索"],
            },
            graph_trace=trace,
        )
        markdown = FeishuSentinelNotifier(self.config)._build_markdown(envelope)

        self.assertEqual(trace["status"], "empty")
        self.assertEqual(trace["fallback_reason"], "filtered_weak_relevance")
        self.assertIn("Graphiti 未找到强关联", markdown)
        self.assertNotIn("闫盼盼", markdown)
        self.assertNotIn("安全直达官网", markdown)
        self.assertNotIn("出口额同比增长199.5%", markdown)
        self.assertNotIn("航油价格", markdown)

    def test_graphiti_trace_for_company_signal_requires_company_anchor(self) -> None:
        now = datetime(2026, 7, 22, 15, 37, 42)
        card = _positive_industry_card(now)
        card.update(
            {
                "card_id": "card:cls:6c94560fef24060b620d5d9c",
                "summary_short": "【奥浦迈：预计2026年半年度净利润同比增长229%】财联社7月22日电，奥浦迈(688293.SH)公告称，预计2026年半年度归属于母公司所有者的净利润为1.24亿元左右，同比增长229%。",
                "signal_layer": "company",
                "primary_industries": ["A股公告速递", "科创板最新动态"],
                "secondary_industries": [],
                "explicit_entities": ["A股公告速递", "科创板最新动态", "奥浦迈"],
                "company_impacts": [
                    {
                        "symbol": "688293",
                        "name": "奥浦迈",
                        "direction": "benefit",
                        "confidence": 0.9,
                    }
                ],
                "transmission_paths": [
                    {
                        "source": "A股公告速递",
                        "mechanism": "业绩预告验证景气兑现，重点观察A股公告速递链条中收入、利润和订单弹性更高的奥浦迈。",
                        "target": "奥浦迈",
                        "affected_symbols": ["688293"],
                        "evidence_snippets": ["奥浦迈：预计2026年半年度净利润同比增长229%"],
                    }
                ],
            }
        )
        graphiti = _FakeGraphitiSearch(
            {
                "success": True,
                "source": "graphiti",
                "edges": [
                    {"name": "MENTIONS", "fact": "A股公告速递提到韩国6月半导体出口额同比增长199.5%至448.2亿美元"},
                    {"name": "MENTIONS", "fact": "科创板最新动态提到航油价格大幅上涨推高三大航成本"},
                    {"name": "MENTIONS", "fact": "奥浦迈完成澎立生物收购后合并报表范围变化，预计净利润同比增长229%"},
                ],
                "episodes": [
                    {"summary_short": "国金证券研报提到跨境电商二季度业绩超预期"},
                    {"summary_short": "奥浦迈半年度业绩预告显示利润同比高增长"},
                ],
                "nodes": [
                    {"name": "中国石化", "labels": ["Entity", "Stock"], "attributes": {"code": "600111"}},
                    {"name": "奥浦迈", "labels": ["Entity", "Stock"], "attributes": {"code": "688293"}},
                ],
            }
        )

        trace = GraphitiSentinelTraceProvider(self.config, graphiti=graphiti).trace(
            card,
            {
                "symbols": ["SIGNAL:POSITIVE"],
                "event_type": "earnings_validation",
                "direction": "positive",
                "severity": "high",
            },
        )
        envelope = _build_envelope(
            card,
            {
                "symbol": "SIGNAL:POSITIVE",
                "symbols": ["SIGNAL:POSITIVE"],
                "event_type": "earnings_validation",
                "direction": "positive",
                "severity": "high",
                "event_id": "",
                "why_triggered": ["正向线索"],
            },
            graph_trace=trace,
        )
        markdown = FeishuSentinelNotifier(self.config)._build_markdown(envelope)

        self.assertEqual(trace["status"], "linked")
        self.assertEqual(trace["edge_count"], 1)
        self.assertEqual(trace["episode_count"], 1)
        self.assertEqual(trace["node_count"], 1)
        self.assertEqual(trace["filtered_out_count"], 4)
        self.assertIn("奥浦迈完成澎立生物收购", markdown)
        self.assertIn("奥浦迈半年度业绩预告", markdown)
        self.assertIn("奥浦迈(688293)", markdown)
        self.assertNotIn("韩国6月半导体出口额", markdown)
        self.assertNotIn("航油价格", markdown)
        self.assertNotIn("跨境电商", markdown)
        self.assertNotIn("中国石化", markdown)

    def test_holding_event_accepts_benefit_harm_direction_aliases(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_harm_card(now)
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
        self.assertEqual(result["triggers"][0]["direction"], "negative")
        self.assertEqual(result["triggers"][0]["canonical_symbol"], "600519")
        self.assertEqual(notifier.envelopes[0].direction, "negative")

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
        card["diagnostics"] = {
            "positive_signal_quality": {
                "category": "true_positive",
                "label": "真利好",
            }
        }
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
        self.assertIn("利好质量=真利好", notifier.envelopes[0].why_triggered)
        self.assertEqual(notifier.envelopes[0].severity, "mid")

    def test_turnover_milestone_signal_uses_normalized_cooldown(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        first = _turnover_signal_card(
            now,
            card_id="card:cls:turnover-1t",
            summary="【沪深两市成交额突破1万亿 较上一日此时放量超800亿】财联社7月20日电，据财联社盯盘数据显示，沪深两市成交额突破1万亿。",
        )
        second = _turnover_signal_card(
            now,
            card_id="card:cls:turnover-1_5t",
            summary="【沪深两市成交额突破1.5万亿 较上一日此时缩量超50亿】财联社7月20日电，据财联社盯盘数据显示，沪深两市成交额突破1.5万亿。",
        )
        provider = _FakeCardProvider([first])
        notifier = _FakeNotifier()
        sentinel = NewsEventSentinel(
            config=self.config,
            repository=self.repo,
            universe_provider=_FakeUniverseProvider(),
            card_provider=provider,
            notifier=notifier,
        )

        first_result = sentinel.run_once(now=now)
        provider.cards = [second]
        second_result = sentinel.run_once(now=now + timedelta(minutes=30))

        self.assertEqual(first_result["triggered"], 1)
        self.assertEqual(second_result["triggered"], 0)
        self.assertEqual(second_result["suppressed_by_cooldown"], 1)
        self.assertEqual(len(notifier.envelopes), 1)

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

    def test_default_active_window_stops_after_midnight(self) -> None:
        now = datetime(2026, 7, 21, 0, 1, 0)
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

        self.assertEqual(self.config.news_event_sentinel_active_windows, "08:00-23:59")
        self.assertEqual(result["status"], "skipped_inactive_window")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(notifier.envelopes), 0)

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

    def test_default_card_provider_includes_recent_portfolio_anysearch_cards_by_publish_time(self) -> None:
        now = datetime.now().replace(microsecond=0)
        published_at = now - timedelta(days=2)
        from src.services.news_signal_service import NewsSignalService

        service = NewsSignalService()
        card = _holding_risk_card(published_at)
        card["card_id"] = "card:portfolio-anysearch:600519"
        card["signal_date"] = published_at.date()
        card["valid_from"] = published_at
        card["diagnostics"] = {"source": "portfolio_anysearch"}
        service.repo.upsert_cards([card])
        provider = NewsSignalCardProvider(config=self.config)

        with patch.object(
            service,
            "ingest_cls_incremental",
            return_value={"status": "empty", "fetched_items": 0, "new_raw_episodes": 0, "errors": []},
        ), patch.object(
            service,
            "list_cards",
            return_value={"items": []},
        ), patch("src.services.news_signal_service.NewsSignalService", return_value=service):
            result = provider.fetch_cards(
                universe=_FakeUniverseProvider().load(now=now),
                now=now,
                limit=20,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["diagnostics"]["portfolio_anysearch_cards"], 1)
        self.assertEqual(result["cards"][0]["card_id"], "card:portfolio-anysearch:600519")

    def test_default_card_provider_excludes_old_portfolio_anysearch_even_when_updated_recently(self) -> None:
        now = datetime.now().replace(microsecond=0)
        published_at = now - timedelta(days=7)
        from src.services.news_signal_service import NewsSignalService

        service = NewsSignalService()
        card = _holding_risk_card(published_at)
        card["card_id"] = "card:portfolio-anysearch:600519"
        card["signal_date"] = published_at.date()
        card["valid_from"] = published_at
        card["updated_at"] = now
        card["diagnostics"] = {"source": "portfolio_anysearch"}
        service.repo.upsert_cards([card])
        provider = NewsSignalCardProvider(config=self.config)

        with patch.object(
            service,
            "ingest_cls_incremental",
            return_value={"status": "empty", "fetched_items": 0, "new_raw_episodes": 0, "errors": []},
        ), patch.object(
            service,
            "list_cards",
            return_value={"items": []},
        ), patch("src.services.news_signal_service.NewsSignalService", return_value=service):
            result = provider.fetch_cards(
                universe=_FakeUniverseProvider().load(now=now),
                now=now,
                limit=20,
            )

        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["diagnostics"]["portfolio_anysearch_cards"], 0)
        self.assertEqual(result["cards"], [])

    def test_default_card_provider_does_not_drop_portfolio_anysearch_when_base_limit_is_full(self) -> None:
        now = datetime.now().replace(microsecond=0)
        published_at = now - timedelta(days=2)
        from src.services.news_signal_service import NewsSignalService

        service = NewsSignalService()
        for symbol in ("600519", "300750"):
            card = _holding_risk_card(published_at)
            card["card_id"] = f"card:portfolio-anysearch:{symbol}"
            card["signal_date"] = published_at.date()
            card["valid_from"] = published_at
            card["diagnostics"] = {"source": "portfolio_anysearch"}
            card["company_impacts"][0]["symbol"] = symbol
            service.repo.upsert_cards([card])
        base_cards = [{"card_id": f"card:cls:{index}", "summary_short": "CLS"} for index in range(20)]
        provider = NewsSignalCardProvider(config=self.config)

        with patch.object(
            service,
            "ingest_cls_incremental",
            return_value={"status": "empty", "fetched_items": 0, "new_raw_episodes": 0, "errors": []},
        ), patch.object(
            service,
            "list_cards",
            return_value={"items": base_cards},
        ), patch("src.services.news_signal_service.NewsSignalService", return_value=service):
            result = provider.fetch_cards(
                universe=_FakeUniverseProvider().load(now=now),
                now=now,
                limit=20,
            )

        ids = [card["card_id"] for card in result["cards"]]
        self.assertEqual(result["card_count"], 20)
        self.assertIn("card:portfolio-anysearch:600519", ids[:2])
        self.assertIn("card:portfolio-anysearch:300750", ids[:2])

    def test_portfolio_anysearch_freshness_suppresses_old_publish_time_even_when_updated_today(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_risk_card(now - timedelta(days=7))
        card["card_id"] = "card:portfolio-anysearch:600519"
        card["valid_from"] = (now - timedelta(days=7)).isoformat()
        card["updated_at"] = now.isoformat()
        card["diagnostics"] = {"source": "portfolio_anysearch"}
        self.config.news_event_sentinel_card_max_age_minutes = 30
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
        self.assertEqual(result["cards_scanned"], 1)
        self.assertEqual(result["run"]["diagnostics"]["stale_card_suppressed"], 1)
        self.assertEqual(notifier.envelopes, [])

    def test_portfolio_anysearch_freshness_allows_publish_time_inside_three_days(self) -> None:
        now = datetime(2026, 7, 20, 10, 15, 0)
        card = _holding_risk_card(now - timedelta(days=2))
        card["card_id"] = "card:portfolio-anysearch:600519"
        card["valid_from"] = (now - timedelta(days=2)).isoformat()
        card["updated_at"] = now.isoformat()
        card["diagnostics"] = {"source": "portfolio_anysearch"}
        self.config.news_event_sentinel_card_max_age_minutes = 30
        self.config.news_signal_portfolio_anysearch_max_age_days = 3
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
        self.assertEqual(notifier.envelopes[0].card_id, "card:portfolio-anysearch:600519")

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
