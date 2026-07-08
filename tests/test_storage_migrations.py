# -*- coding: utf-8 -*-
"""Storage schema migration regression tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from src.storage import DatabaseManager


class StorageMigrationTestCase(unittest.TestCase):
    def test_news_extracted_event_migration_creates_table_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migration.db"
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                manager = object.__new__(DatabaseManager)
                manager._engine = engine
                manager._migrate_news_extracted_events()
                manager._migrate_news_extracted_events()
                with engine.begin() as conn:
                    table = conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='news_extracted_events'"
                    ).fetchone()
                    columns = {
                        str(row[1])
                        for row in conn.exec_driver_sql("PRAGMA table_info('news_extracted_events')").fetchall()
                    }

                self.assertIsNotNone(table)
                self.assertIn("event_id", columns)
                self.assertIn("raw_episode_id", columns)
                self.assertIn("verification_status", columns)
                self.assertIn("entity_links_json", columns)
            finally:
                engine.dispose()

    def test_news_signal_edge_quality_migration_adds_columns_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migration.db"
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE TABLE news_signal_edges (id INTEGER PRIMARY KEY, edge_id VARCHAR(128), source_card_id VARCHAR(96), target_type VARCHAR(32), target_id VARCHAR(128), edge_type VARCHAR(64))"
                    )
                    manager = object.__new__(DatabaseManager)
                    manager._engine = engine
                    manager._migrate_news_signal_edge_quality()
                    manager._migrate_news_signal_edge_quality()
                    columns = {
                        str(row[1])
                        for row in conn.exec_driver_sql("PRAGMA table_info('news_signal_edges')").fetchall()
                    }

                self.assertIn("edge_quality", columns)
                self.assertIn("quality_grade", columns)
                self.assertIn("quality_flags_json", columns)
            finally:
                engine.dispose()

    def test_raw_news_quality_migration_adds_columns_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migration.db"
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE TABLE raw_news_episodes (id INTEGER PRIMARY KEY, episode_id VARCHAR(80), dedup_key VARCHAR(128), source VARCHAR(80), title VARCHAR(300), signal_date DATE)"
                    )
                    manager = object.__new__(DatabaseManager)
                    manager._engine = engine
                    manager._migrate_raw_news_episode_quality()
                    manager._migrate_raw_news_episode_quality()
                    columns = {
                        str(row[1])
                        for row in conn.exec_driver_sql("PRAGMA table_info('raw_news_episodes')").fetchall()
                    }

                self.assertIn("normalized_content", columns)
                self.assertIn("quality_score", columns)
                self.assertIn("quality_grade", columns)
                self.assertIn("quality_flags_json", columns)
            finally:
                engine.dispose()

    def test_add_sqlite_column_tolerates_duplicate_column_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migration.db"
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE TABLE news_signal_cards (id INTEGER PRIMARY KEY, signal_layer VARCHAR(24))"
                    )

                    with patch.object(DatabaseManager, "_sqlite_column_exists", side_effect=[False, True]):
                        DatabaseManager._add_sqlite_column_if_missing(
                            conn,
                            "news_signal_cards",
                            "signal_layer",
                            (
                                "ALTER TABLE news_signal_cards "
                                "ADD COLUMN signal_layer VARCHAR(24) DEFAULT 'industry'"
                            ),
                        )

                    columns = [
                        str(row[1])
                        for row in conn.exec_driver_sql("PRAGMA table_info('news_signal_cards')").fetchall()
                    ]
                self.assertEqual(columns.count("signal_layer"), 1)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
