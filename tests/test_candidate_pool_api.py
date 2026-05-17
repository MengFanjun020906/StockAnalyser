import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from api.app import create_app
from src.agent.candidate_pool_store import CandidatePoolStore
from src.config import Config
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class CandidatePoolApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "candidate_pool_api.db"
        self.env_path = self.data_dir / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                    f"AGENT_CANDIDATE_POOL_DB_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["AGENT_CANDIDATE_POOL_DB_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.client = TestClient(create_app(static_dir=self.data_dir / "empty-static"))

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("AGENT_CANDIDATE_POOL_DB_PATH", None)
        self.temp_dir.cleanup()

    def test_candidate_pool_latest_and_runs(self) -> None:
        CandidatePoolStore(str(self.db_path)).save_run(
            {
                "status": "ok",
                "market": "cn",
                "candidate_source": "unit",
                "quality": {"hard_strategy_trunk_missing": False},
                "hard_exclusion": {"excluded_count": 0},
                "candidates": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "source": "alphasift:quality_value",
                        "signal_score": 80,
                        "candidate_dimensions": ["strategy"],
                        "reason": "策略候选",
                    }
                ],
            },
            run_id="api-run-1",
        )

        latest = self.client.get("/api/v1/candidate-pool/latest")
        self.assertEqual(latest.status_code, 200)
        payload = latest.json()
        self.assertEqual(payload["run"]["run_id"], "api-run-1")
        self.assertEqual(payload["items"][0]["code"], "600519")
        self.assertEqual(payload["summary"]["candidate_count"], 1)

        runs = self.client.get("/api/v1/candidate-pool/runs", params={"limit": 5})
        self.assertEqual(runs.status_code, 200)
        self.assertEqual(runs.json()["runs"][0]["run_id"], "api-run-1")

        detail = self.client.get("/api/v1/candidate-pool/runs/api-run-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["items"][0]["name"], "贵州茅台")

    def test_candidate_pool_latest_empty(self) -> None:
        latest = self.client.get("/api/v1/candidate-pool/latest")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["items"], [])
