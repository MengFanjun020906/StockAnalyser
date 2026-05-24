# -*- coding: utf-8 -*-
"""Persistent file-backed cache for candidate expert v2 packets.

Design choices (see docs/选股池多专家设计.md §9):
- Cross-session persistence (survives process restart) but never crosses
  trading days: any key whose ``date`` field differs from today is ignored.
- TTL per expert dimension (intraday data shorter, eod data full day).
- Cache key: f"{expert}:{market}:{date}:{seed_hash}".

Storage is a single JSON-lines style directory:
``$AGENT_CANDIDATE_V2_CACHE_DIR`` or ``./.cache/candidate_experts_v2``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.agent.candidate_experts_v2.schemas import ExpertPacketV2, SeedItem

logger = logging.getLogger(__name__)


DEFAULT_TTL_BY_DIMENSION: Dict[str, int] = {
    "technical": 6 * 3600,
    "capital": 30 * 60,
    "fundamental": 24 * 3600,
    "news_event": 60 * 60,
    "sector_theme": 30 * 60,
}


def _cache_root() -> Path:
    env_dir = os.getenv("AGENT_CANDIDATE_V2_CACHE_DIR")
    root = Path(env_dir) if env_dir else Path(".cache") / "candidate_experts_v2"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _today_iso() -> str:
    return date.today().isoformat()


def seed_hash(seeds: Iterable[SeedItem]) -> str:
    """Stable hash of the seed pool used for cache keying."""
    payload = sorted(
        f"{seed.code}:{seed.source}" for seed in seeds if isinstance(seed, SeedItem)
    )
    digest = hashlib.sha1("|".join(payload).encode("utf-8")).hexdigest()
    return digest[:12]


def cache_key(expert: str, market: str, seeds_hash: str, *, today: Optional[str] = None) -> str:
    return f"{expert}:{market}:{today or _today_iso()}:{seeds_hash}"


def _key_to_path(key: str) -> Path:
    safe = key.replace(":", "__").replace("/", "_")
    return _cache_root() / f"{safe}.json"


def load_packet(key: str, *, dimension: str) -> Optional[ExpertPacketV2]:
    """Return a cached packet if still fresh, else None."""
    path = _key_to_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("candidate v2 cache read failed for %s: %s", key, exc)
        return None

    stored_date = payload.get("_date")
    if stored_date != _today_iso():
        # cross-trading-day cache is never reused
        return None

    ttl = int(payload.get("_ttl") or DEFAULT_TTL_BY_DIMENSION.get(dimension, 1800))
    ts = float(payload.get("_ts") or 0)
    if ttl > 0 and (time.time() - ts) > ttl:
        return None

    packet_data = payload.get("packet")
    if not isinstance(packet_data, dict):
        return None
    try:
        packet = ExpertPacketV2(**packet_data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("candidate v2 cache deserialize failed for %s: %s", key, exc)
        return None
    packet.cache_hit = True
    return packet


def save_packet(key: str, packet: ExpertPacketV2, *, dimension: str) -> None:
    """Persist a packet under the given cache key (best-effort)."""
    path = _key_to_path(key)
    payload: Dict[str, Any] = {
        "_date": _today_iso(),
        "_ts": time.time(),
        "_ttl": DEFAULT_TTL_BY_DIMENSION.get(dimension, 1800),
        "packet": packet.model_dump(mode="json"),
    }
    try:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - non-fatal
        logger.debug("candidate v2 cache write failed for %s: %s", key, exc)


def clear_cache() -> int:
    """Remove all cached packets. Returns the number of files deleted."""
    root = _cache_root()
    count = 0
    for path in root.glob("*.json"):
        try:
            path.unlink()
            count += 1
        except OSError:
            pass
    return count
