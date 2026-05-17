from src.agent.candidate_pool_store import CandidatePoolStore, ensure_candidate_pool_schema


def test_candidate_pool_store_saves_and_reads_latest(tmp_path):
    db_path = tmp_path / "candidate_pool.db"
    ensure_candidate_pool_schema(str(db_path))
    store = CandidatePoolStore(str(db_path))

    payload = {
        "status": "ok",
        "market": "cn",
        "candidate_source": "expert_graph_discovery",
        "candidate_count": 2,
        "fallback_used": False,
        "quality": {"hard_strategy_trunk_missing": False, "multi_source_count": 1},
        "hard_exclusion": {"excluded_count": 1, "reason_counts": {"name_code_mismatch": 1}},
        "discovery_steps": [{"source": "alphasift", "status": "ok", "count": 1}],
        "expert_packets": [{"expert_id": "technical_strategy_expert", "status": "ok"}],
        "capacity": {"max_candidates_to_deep_dive": 8},
        "note": "unit-test",
        "candidates": [
            {
                "code": "600519",
                "name": "贵州茅台",
                "source": "multi_recall",
                "signal_score": 88,
                "recall_sources": ["alphasift:quality_value", "sequoia:rps_breakout"],
                "candidate_dimensions": ["strategy", "technical"],
                "reason_dimensions": [{"dimension": "strategy", "label": "策略", "detail": "AlphaSift YAML 多因子策略入池"}],
                "metrics": {"rps": 92},
            },
            {
                "code": "300750",
                "name": "宁德时代",
                "source": "fundamental:quality_growth",
                "signal_score": 72,
                "candidate_dimensions": ["fundamental"],
                "reason": "基本面质量成长候选",
            },
        ],
    }

    saved = store.save_run(payload, run_id="run-1")
    assert saved["saved_count"] == 2

    latest = store.get_latest()
    assert latest is not None
    assert latest["run"]["run_id"] == "run-1"
    assert latest["summary"]["candidate_count"] == 2
    assert latest["summary"]["multi_source_count"] == 1
    assert latest["summary"]["dimension_counts"]["technical"] == 1
    assert latest["items"][0]["code"] == "600519"
    assert latest["items"][0]["reason_dimensions"][0]["label"] == "策略"

    store.save_run({**payload, "candidates": payload["candidates"][:1]}, run_id="run-2")
    second = store.get_run("run-2")
    assert second is not None
    assert second["items"][0]["recurrence_count"] == 2
    assert second["items"][0]["lifecycle_status"] == "active"


def test_candidate_pool_store_empty_latest(tmp_path):
    store = CandidatePoolStore(str(tmp_path / "empty.db"))
    assert store.get_latest() is None
    assert store.list_runs() == []
