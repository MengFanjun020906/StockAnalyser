# -*- coding: utf-8 -*-
"""Best-effort telemetry for Agent LLM calls."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_telemetry_context: ContextVar[Dict[str, Any]] = ContextVar("agent_llm_telemetry_context", default={})


def get_llm_telemetry_context() -> Dict[str, Any]:
    return dict(_telemetry_context.get() or {})


def set_llm_telemetry_context(**updates: Any) -> Token:
    context = get_llm_telemetry_context()
    for key, value in updates.items():
        if value is None:
            context.pop(key, None)
        else:
            context[key] = value
    return _telemetry_context.set(context)


def reset_llm_telemetry_context(token: Token) -> None:
    _telemetry_context.reset(token)


@contextmanager
def llm_telemetry_scope(**updates: Any) -> Iterator[None]:
    token = set_llm_telemetry_context(**updates)
    try:
        yield
    finally:
        reset_llm_telemetry_context(token)


def record_llm_telemetry(
    *,
    model: str,
    provider: str,
    usage: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[float] = None,
    tool_calls: int = 0,
    ok: bool = True,
    error: Optional[str] = None,
) -> None:
    """Append one JSONL telemetry event if a trace artifact dir is active."""
    context = get_llm_telemetry_context()
    artifact_dir = context.get("artifact_dir")
    if not artifact_dir:
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trace_id": context.get("trace_id"),
        "agent_role": context.get("agent_role") or "unknown",
        "symbol": context.get("symbol"),
        "stage": context.get("stage") or "unknown",
        "provider": provider or "",
        "model": model or "",
        "input_tokens": _int_usage(usage, "prompt_tokens"),
        "output_tokens": _int_usage(usage, "completion_tokens"),
        "total_tokens": _int_usage(usage, "total_tokens"),
        "latency_ms": round(float(latency_ms or 0), 2),
        "estimated_cost": _float_usage(usage, "estimated_cost"),
        "tool_calls": int(tool_calls or 0),
        "ok": bool(ok),
        "error": error,
    }
    try:
        path = Path(str(artifact_dir)).expanduser().resolve() / "llm_usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False))
            fh.write("\n")
    except Exception as exc:
        logger.debug("LLM telemetry write skipped: %s", exc)


def _int_usage(usage: Optional[Dict[str, Any]], key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _float_usage(usage: Optional[Dict[str, Any]], key: str) -> float:
    if not isinstance(usage, dict):
        return 0.0
    try:
        return float(usage.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "get_llm_telemetry_context",
    "llm_telemetry_scope",
    "record_llm_telemetry",
    "reset_llm_telemetry_context",
    "set_llm_telemetry_context",
]
