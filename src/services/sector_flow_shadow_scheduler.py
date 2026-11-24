# -*- coding: utf-8 -*-
"""每日板块资金流影子采集（FAC-01d）。

只做一件事：在 A 股收盘后的固定窗口内构建/补全当日板块资金流快照并落盘。
不发通知、不写正式报告、不参与任何选股决策。

接入方式：复用 ``main.py --schedule`` 已有的 ``background_tasks`` 机制，
由 ``src/scheduler.py`` 的轮询循环每 10 分钟触发一次；窗口外直接跳过，
窗口内重复触发命中快照缓存（``build_or_load_snapshot`` 的当日缓存），
当日快照已 complete 后不再重复采集，失败板块由 ``retry_failed=True``
单独重试，不影响主分析流程。
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.agent.candidate_experts_v2 import sector_flow

logger = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SHADOW_WINDOW_START = dt_time(15, 30)
# 窗口终点放宽到 18:00：上游偶发连接重置时，10 分钟一次的检查可提供约 16 次
# 独立尝试，避免 15:30–16:00 内三次全失败就当天零样本。
SHADOW_WINDOW_END = dt_time(18, 0)
SHADOW_INTERVAL_SECONDS = 600
MAX_REPORTED_FAILURES = 50
RUN_RECORD_SUBDIR = "sector_flow_shadow_runs"
WINDOW_LABEL = f"{SHADOW_WINDOW_START.strftime('%H:%M')}-{SHADOW_WINDOW_END.strftime('%H:%M')}"


def _to_shanghai(moment: Optional[datetime] = None) -> datetime:
    """统一到 Asia/Shanghai；naive 时间按上海时区解释。"""
    if moment is None:
        return datetime.now(SHANGHAI_TZ)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SHANGHAI_TZ)
    return moment.astimezone(SHANGHAI_TZ)


def in_shadow_window(moment: Optional[datetime] = None) -> bool:
    """是否落在工作日 15:30–18:00（Asia/Shanghai）。

    只按周一至周五判断，法定节假日不单独识别；节假日触发会构建一次
    无新增交易日的快照，不会报错。
    """
    current = _to_shanghai(moment)
    if current.weekday() >= 5:
        return False
    clock = current.replace(tzinfo=None).time()
    return SHADOW_WINDOW_START <= clock <= SHADOW_WINDOW_END


def _snapshot_complete_for(cache: Path, day: str) -> bool:
    """当天快照是否已完整；成功后不再重复采集。

    截断快照（``universe_size < catalog_size``）不算完成，仍需走一次全量重建。
    """
    snapshot = sector_flow._read_json(
        cache / sector_flow._SNAPSHOT_TMPL.format(date=day)
    ) or {}
    catalog_size = int(snapshot.get("catalog_size") or 0)
    universe_size = int(snapshot.get("universe_size") or 0)
    return (
        str(snapshot.get("as_of_date") or "") == day
        and str(snapshot.get("percentile_universe") or "") == "complete"
        and catalog_size > 0
        and universe_size == catalog_size
    )


def _cache_root(config: Any, settings: Dict[str, Any]) -> Path:
    override = str(getattr(config, "agent_sector_flow_cache_dir", "") or "").strip()
    return sector_flow._resolve_cache_dir(override or None, settings)


def _append_run_record(cache: Path, record: Dict[str, Any]) -> None:
    """按日期追加 JSONL 运行记录，失败不影响采集结果。"""
    try:
        runs_dir = cache / RUN_RECORD_SUBDIR
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{record['date']}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - 磁盘异常时只告警
        logger.warning("[SectorFlowShadow] 写入运行记录失败: %s", exc)


def _summarize(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    counts = Counter(str(record.get("status") or "unknown") for record in (snapshot.get("boards") or {}).values())
    failed_boards = list(snapshot.get("failed_boards") or [])
    return {
        "snapshot_as_of": snapshot.get("as_of_date"),
        "universe_size": snapshot.get("universe_size"),
        "catalog_size": snapshot.get("catalog_size"),
        "universe_coverage": snapshot.get("universe_coverage"),
        "percentile_universe": snapshot.get("percentile_universe"),
        "percentile_sample_size": snapshot.get("percentile_sample_size"),
        "boards_ok": counts.get("ok", 0),
        "boards_partial": counts.get("partial", 0),
        "boards_missing": counts.get("missing", 0),
        "failed_count": len(failed_boards),
        "failed_boards": failed_boards[:MAX_REPORTED_FAILURES],
    }


def run_shadow_snapshot_once(
    *,
    config: Any = None,
    now: Optional[datetime] = None,
    source: Any = None,
    settings: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """执行一次影子采集；任何异常都被记录并吞掉，绝不向上抛出。"""
    settings = settings or sector_flow.load_settings()
    moment = _to_shanghai(now)
    day = moment.date().isoformat()

    if not force and not in_shadow_window(moment):
        logger.debug("[SectorFlowShadow] 跳过：%s 不在 %s 窗口", moment.isoformat(), WINDOW_LABEL)
        return {"status": "skipped", "reason": "outside_window", "date": day}

    cache = _cache_root(config, settings)
    if not force and _snapshot_complete_for(cache, day):
        logger.debug("[SectorFlowShadow] 跳过：%s 当日快照已 complete", day)
        return {"status": "skipped", "reason": "already_complete", "date": day}

    snapshot_path = cache / sector_flow._SNAPSHOT_TMPL.format(date=day)
    prior = sector_flow._read_json(snapshot_path) or {}
    started = time.time()
    record: Dict[str, Any] = {
        "date": day,
        "requested_date": day,
        "started_at": moment.replace(microsecond=0).isoformat(),
        "timezone": "Asia/Shanghai",
        "window": WINDOW_LABEL,
    }

    try:
        snapshot = sector_flow.build_or_load_snapshot(
            day,
            cache_dir=str(cache),
            retry_failed=True,
            source=source,
            settings=settings,
        )
    except Exception as exc:
        duration = round(time.time() - started, 2)
        record.update(
            finished_at=_to_shanghai().replace(microsecond=0).isoformat(),
            duration_s=duration,
            cache_hit=False,
            outcome="error",
            error=f"{type(exc).__name__}: {exc}",
        )
        _append_run_record(cache, record)
        logger.exception("[SectorFlowShadow] 采集失败 date=%s duration=%.2fs: %s", day, duration, exc)
        return record

    duration = round(time.time() - started, 2)
    prior_generated = str(prior.get("generated_at") or "")
    cache_hit = bool(prior_generated) and prior_generated == str(snapshot.get("generated_at") or "")

    summary = _summarize(snapshot)
    outcome = "complete" if summary.get("percentile_universe") == "complete" else "partial"
    if str(summary.get("snapshot_as_of") or "") != day:
        outcome = "not_ready"

    record.update(
        finished_at=_to_shanghai().replace(microsecond=0).isoformat(),
        duration_s=duration,
        cache_hit=cache_hit,
        outcome=outcome,
        **summary,
    )
    _append_run_record(cache, record)

    if outcome == "complete":
        logger.info(
            "[SectorFlowShadow] 完成 date=%s cache_hit=%s duration=%.2fs ok=%d failed=%d percentile=%s sample=%d",
            day,
            cache_hit,
            duration,
            summary["boards_ok"],
            summary["failed_count"],
            summary["percentile_universe"],
            summary["percentile_sample_size"],
        )
    elif outcome == "not_ready":
        logger.warning(
            "[SectorFlowShadow] 当日板块资金流尚未就绪 requested=%s snapshot_as_of=%s duration=%.2fs",
            day,
            summary.get("snapshot_as_of"),
            duration,
        )
    else:
        logger.warning(
            "[SectorFlowShadow] 部分完成 date=%s duration=%.2fs ok=%d failed=%d percentile=%s failed_boards=%s",
            day,
            duration,
            summary["boards_ok"],
            summary["failed_count"],
            summary["percentile_universe"],
            summary["failed_boards"],
        )
    return record


def build_sector_flow_shadow_background_tasks(config: Any) -> List[Dict[str, Any]]:
    """按现有后台任务契约返回影子采集任务；观察开关关闭时返回空列表。"""
    if not bool(getattr(config, "agent_sector_flow_observation_enabled", False)):
        return []

    def _shadow_task() -> None:
        run_shadow_snapshot_once(config=config)

    logger.info(
        "[SectorFlowShadow] 已注册每日影子采集：工作日 %s（Asia/Shanghai），每 %ss 检查一次",
        WINDOW_LABEL,
        SHADOW_INTERVAL_SECONDS,
    )
    return [
        {
            "task": _shadow_task,
            "interval_seconds": SHADOW_INTERVAL_SECONDS,
            "run_immediately": True,
            "name": "sector_flow_shadow",
        }
    ]
