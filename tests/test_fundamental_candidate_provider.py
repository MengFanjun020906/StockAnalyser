import sqlite3

from src.agent.candidate_providers.fundamental_provider import (
    FundamentalCandidateProvider,
    ensure_fundamental_candidate_schema,
    upsert_fundamental_snapshots,
)


def test_fundamental_candidate_provider_discovers_quality_candidates(tmp_path):
    db_path = tmp_path / "fundamental.db"
    ensure_fundamental_candidate_schema(str(db_path))
    written = upsert_fundamental_snapshots(
        [
            {
                "code": "600001",
                "name": "测试优质",
                "report_period": "20251231",
                "roe": 18.5,
                "gross_margin": 42,
                "net_margin": 16,
                "revenue_growth": 22,
                "profit_growth": 35,
                "operating_cashflow_ratio": 88,
                "debt_ratio": 38,
                "pe_ttm": 22,
                "pb": 2.6,
                "source": "unit",
            },
            {
                "code": "600002",
                "name": "测试较弱",
                "report_period": "20251231",
                "roe": 2,
                "gross_margin": 8,
                "net_margin": 1,
                "revenue_growth": -20,
                "profit_growth": -35,
                "operating_cashflow_ratio": -30,
                "debt_ratio": 82,
                "pe_ttm": 80,
                "pb": 8,
                "source": "unit",
            },
        ],
        str(db_path),
    )

    result = FundamentalCandidateProvider(str(db_path)).discover(limit=5)

    assert written == 2
    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["code"] == "600001"
    assert candidate["source"] == "fundamental:quality_snapshot"
    assert candidate["candidate_dimension"] if "candidate_dimension" in candidate else True
    assert candidate["metrics"]["roe"] == 18.5
    assert any(item["dimension"] == "fundamental" for item in candidate["reason_dimensions"])


def test_fundamental_candidate_provider_reports_missing_table(tmp_path):
    db_path = tmp_path / "empty.db"
    with sqlite3.connect(db_path):
        pass

    result = FundamentalCandidateProvider(str(db_path)).discover(limit=3)

    assert result["status"] == "unavailable"
    assert "fundamental_candidate_snapshot table not found" in result["error"]
