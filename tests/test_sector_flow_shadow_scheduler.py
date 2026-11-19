# -*- coding: utf-8 -*-
"""每日板块资金流影子采集（FAC-01d）测试。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.candidate_experts_v2 import sector_flow
from src.services import sector_flow_shadow_scheduler as shadow

DAYS = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def _row(day, amount):
    return {
        "date": day,
        "main_amount": amount,
        "main_amount_pct": 2.0,
        "super_big_amount": 1.0,
        "big_amount": 1.0,
    }


class _FakeSource:
    def __init__(self, catalog, histories, fail_boards=None, fail_catalog=False):
        self._catalog = catalog
        self._histories = histories
        self.fail_boards = set(fail_boards or [])
        self.fail_catalog = fail_catalog
        self.history_calls = 0

    def board_catalog(self):
        if self.fail_catalog:
            raise RuntimeError("catalog boom")
        return [dict(item) for item in self._catalog]

    def flow_rank(self, indicator):
        if indicator != "5日":
            return {}
        return {name: {"return_pct": 1.0} for name in self._histories}

    def board_history(self, board_name, limit):
        self.history_calls += 1
        if board_name in self.fail_boards:
            raise TimeoutError("provider timeout")
        return list(self._histories[board_name])[-limit:]


def _catalog(*items):
    return [
        {"bk_code": code, "bk_name": name, "member_count": members}
        for code, name, members in items
    ]


def _histories_map():
    return {
        name: [_row(day, 10.0) for day in DAYS]
        for name in ("板块一", "板块二")
    }


def _config(tmp_path, enabled=True):
    return SimpleNamespace(
        agent_sector_flow_observation_enabled=enabled,
        agent_sector_flow_cache_dir=str(tmp_path),
    )


def _read_records(tmp_path, day="2026-11-13"):
    path = Path(tmp_path) / shadow.RUN_RECORD_SUBDIR / f"{day}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_in_shadow_window_uses_shanghai_timezone_and_weekdays():
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 15, 30)) is True
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 16, 0)) is True
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 15, 29)) is False
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 16, 1)) is False
    # UTC 07:30 == 上海 15:30
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 7, 30, tzinfo=timezone.utc)) is True
    assert shadow.in_shadow_window(datetime(2026, 11, 13, 7, 29, tzinfo=timezone.utc)) is False
    # 周六
    assert shadow.in_shadow_window(datetime(2026, 11, 14, 15, 35)) is False


def test_outside_window_skips_without_fetching(tmp_path):
    source = _FakeSource(_catalog(("BK0001", "板块一", 10)), _histories_map())
    record = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path),
        now=datetime(2026, 11, 13, 15, 0),
        source=source,
    )
    assert record["status"] == "skipped"
    assert record["reason"] == "outside_window"
    assert source.history_calls == 0
    assert not (Path(tmp_path) / shadow.RUN_RECORD_SUBDIR).exists()


def test_window_build_writes_snapshot_and_run_record(tmp_path):
    source = _FakeSource(_catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10)), _histories_map())
    record = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path),
        now=datetime(2026, 11, 13, 15, 35),
        source=source,
    )
    assert record["outcome"] == "complete"
    assert record["cache_hit"] is False
    assert record["boards_ok"] == 2
    assert record["failed_count"] == 0
    assert record["percentile_universe"] == "complete"
    assert record["universe_coverage"] == 1.0
    assert record["duration_s"] >= 0
    snapshot_path = Path(tmp_path) / "sector_flow_snapshot_2026-11-13.json"
    assert snapshot_path.exists()
    stored = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert stored["percentile_universe"] == "complete"
    assert _read_records(tmp_path)[-1]["outcome"] == "complete"


def test_repeat_trigger_hits_cache_and_does_not_refetch(tmp_path):
    source = _FakeSource(_catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10)), _histories_map())
    shadow.run_shadow_snapshot_once(
        config=_config(tmp_path), now=datetime(2026, 11, 13, 15, 32), source=source
    )
    calls_before = source.history_calls
    record = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path), now=datetime(2026, 11, 13, 15, 45), source=source
    )
    assert source.history_calls == calls_before
    assert record["cache_hit"] is True
    assert record["outcome"] == "complete"
    assert len(_read_records(tmp_path)) == 2


def test_failure_is_recorded_and_never_raises(tmp_path):
    source = _FakeSource(_catalog(("BK0001", "板块一", 10)), _histories_map(), fail_catalog=True)
    record = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path), now=datetime(2026, 11, 13, 15, 35), source=source
    )
    assert record["outcome"] == "error"
    assert "catalog boom" in record["error"]
    assert record["duration_s"] >= 0
    assert _read_records(tmp_path)[-1]["outcome"] == "error"


def test_failed_boards_are_retried_one_by_one(tmp_path):
    source = _FakeSource(
        _catalog(("BK0001", "板块一", 10), ("BK0002", "板块二", 10)),
        _histories_map(),
        fail_boards={"板块二"},
    )
    first = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path), now=datetime(2026, 11, 13, 15, 31), source=source
    )
    assert first["outcome"] == "partial"
    assert first["failed_count"] == 1
    assert first["failed_boards"][0]["bk_code"] == "BK0002"

    # 模拟冷却已过，下一次触发只重试失败板块
    snapshot_path = Path(tmp_path) / "sector_flow_snapshot_2026-11-13.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["last_error_retry_at"] = (datetime.now() - timedelta(minutes=30)).replace(microsecond=0).isoformat()
    sector_flow._write_json_atomic(snapshot_path, snapshot)
    calls_before = source.history_calls
    source.fail_boards.clear()

    second = shadow.run_shadow_snapshot_once(
        config=_config(tmp_path), now=datetime(2026, 11, 13, 15, 41), source=source
    )
    assert source.history_calls - calls_before == 1
    assert second["failed_count"] == 0
    assert second["outcome"] == "complete"


def test_background_task_registration_respects_switch(tmp_path):
    assert shadow.build_sector_flow_shadow_background_tasks(_config(tmp_path, enabled=False)) == []
    tasks = shadow.build_sector_flow_shadow_background_tasks(_config(tmp_path, enabled=True))
    assert len(tasks) == 1
    assert tasks[0]["name"] == "sector_flow_shadow"
    assert tasks[0]["interval_seconds"] == shadow.SHADOW_INTERVAL_SECONDS
    assert tasks[0]["run_immediately"] is True
    assert callable(tasks[0]["task"])
