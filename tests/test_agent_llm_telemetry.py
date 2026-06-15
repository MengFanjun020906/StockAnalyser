# -*- coding: utf-8 -*-
"""Tests for Agent LLM telemetry JSONL artifacts."""

from __future__ import annotations

import json
from pathlib import Path


def test_record_llm_telemetry_writes_jsonl(tmp_path: Path):
    from src.agent.llm_telemetry import llm_telemetry_scope, record_llm_telemetry

    with llm_telemetry_scope(
        trace_id="trace-unit",
        artifact_dir=str(tmp_path),
        stage="judge_decision",
        agent_role="judge",
        symbol="600519",
    ):
        record_llm_telemetry(
            model="unit/model",
            provider="unit",
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            latency_ms=12.34,
            tool_calls=2,
            ok=True,
        )

    rows = [json.loads(line) for line in (tmp_path / "llm_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["trace_id"] == "trace-unit"
    assert row["stage"] == "judge_decision"
    assert row["agent_role"] == "judge"
    assert row["symbol"] == "600519"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 7
    assert row["total_tokens"] == 18
    assert row["tool_calls"] == 2
    assert row["ok"] is True


def test_record_llm_telemetry_without_artifact_dir_is_noop(tmp_path: Path):
    from src.agent.llm_telemetry import llm_telemetry_scope, record_llm_telemetry

    with llm_telemetry_scope(trace_id="trace-unit", stage="judge_decision"):
        record_llm_telemetry(
            model="unit/model",
            provider="unit",
            usage={"total_tokens": 18},
            latency_ms=1,
            ok=False,
            error="boom",
        )

    assert not (tmp_path / "llm_usage.jsonl").exists()
