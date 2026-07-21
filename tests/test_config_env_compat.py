# -*- coding: utf-8 -*-
"""Tests for backward-compatible config env aliases and TickFlow loading."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import Config, setup_env


class ConfigEnvCompatibilityTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_reads_tickflow_api_key(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
                "TICKFLOW_API_KEY": "tf-secret",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.tickflow_api_key, "tf-secret")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_keeps_default_behavior_without_tickflow_api_key(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertIsNone(config.tickflow_api_key)
        self.assertEqual(
            config.realtime_source_priority,
            "tencent,akshare_sina,efinance,akshare_em",
        )

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_reads_graphiti_config(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
                "GRAPHITI_ENABLED": "true",
                "NEO4J_URI": "bolt://neo4j.example:7687",
                "NEO4J_USER": "neo4j_user",
                "NEO4J_PASSWORD": "neo4j_secret",
                "GRAPHITI_LLM_MODEL": "openai/gpt-4o-mini",
                "GRAPHITI_EMBEDDING_MODEL": "openai/text-embedding-3-small",
                "GRAPHITI_EMBEDDING_BASE_URL": "https://embed.example.com/v1",
                "GRAPHITI_EMBEDDING_API_KEY": "embed-secret",
                "GRAPHITI_GROUP_STRATEGY": "single",
                "GRAPHITI_OUTBOX_WORKER_ENABLED": "true",
                "GRAPHITI_OUTBOX_INTERVAL_SECONDS": "90",
                "GRAPHITI_OUTBOX_BATCH_SIZE": "15",
                "GRAPHITI_OUTBOX_MAX_ATTEMPTS": "6",
                "GRAPHITI_OUTBOX_RETRY_BASE_SECONDS": "45",
                "GRAPHITI_OUTBOX_JOB_TIMEOUT_SECONDS": "180",
                "GRAPHITI_SELECTION_SEARCH_TIMEOUT_SECONDS": "9",
                "NEWS_SIGNAL_CLS_INCREMENTAL_ENABLED": "true",
                "NEWS_SIGNAL_CLS_INCREMENTAL_INTERVAL_MINUTES": "7",
                "NEWS_SIGNAL_CLS_INCREMENTAL_LIMIT": "40",
                "NEWS_EVENT_SENTINEL_ENABLED": "true",
                "NEWS_EVENT_SENTINEL_INTERVAL_MINUTES": "45",
                "NEWS_EVENT_SENTINEL_ACTIVE_WINDOWS": "08:00-02:30",
                "NEWS_EVENT_SENTINEL_MAX_ITEMS_PER_SOURCE": "25",
                "NEWS_EVENT_SENTINEL_CARD_MAX_AGE_MINUTES": "35",
                "NEWS_EVENT_SENTINEL_MIN_SEVERITY": "high",
                "NEWS_EVENT_SENTINEL_COOLDOWN_MINUTES": "90",
                "NEWS_EVENT_SENTINEL_TRIGGER_MODE": "notify_only",
                "NEWS_EVENT_SENTINEL_TRACE_MAX_PER_RUN": "3",
                "NEWS_EVENT_SENTINEL_TRACE_MAX_PER_DAY": "12",
                "NEWS_EVENT_SENTINEL_RUN_IMMEDIATELY": "true",
                "NEWS_EVENT_SENTINEL_FEISHU_ENABLED": "true",
                "NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED": "true",
                "NEWS_EVENT_SENTINEL_HEARTBEAT_INTERVAL_MINUTES": "15",
                "NEWS_SIGNAL_EMBEDDING_THRESHOLDS_JSON": '{"default":0.8,"custom":0.73}',
            },
            clear=True,
        ):
            with patch.object(Config, "_parse_stock_email_groups", return_value=[]):
                config = Config._load_from_env()

        self.assertTrue(config.graphiti_enabled)
        self.assertEqual(config.graphiti_neo4j_uri, "bolt://neo4j.example:7687")
        self.assertEqual(config.graphiti_neo4j_user, "neo4j_user")
        self.assertEqual(config.graphiti_neo4j_password, "neo4j_secret")
        self.assertEqual(config.graphiti_llm_model, "openai/gpt-4o-mini")
        self.assertEqual(config.graphiti_embedding_model, "openai/text-embedding-3-small")
        self.assertEqual(config.graphiti_embedding_base_url, "https://embed.example.com/v1")
        self.assertEqual(config.graphiti_embedding_api_key, "embed-secret")
        self.assertEqual(config.graphiti_group_strategy, "single")
        self.assertTrue(config.graphiti_outbox_worker_enabled)
        self.assertEqual(config.graphiti_outbox_interval_seconds, 90)
        self.assertEqual(config.graphiti_outbox_batch_size, 15)
        self.assertEqual(config.graphiti_outbox_max_attempts, 6)
        self.assertEqual(config.graphiti_outbox_retry_base_seconds, 45)
        self.assertEqual(config.graphiti_outbox_job_timeout_seconds, 180)
        self.assertEqual(config.graphiti_selection_search_timeout_seconds, 9.0)
        self.assertTrue(config.news_signal_cls_incremental_enabled)
        self.assertEqual(config.news_signal_cls_incremental_interval_minutes, 7)
        self.assertEqual(config.news_signal_cls_incremental_limit, 40)
        self.assertTrue(config.news_event_sentinel_enabled)
        self.assertEqual(config.news_event_sentinel_interval_minutes, 45)
        self.assertEqual(config.news_event_sentinel_active_windows, "08:00-02:30")
        self.assertEqual(config.news_event_sentinel_max_items_per_source, 25)
        self.assertEqual(config.news_event_sentinel_card_max_age_minutes, 35)
        self.assertEqual(config.news_event_sentinel_min_severity, "high")
        self.assertEqual(config.news_event_sentinel_cooldown_minutes, 90)
        self.assertEqual(config.news_event_sentinel_trigger_mode, "notify_only")
        self.assertEqual(config.news_event_sentinel_trace_max_per_run, 3)
        self.assertEqual(config.news_event_sentinel_trace_max_per_day, 12)
        self.assertTrue(config.news_event_sentinel_run_immediately)
        self.assertTrue(config.news_event_sentinel_feishu_enabled)
        self.assertTrue(config.news_event_sentinel_heartbeat_enabled)
        self.assertEqual(config.news_event_sentinel_heartbeat_interval_minutes, 15)
        self.assertIn('"custom":0.73', config.news_signal_embedding_thresholds_json)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_falls_back_to_legacy_run_immediately(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_prefers_schedule_specific_setting(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
            "SCHEDULE_RUN_IMMEDIATELY": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_legacy_run_immediately_stays_false_when_schedule_alias_is_unset(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_schedule_run_immediately_stays_false_without_falling_back(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "true",
            "SCHEDULE_RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertTrue(config.run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_blank_schedule_time_falls_back_to_default(
        self,
        _mock_parse_yaml,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "SCHEDULE_TIME=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.schedule_time, "18:00")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_report_language_prefers_preexisting_process_env_over_env_file(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("REPORT_LANGUAGE=zh\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "REPORT_LANGUAGE": "en",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.report_language, "en")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_report_language_uses_env_file_when_process_env_is_absent(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("REPORT_LANGUAGE=en\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.report_language, "en")

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_reload_from_updated_env_file_after_runtime_refresh(
        self,
        _mock_parse_yaml,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "SCHEDULE_ENABLED=false",
                        "SCHEDULE_TIME=18:00",
                        "RUN_IMMEDIATELY=true",
                        "SCHEDULE_RUN_IMMEDIATELY=false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519",
                    "SCHEDULE_ENABLED": "false",
                    "SCHEDULE_TIME": "18:00",
                    "RUN_IMMEDIATELY": "true",
                    "SCHEDULE_RUN_IMMEDIATELY": "false",
                },
                clear=True,
            ):
                Config._load_from_env()
                env_path.write_text(
                    "\n".join(
                        [
                            "STOCK_LIST=300750,TSLA",
                            "SCHEDULE_ENABLED=true",
                            "SCHEDULE_TIME=09:30",
                            "RUN_IMMEDIATELY=false",
                            "SCHEDULE_RUN_IMMEDIATELY=true",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                Config.reset_instance()
                setup_env(override=True)
                config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["300750", "TSLA"])
        self.assertTrue(config.schedule_enabled)
        self.assertEqual(config.schedule_time, "09:30")
        self.assertFalse(config.run_immediately)
        self.assertTrue(config.schedule_run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_prefer_process_env_when_values_differ(
        self,
        _mock_parse_yaml,
    ) -> None:
        """When process env explicitly sets a WEBUI-mutable key to a value
        that differs from .env (e.g. via docker-compose ``environment:``),
        the process env must win because ``_capture_bootstrap_runtime_env_overrides``
        runs before dotenv loads and the mismatch proves an intentional override.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=300750,TSLA",
                        "SCHEDULE_ENABLED=true",
                        "SCHEDULE_TIME=09:30",
                        "RUN_IMMEDIATELY=false",
                        "SCHEDULE_RUN_IMMEDIATELY=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519,000001",
                    "SCHEDULE_ENABLED": "false",
                    "SCHEDULE_TIME": "18:00",
                    "RUN_IMMEDIATELY": "true",
                    "SCHEDULE_RUN_IMMEDIATELY": "false",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        # Explicit process env overrides win when values differ from .env
        self.assertEqual(config.stock_list, ["600519", "000001"])
        self.assertFalse(config.schedule_enabled)
        self.assertEqual(config.schedule_time, "18:00")
        self.assertTrue(config.run_immediately)
        self.assertFalse(config.schedule_run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_use_process_env_when_absent_from_file(
        self,
        _mock_parse_yaml,
    ) -> None:
        """When a WEBUI-mutable key exists only in process env (not in .env),
        it IS a genuine explicit override and must be honoured.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            # .env has no STOCK_LIST or SCHEDULE_* keys at all
            env_path.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519,000001",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519", "000001"])

    def test_parse_report_language_accepts_known_alias_without_warning(self) -> None:
        with self.assertNoLogs("src.config", level="WARNING"):
            parsed = Config._parse_report_language("zh-cn")

        self.assertEqual(parsed, "zh")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_invalid_numeric_env_values_fall_back_to_defaults(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "AGENT_ORCHESTRATOR_TIMEOUT_S": "oops",
            "NEWS_MAX_AGE_DAYS": "bad",
            "MAX_WORKERS": "",
            "WEBUI_PORT": "invalid",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(config.agent_orchestrator_timeout_s, 600)
        self.assertEqual(config.news_max_age_days, 3)
        self.assertEqual(config.max_workers, 3)
        self.assertEqual(config.webui_port, 8000)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_email_groups_support_case_insensitive_env_names(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "STOCK_LIST": "600519,300750",
            "Stock_Group_1": "600519",
            "Email_Group_1": "user1@example.com",
            "stock_group_2": "300750",
            "email_group_2": "user2@example.com",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(
            config.stock_email_groups,
            [
                (["600519"], ["user1@example.com"]),
                (["300750"], ["user2@example.com"]),
            ],
        )

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_email_groups_normalize_codes_at_parse_time(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        """STOCK_GROUP codes are canonicalized at parse time so that
        runtime email routing matches the same equivalence used in
        validate_structured()."""
        env = {
            "STOCK_LIST": "600519,HK00700",
            "STOCK_GROUP_1": "SH600519,1810.HK",
            "EMAIL_GROUP_1": "user@example.com",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        stocks, emails = config.stock_email_groups[0]
        self.assertEqual(stocks, ["600519", "HK01810"])
        self.assertEqual(emails, ["user@example.com"])


if __name__ == "__main__":
    unittest.main()
