# -*- coding: utf-8 -*-
"""Parallel runtime for candidate expert committee v2.

Runs multiple bounded expert tasks concurrently with per-expert timeout and
overall wall-clock budget. Failed/timed-out experts still produce an
ExpertPacketV2 with the appropriate status so the aggregator stays uniform.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import time
from typing import Callable, Dict, List

from src.agent.candidate_experts_v2.schemas import (
    ExpertDataQualityV2,
    ExpertPacketV2,
)

logger = logging.getLogger(__name__)


ExpertTask = Callable[[], ExpertPacketV2]


def _dimension_for(expert_name: str) -> str:
    if expert_name.startswith("capital"):
        return "capital"
    if expert_name.startswith("early_turn") or expert_name.startswith("low_base"):
        return "early_turn"
    if expert_name.startswith("technical"):
        return "technical"
    if expert_name.startswith("fundamental"):
        return "fundamental"
    if expert_name.startswith("news") or expert_name.startswith("event"):
        return "news_event"
    if expert_name.startswith("sector") or expert_name.startswith("theme"):
        return "sector_theme"
    return "candidate"


def run_experts_parallel(
    tasks: Dict[str, ExpertTask],
    *,
    per_expert_timeout_s: float = 30.0,
    overall_timeout_s: float = 45.0,
    max_workers: int = 5,
) -> List[ExpertPacketV2]:
    """Run multiple expert tasks in parallel and collect packets.

    Args:
        tasks: Mapping of expert-name to a no-argument callable returning a packet.
        per_expert_timeout_s: Soft per-expert deadline. Used only as informational
            timeout_s on timeout packets; actual cancellation is governed by
            overall_timeout_s in this implementation.
        overall_timeout_s: Wall-clock budget across all experts.
        max_workers: Thread pool size.

    Returns:
        A packet list in the same order as ``tasks``.
    """
    if not tasks:
        return []

    started = time.time()
    deadline = started + overall_timeout_s
    packets: Dict[str, ExpertPacketV2] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(tasks)))
    try:
        futures = {
            pool.submit(contextvars.copy_context().run, task): name
            for name, task in tasks.items()
        }
        for future in concurrent.futures.as_completed(futures, timeout=overall_timeout_s):
            name = futures[future]
            try:
                packet = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("candidate expert %s failed: %s", name, exc)
                packet = ExpertPacketV2(
                    expert=name,
                    dimension=_dimension_for(name),
                    status="failed",
                    errors=[str(exc)],
                    data_quality=ExpertDataQualityV2(warnings=[f"expert {name} raised {type(exc).__name__}"]),
                )
            packet.elapsed_ms = max(0, int((time.time() - started) * 1000))
            packets[name] = packet
            if time.time() >= deadline:
                break
    except concurrent.futures.TimeoutError:
        pass
    finally:
        # Mark anyone still missing as timeout
        for name in tasks:
            if name in packets:
                continue
            elapsed_ms = int((time.time() - started) * 1000)
            packets[name] = ExpertPacketV2(
                expert=name,
                dimension=_dimension_for(name),
                status="timeout",
                data_quality=ExpertDataQualityV2(
                    warnings=[
                        (
                            f"expert did not finish before committee overall timeout; "
                            f"configured expert budget {per_expert_timeout_s:.1f}s, "
                            f"overall budget {overall_timeout_s:.1f}s"
                        )
                    ],
                ),
                diagnostics=[
                    {
                        "source": "expert_parallel_runtime",
                        "status": "timeout",
                        "reason": "overall_timeout_exhausted_before_expert_returned",
                        "expert": name,
                        "elapsed_ms": elapsed_ms,
                        "configured_per_expert_timeout_s": round(float(per_expert_timeout_s), 3),
                        "configured_overall_timeout_s": round(float(overall_timeout_s), 3),
                    }
                ],
                errors=[
                    (
                        f"{name} did not return before committee overall timeout "
                        f"after {elapsed_ms / 1000.0:.1f}s "
                        f"(expert budget {per_expert_timeout_s:.1f}s, overall budget {overall_timeout_s:.1f}s)"
                    )
                ],
                elapsed_ms=elapsed_ms,
            )
        pool.shutdown(wait=False, cancel_futures=True)

    return [packets[name] for name in tasks]
