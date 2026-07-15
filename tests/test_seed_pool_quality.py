# -*- coding: utf-8 -*-
"""Seed Pool quality monitoring integration tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from src.data import stock_index_loader
from api.app import create_app
from src.config import Config
from src.repositories.news_signal_repo import NewsSignalRepository
from src.services.news_signal_service import NewsSignalService
from src.services.seed_pool_quality_service import SeedPoolQualityService
from src.services.seed_pool_quality_service import effective_seed_pool_date
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class SeedPoolQualityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "seed_pool_quality.db"
        self.sequoia_db_path = self.data_dir / "sequoia_v2.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600001,600002",
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
        os.environ["SEQUOIA_CANDIDATE_DB_PATH"] = str(self.sequoia_db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.client = TestClient(create_app(static_dir=self.data_dir / "empty-static"))
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        stock_index_loader._clear_stock_index_cache_for_tests()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("SEQUOIA_CANDIDATE_DB_PATH", None)
        self.temp_dir.cleanup()

    def _save_bars(self, code: str, rows: list[dict]) -> None:
        df = pd.DataFrame(rows)
        self.db.save_daily_data(df, code=code, data_source="seed-quality-test")

    def _save_sequoia_bars(self, symbol: str, rows: list[dict]) -> None:
        from scripts.update_sequoia_candidates import init_db, upsert_rows

        init_db(str(self.sequoia_db_path))
        upsert_rows(
            str(self.sequoia_db_path),
            [
                (
                    symbol,
                    row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"]),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("volume"),
                    row.get("turnover") or row.get("amount"),
                )
                for row in rows
            ],
        )

    def test_quality_snapshot_evaluation_summary_and_chart_data(self) -> None:
        seed_date = date(2026, 6, 5)
        eval_date = date(2026, 6, 8)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 2,
                "seed_sources": {"news_theme_daily": 1, "hot_rank": 1},
                "preview": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "source": "news_theme_daily",
                        "reason": "AI服务器催化",
                        "catalyst_tags": ["100亿算力大单"],
                        "catalyst_tier": 1,
                        "trigger_signals": [{"label": "AI服务器"}],
                    },
                    {
                        "code": "600002",
                        "name": "测试二",
                        "source": "hot_rank",
                        "reason": "热榜异动",
                        "catalyst_tier": 2,
                    },
                ],
            },
            "thesis_desk_packets": [
                {
                    "expert": "momentum_desk",
                    "status": "ok",
                    "candidates": [
                        {
                            "code": "600001",
                            "stance": "support",
                            "reason": "BOS 支撑 10.50，突破后承接正常",
                            "risks": ["跌破 BOS 失败"],
                        }
                    ],
                },
                {
                    "expert": "quality_repair_desk",
                    "status": "ok",
                    "rejected": [
                        {
                            "code": "600002",
                            "reason": "一字涨停买不到，追高风险",
                        }
                    ],
                },
            ],
        }
        saved = SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-1",
            trace_id="trace-1",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["item_count"], 2)

        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0, "volume": 1, "amount": 10},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1, "amount": 10.5},
            ],
        )

        self._save_bars(
            "600002",
            [
                {"date": seed_date, "open": 9.9, "high": 10.1, "low": 9.8, "close": 10.0, "volume": 1, "amount": 10},
                {"date": eval_date, "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1, "amount": 11},
            ],
        )
        self._save_bars(
            "000001.SH",
            [
                {"date": seed_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000, "volume": 1, "amount": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030, "volume": 1, "amount": 3030},
            ],
        )

        eval_resp = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-05"})
        self.assertEqual(eval_resp.status_code, 200)
        self.assertEqual(eval_resp.json()["updated"], 2)

        quality_resp = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"})
        self.assertEqual(quality_resp.status_code, 200)
        quality = quality_resp.json()
        self.assertEqual(quality["summary"]["seed_count"], 2)
        self.assertEqual(quality["summary"]["tradable_count"], 1)
        self.assertEqual(quality["summary"]["limit_up_unable_buy_count"], 1)
        self.assertAlmostEqual(quality["summary"]["avg_return_pct"], 5.0)
        self.assertAlmostEqual(quality["summary"]["avg_alpha_return_pct"], 4.0)

        first = next(item for item in quality["items"] if item["code"] == "600001")
        self.assertEqual(first["evaluation"]["benchmark_code"], "000001.SH")
        self.assertAlmostEqual(first["evaluation"]["mfe_pct"], 10.0)
        self.assertAlmostEqual(first["evaluation"]["mae_pct"], -5.0)
        self.assertEqual(first["catalyst_tags"], ["100亿算力大单"])

        second = next(item for item in quality["items"] if item["code"] == "600002")
        self.assertEqual(second["evaluation"]["liquidity_status"], "LIMIT_UP_UNABLE_BUY")

        chart_resp = self.client.get(f"/api/v1/seed-pool-quality/items/{first['id']}/chart-data")
        self.assertEqual(chart_resp.status_code, 200)
        chart = chart_resp.json()
        self.assertGreaterEqual(len(chart["bars"]), 2)
        self.assertEqual(chart["price_lines"], [])
        self.assertEqual(chart["catalyst"]["catalyst_tags"], ["100亿算力大单"])

        dates_resp = self.client.get("/api/v1/seed-pool-quality/dates")
        self.assertEqual(dates_resp.status_code, 200)
        self.assertEqual(dates_resp.json()["dates"][0]["seed_date"], "2026-06-05")

    def test_snapshot_persists_news_signal_seed_link(self) -> None:
        card_id = "card:seed-link:600001"
        NewsSignalRepository(self.db).upsert_cards(
            [
                {
                    "card_id": card_id,
                    "signal_date": date(2026, 7, 10),
                    "summary_short": "订单催化已确认",
                    "status": "active",
                    "evidence_grade": "confirmed",
                    "mapping_status": "mapped",
                    "mapping_confidence": 0.92,
                    "signal_score": 82.0,
                }
            ]
        )
        payload = {
            "status": "ok",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [
                    {
                        "code": "600001",
                        "name": "测试一",
                        "source": "daily_screener",
                        "trigger_signals": [
                            {
                                "dimension": "news_event",
                                "signal_type": "news_signal_card",
                                "value": {
                                    "card_id": card_id,
                                    "gate_result": "matched_existing_seed",
                                    "signal_score": 82.0,
                                    "mapping_confidence": 0.92,
                                    "evidence_grade": "confirmed",
                                },
                            }
                        ],
                    }
                ],
            },
        }

        saved = SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-news-link",
            trace_id="trace-news-link",
            seed_date=date(2026, 7, 10),
            generated_at=datetime(2026, 7, 10, 15, 30),
            market="cn",
        )
        detail = NewsSignalService().get_card(card_id)

        self.assertEqual(saved["seed_link_count"], 1)
        self.assertEqual(len(detail["seed_links"]), 1)
        self.assertEqual(detail["seed_links"][0]["source_desk"], "daily_screener")
        self.assertEqual(detail["seed_links"][0]["gate_result"], "matched_existing_seed")

    def test_snapshot_persists_full_seed_fact_packets_beyond_preview(self):
        seed_date = date(2026, 6, 5)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 2,
                "preview": [
                    {"code": "600001", "name": "测试一", "source": "daily_screener", "hint": "预览内"}
                ],
            },
            "seed_fact_packets": [
                {
                    "code": "600001",
                    "name": "测试一",
                    "market": "cn",
                    "recall_sources": ["daily_screener"],
                    "flags": [{"kind": "pattern", "detector": "screener:ma", "summary": "预览内"}],
                    "fact_sheet": {"freshness": "2026-06-05"},
                },
                {
                    "code": "603072",
                    "name": "天和磁材",
                    "market": "cn",
                    "recall_sources": ["alphasift"],
                    "flags": [{"kind": "pattern", "detector": "alphasift:high_tight_flag", "summary": "放量突破"}],
                    "fact_sheet": {"freshness": "local_phase_a", "trend_state": "bullish"},
                },
            ],
            "thesis_desk_packets": [
                {
                    "expert": "momentum_desk",
                    "status": "ok",
                    "candidates": [{"code": "603072", "name": "天和磁材", "reason": "动量席选中"}],
                }
            ],
            "candidates": [
                {"code": "603072", "name": "天和磁材", "candidate_source": "thesis_desk_committee"}
            ],
        }

        saved = SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-full-seeds",
            trace_id="trace-full-seeds",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )

        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["item_count"], 2)
        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"}).json()
        codes = [item["code"] for item in quality["items"]]
        self.assertEqual(codes, ["600001", "603072"])
        item_603072 = next(item for item in quality["items"] if item["code"] == "603072")
        self.assertEqual(item_603072["source"], "alphasift")
        self.assertEqual(item_603072["desk_outcomes"][0]["desk"], "momentum_desk")

    def test_same_run_id_can_persist_distinct_seed_dates_without_overwrite(self) -> None:
        service = SeedPoolQualityService()
        first = service.persist_candidate_discovery_snapshot(
            candidate_discovery={
                "status": "ok",
                "market": "cn",
                "seed_pool_summary": {
                    "seed_count": 1,
                    "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
                },
            },
            run_id="selection-run",
            trace_id="selection-run",
            seed_date=date(2026, 6, 7),
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        second = service.persist_candidate_discovery_snapshot(
            candidate_discovery={
                "status": "ok",
                "market": "cn",
                "seed_pool_summary": {
                    "seed_count": 1,
                    "preview": [{"code": "600002", "name": "测试二", "source": "daily_screener"}],
                },
            },
            run_id="selection-run",
            trace_id="selection-run",
            seed_date=date(2026, 6, 8),
            generated_at=datetime(2026, 6, 8, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])

        june_7 = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        june_8 = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-08"}).json()
        self.assertEqual(june_7["items"][0]["code"], "600001")
        self.assertEqual(june_8["items"][0]["code"], "600002")

    def test_snapshot_deduplicates_packet_and_final_candidate_desk_outcomes(self) -> None:
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [
                    {
                        "code": "002718",
                        "name": "友邦吊顶",
                        "source": "daily_screener",
                        "freshness": "20260611",
                    }
                ],
            },
            "thesis_desk_packets": [
                {
                    "expert": "momentum_desk",
                    "status": "ok",
                    "candidates": [
                        {
                            "code": "002718",
                            "stance": "watch",
                            "reason": "动量席已看过，等待回踩确认。",
                        }
                    ],
                }
            ],
            "candidates": [
                {
                    "code": "002718",
                    "name": "友邦吊顶",
                    "stance_by_desk": {"momentum_desk": "watch"},
                    "reason": "最终候选聚合理由。",
                }
            ],
        }

        saved = SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="trace-case:2026-06-11",
            trace_id="trace-case",
            generated_at=datetime(2026, 6, 12, 14, 20),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self.assertEqual(saved["status"], "ok")

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-11"}).json()
        self.assertEqual(quality["snapshot"]["trace_id"], "trace-case")
        item = quality["items"][0]
        self.assertEqual(item["code"], "002718")
        momentum = [outcome for outcome in item["desk_outcomes"] if outcome["desk"] == "momentum_desk"]
        self.assertEqual(len(momentum), 1)
        self.assertEqual(momentum[0]["stance"], "watch")
        self.assertEqual(momentum[0]["reason"], "动量席已看过，等待回踩确认。")

    def test_default_seed_date_before_open_belongs_to_previous_day(self) -> None:
        self.assertEqual(
            effective_seed_pool_date(datetime(2026, 6, 10, 8, 59)),
            date(2026, 6, 9),
        )
        self.assertEqual(
            effective_seed_pool_date(datetime(2026, 6, 10, 9, 0)),
            date(2026, 6, 10),
        )

        saved = SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery={
                "status": "ok",
                "market": "cn",
                "seed_pool_summary": {
                    "seed_count": 1,
                    "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
                },
            },
            run_id="preopen-run",
            trace_id="preopen-trace",
            generated_at=datetime(2026, 6, 10, 8, 59),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self.assertEqual(saved["status"], "ok")

        june_9 = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-09"}).json()
        self.assertEqual(june_9["snapshot"]["run_id"], "preopen-run")
        self.assertEqual(june_9["items"][0]["code"], "600001")

    def test_same_seed_date_replaces_old_snapshot_and_evaluates_latest_only(self) -> None:
        service = SeedPoolQualityService()
        seed_date = date(2026, 6, 5)
        eval_date = date(2026, 6, 8)
        first = service.persist_candidate_discovery_snapshot(
            candidate_discovery={
                "status": "ok",
                "market": "cn",
                "seed_pool_summary": {
                    "seed_count": 1,
                    "preview": [{"code": "600001", "name": "旧池", "source": "daily_screener"}],
                },
            },
            run_id="run-old",
            trace_id="trace-old",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 9, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        second = service.persist_candidate_discovery_snapshot(
            candidate_discovery={
                "status": "ok",
                "market": "cn",
                "seed_pool_summary": {
                    "seed_count": 1,
                    "preview": [{"code": "600002", "name": "新池", "source": "daily_screener"}],
                },
            },
            run_id="run-new",
            trace_id="trace-new",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

        dates = self.client.get("/api/v1/seed-pool-quality/dates").json()["dates"]
        self.assertEqual(dates[0]["seed_date"], "2026-06-05")
        self.assertEqual(dates[0]["snapshot_count"], 1)
        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"}).json()
        self.assertEqual(quality["snapshot"]["run_id"], "run-new")
        self.assertEqual([item["code"] for item in quality["items"]], ["600002"])

        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
            ],
        )
        self._save_bars(
            "600002",
            [
                {"date": seed_date, "open": 19.8, "high": 20.2, "low": 19.7, "close": 20.0},
                {"date": eval_date, "open": 20.2, "high": 21.0, "low": 19.5, "close": 21.0},
            ],
        )
        self._save_bars(
            "000001.SH",
            [
                {"date": seed_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )

        evaluated = service.evaluate_seed_date(seed_date)
        self.assertEqual(evaluated["requested"], 1)
        self.assertEqual(evaluated["updated"], 1)
        quality_after_eval = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"}).json()
        self.assertEqual([item["code"] for item in quality_after_eval["items"]], ["600002"])
        self.assertAlmostEqual(quality_after_eval["items"][0]["evaluation"]["next_close_return_pct"], 5.0)

    def test_weekend_snapshot_uses_previous_trading_day_seed_close(self) -> None:
        seed_date = date(2026, 6, 7)
        prior_trade_date = date(2026, 6, 5)
        eval_date = date(2026, 6, 8)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-weekend",
            trace_id="trace-weekend",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600001",
            [
                {"date": prior_trade_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
            ],
        )
        self._save_bars(
            "000001.SH",
            [
                {"date": prior_trade_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )

        eval_resp = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-07"})
        self.assertEqual(eval_resp.status_code, 200)
        self.assertEqual(eval_resp.json()["expected_evaluation_date"], "2026-06-08")
        self.assertEqual(eval_resp.json()["updated"], 1)

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        item = quality["items"][0]
        self.assertEqual(item["evaluation"]["evaluation_date"], "2026-06-08")
        self.assertAlmostEqual(item["evaluation"]["seed_close"], 10.0)
        self.assertAlmostEqual(item["evaluation"]["alpha_return_pct"], 4.0)

    def test_evaluate_skips_exchange_holiday_using_benchmark_trading_day(self) -> None:
        seed_date = date(2026, 6, 18)
        holiday_date = date(2026, 6, 19)
        eval_date = date(2026, 6, 22)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-holiday",
            trace_id="trace-holiday",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 18, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
            ],
        )
        self._save_bars(
            "000001.SH",
            [
                {"date": seed_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )

        eval_resp = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-18"})
        self.assertEqual(eval_resp.status_code, 200)
        self.assertEqual(eval_resp.json()["expected_evaluation_date"], "2026-06-22")
        self.assertEqual(eval_resp.json()["updated"], 1)

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-18"}).json()
        item = quality["items"][0]
        self.assertEqual(item["evaluation"]["evaluation_date"], "2026-06-22")
        self.assertNotEqual(item["evaluation"]["evaluation_date"], holiday_date.isoformat())
        self.assertAlmostEqual(item["evaluation"]["alpha_return_pct"], 4.0)

    def test_evaluate_seed_date_refreshes_existing_ok_evaluations(self) -> None:
        seed_date = date(2026, 6, 5)
        eval_date = date(2026, 6, 8)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-refresh-ok",
            trace_id="trace-refresh-ok",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
            ],
        )
        self._save_bars(
            "000001.SH",
            [
                {"date": seed_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )
        first = SeedPoolQualityService().evaluate_seed_date(seed_date)
        self.assertEqual(first["updated"], 1)

        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 12.0, "low": 9.5, "close": 11.0},
            ],
        )
        second = SeedPoolQualityService().evaluate_seed_date(seed_date)
        self.assertEqual(second["requested"], 1)
        self.assertEqual(second["updated"], 1)
        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"}).json()
        item = quality["items"][0]
        self.assertAlmostEqual(item["evaluation"]["next_close_return_pct"], 10.0)
        self.assertAlmostEqual(item["evaluation"]["alpha_return_pct"], 9.0)

    def test_evaluate_reports_missing_local_database_data_before_writing_missing_price(self) -> None:
        seed_date = date(2026, 6, 7)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-missing",
            trace_id="trace-missing",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )

        missing_benchmark = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-07"})
        self.assertEqual(missing_benchmark.status_code, 409)
        missing_benchmark_error = missing_benchmark.json().get("detail", missing_benchmark.json())
        self.assertEqual(missing_benchmark_error["error"], "missing_benchmark_ohlc")

        self._save_bars(
            "000001.SH",
            [
                {"date": date(2026, 6, 5), "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": date(2026, 6, 8), "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )
        missing_stock = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-07"})
        self.assertEqual(missing_stock.status_code, 409)
        missing_stock_error = missing_stock.json().get("detail", missing_stock.json())
        self.assertEqual(missing_stock_error["error"], "missing_stock_ohlc")

    def test_benchmark_prefers_sequoia_index_and_avoids_stock_code_ambiguity(self) -> None:
        seed_date = date(2026, 6, 5)
        eval_date = date(2026, 6, 8)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-sequoia-index",
            trace_id="trace-sequoia-index",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 5, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600001",
            [
                {"date": seed_date, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": eval_date, "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
            ],
        )
        self._save_bars(
            "000001",
            [
                {"date": seed_date, "open": 10, "high": 10, "low": 10, "close": 10},
                {"date": eval_date, "open": 20, "high": 20, "low": 20, "close": 20},
            ],
        )
        self._save_sequoia_bars(
            "000001.SH",
            [
                {"date": seed_date, "open": 3000, "high": 3020, "low": 2990, "close": 3000},
                {"date": eval_date, "open": 3010, "high": 3040, "low": 3000, "close": 3030},
            ],
        )

        eval_resp = self.client.post("/api/v1/seed-pool-quality/evaluate", params={"seed_date": "2026-06-05"})
        self.assertEqual(eval_resp.status_code, 200)
        self.assertEqual(eval_resp.json()["updated"], 1)

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-05"}).json()
        item = quality["items"][0]
        self.assertAlmostEqual(item["evaluation"]["benchmark_return_pct"], 1.0)
        self.assertAlmostEqual(item["evaluation"]["alpha_return_pct"], 4.0)

    def test_chart_data_falls_back_to_sequoia_stock_daily_for_seed_bars(self) -> None:
        seed_date = date(2026, 6, 7)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "000050", "name": "深天马Ａ", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-sequoia-bars",
            trace_id="trace-sequoia-bars",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_sequoia_bars(
            "000050",
            [
                {"date": date(2026, 6, 5), "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "turnover": 100},
                {"date": date(2026, 6, 8), "open": 10.8, "high": 12.0, "low": 10.2, "close": 11.5, "turnover": 120},
            ],
        )

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        item_id = quality["items"][0]["id"]
        chart_resp = self.client.get(f"/api/v1/seed-pool-quality/items/{item_id}/chart-data")
        self.assertEqual(chart_resp.status_code, 200)
        chart = chart_resp.json()
        self.assertEqual([bar["trade_date"] for bar in chart["bars"]], ["2026-06-05", "2026-06-08"])
        self.assertEqual(chart["bars"][0]["source"], "sequoia_stock_daily:000050")

    def test_chart_data_prefers_sequoia_window_over_partial_main_db(self) -> None:
        seed_date = date(2026, 6, 7)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600403", "name": "大有能源", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-partial-main-bars",
            trace_id="trace-partial-main-bars",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600403",
            [{"date": date(2026, 6, 8), "open": 10.8, "high": 12.0, "low": 10.2, "close": 11.5}],
        )
        self._save_sequoia_bars(
            "600403",
            [
                {"date": date(2026, 6, 5), "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "turnover": 100},
                {"date": date(2026, 6, 8), "open": 10.7, "high": 11.8, "low": 10.1, "close": 11.3, "turnover": 120},
            ],
        )

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        item_id = quality["items"][0]["id"]
        chart_resp = self.client.get(f"/api/v1/seed-pool-quality/items/{item_id}/chart-data")
        self.assertEqual(chart_resp.status_code, 200)
        chart = chart_resp.json()
        self.assertEqual([bar["trade_date"] for bar in chart["bars"]], ["2026-06-05", "2026-06-08"])
        self.assertEqual(chart["bars"][0]["source"], "sequoia_stock_daily:600403")
        self.assertEqual(chart["bars"][1]["source"], "sequoia_stock_daily:600403")
        self.assertAlmostEqual(chart["bars"][1]["close"], 11.3)

    def test_chart_data_anchors_weekend_seed_to_previous_trading_day(self) -> None:
        seed_date = date(2026, 6, 7)
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "600001", "name": "测试一", "source": "daily_screener"}],
            },
        }
        SeedPoolQualityService().persist_candidate_discovery_snapshot(
            candidate_discovery=payload,
            run_id="run-weekend-chart",
            trace_id="trace-weekend-chart",
            seed_date=seed_date,
            generated_at=datetime(2026, 6, 7, 18, 0),
            market="cn",
            candidate_discovery_mode="thesis_desk_committee",
        )
        self._save_bars(
            "600001",
            [
                {"date": date(2026, 5, 30), "open": 8.0, "high": 8.5, "low": 7.8, "close": 8.2},
                {"date": date(2026, 6, 1), "open": 8.2, "high": 8.8, "low": 8.1, "close": 8.6},
                {"date": date(2026, 6, 5), "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0},
                {"date": date(2026, 6, 8), "open": 10.2, "high": 11.0, "low": 9.5, "close": 10.5},
                {"date": date(2026, 6, 9), "open": 10.4, "high": 10.8, "low": 10.1, "close": 10.6},
            ],
        )

        quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        item_id = quality["items"][0]["id"]
        chart_resp = self.client.get(f"/api/v1/seed-pool-quality/items/{item_id}/chart-data")
        self.assertEqual(chart_resp.status_code, 200)
        chart = chart_resp.json()
        self.assertEqual([bar["trade_date"] for bar in chart["bars"]], ["2026-05-30", "2026-06-01", "2026-06-05", "2026-06-08", "2026-06-09"])

    def test_quality_api_resolves_placeholder_stock_names_from_index(self) -> None:
        index_path = self.data_dir / "stocks.index.json"
        index_path.write_text(
            '[["000050.SZ","000050","深天马Ａ","shentianmaA","stmA",[],"CN","stock",true,100]]',
            encoding="utf-8",
        )
        payload = {
            "status": "ok",
            "market": "cn",
            "seed_pool_summary": {
                "seed_count": 1,
                "preview": [{"code": "000050", "name": "000050", "source": "daily_screener"}],
            },
        }
        with patch.object(stock_index_loader, "get_stock_index_candidate_paths", return_value=(index_path,)):
            stock_index_loader._clear_stock_index_cache_for_tests()
            SeedPoolQualityService().persist_candidate_discovery_snapshot(
                candidate_discovery=payload,
                run_id="run-name",
                trace_id="trace-name",
                seed_date=date(2026, 6, 7),
                generated_at=datetime(2026, 6, 7, 18, 0),
                market="cn",
                candidate_discovery_mode="thesis_desk_committee",
            )
            quality = self.client.get("/api/v1/seed-pool-quality", params={"seed_date": "2026-06-07"}).json()
        self.assertEqual(quality["items"][0]["code"], "000050")
        self.assertEqual(quality["items"][0]["name"], "深天马Ａ")


if __name__ == "__main__":
    unittest.main()
