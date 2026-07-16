"""Small persistent cache for successful Agent tool results."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_CACHE_ROOT = Path(__file__).resolve().parents[3] / ".cache" / "agent_tools"
_CACHE_LOCK = threading.Lock()


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "unknown"))[:120]


def _cache_path(tool_name: str, cache_key: str) -> Path:
    return _CACHE_ROOT / _safe_component(tool_name) / f"{_safe_component(cache_key)}.json"


def write_tool_result_cache(tool_name: str, cache_key: str, payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload:
        return
    path = _cache_path(tool_name, cache_key)
    envelope = {"cached_at": time.time(), "payload": payload}
    with _CACHE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str),
            encoding="utf-8",
        )
        temp_path.replace(path)


def read_tool_result_cache(
    tool_name: str,
    cache_key: str,
    *,
    max_age_seconds: float,
) -> Tuple[Optional[Dict[str, Any]], float]:
    path = _cache_path(tool_name, cache_key)
    if not path.exists():
        return None, 0.0
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        cached_at = float(envelope.get("cached_at") or 0.0)
        payload = envelope.get("payload")
    except Exception:
        return None, 0.0
    age_seconds = max(0.0, time.time() - cached_at)
    if age_seconds > max(0.0, float(max_age_seconds)) or not isinstance(payload, dict):
        return None, age_seconds
    return payload, age_seconds
