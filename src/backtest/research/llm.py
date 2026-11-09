# -*- coding: utf-8 -*-
"""Budgeted, cached JSON-only LLM calls for historical Agent research."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent.llm_adapter import LLMToolAdapter
from src.config import get_config, get_effective_agent_primary_model

logger = logging.getLogger(__name__)


class ResearchLLMError(RuntimeError):
    pass


class ResearchBudgetExceeded(ResearchLLMError):
    pass


@dataclass
class ResearchCallRecord:
    stage: str
    cache_key: str
    cache_hit: bool
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "cache_key": self.cache_key,
            "cache_hit": self.cache_hit,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "created_at": self.created_at,
        }


class ResearchCache:
    """Per-run content-addressed cache for LLM outputs and tool snapshots."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._memory: dict[str, dict[str, Any]] = {}
        self._conn: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_llm_cache(
                    cache_key TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    model TEXT,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_tool_cache(
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT response_json FROM research_llm_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return self._memory.get(key)

    def put(self, key: str, stage: str, response: dict[str, Any], record: ResearchCallRecord) -> None:
        payload = json.dumps(response, ensure_ascii=True, sort_keys=True)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_llm_cache("
                "cache_key,stage,response_json,model,prompt_tokens,completion_tokens,total_tokens,cost,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    stage,
                    payload,
                    record.model,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.total_tokens,
                    record.cost,
                    record.created_at,
                ),
            )
            self._conn.commit()
            return
        self._memory[key] = response

    def get_tool(self, key: str) -> dict[str, Any] | None:
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT payload_json FROM research_tool_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return self._memory.get(f"tool:{key}")

    def put_tool(self, key: str, payload: dict[str, Any]) -> None:
        value = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO research_tool_cache(cache_key,payload_json,created_at) VALUES (?,?,?)",
                (key, value, datetime.now().isoformat()),
            )
            self._conn.commit()
            return
        self._memory[f"tool:{key}"] = payload

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class ResearchLLM:
    def __init__(
        self,
        *,
        max_calls: int,
        cache: ResearchCache,
        adapter: Any | None = None,
        model_ref: str | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        self.max_calls = max(1, int(max_calls))
        self.cache = cache
        self.adapter = adapter or LLMToolAdapter()
        self.requested_model_ref = model_ref
        # LiteLLM routing is owned by the existing Agent config; model_ref is a
        # recorded research label, not an override that silently switches models.
        self.model_ref = get_effective_agent_primary_model(get_config())
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.calls: list[ResearchCallRecord] = []
        self.errors: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def cache_hits(self) -> int:
        return sum(1 for call in self.calls if call.cache_hit)

    @property
    def llm_call_count(self) -> int:
        return sum(1 for call in self.calls if not call.cache_hit)

    def is_available(self) -> bool:
        return bool(self.adapter and getattr(self.adapter, "is_available", False))

    def call_json(
        self,
        *,
        stage: str,
        system_prompt: str,
        payload: dict[str, Any],
        cache_key: str | None = None,
        max_tokens: int = 2200,
    ) -> tuple[dict[str, Any], ResearchCallRecord]:
        key = cache_key or _cache_key(stage, self.model_ref, system_prompt, payload)
        cached = self.cache.get(key)
        if cached is not None:
            record = ResearchCallRecord(
                stage=stage,
                cache_key=key,
                cache_hit=True,
                model=self.model_ref,
                created_at=datetime.now().isoformat(),
            )
            self.calls.append(record)
            return cached, record
        if self.llm_call_count >= self.max_calls:
            raise ResearchBudgetExceeded(
                f"research LLM budget exceeded: {self.llm_call_count}/{self.max_calls}"
            )
        if not self.is_available():
            raise ResearchLLMError("research LLM is not configured")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ]
        try:
            response = self.adapter.call_text(
                messages,
                temperature=0,
                max_tokens=max_tokens,
                timeout=self.timeout_seconds,
                response_format={"type": "json_object"},
                disable_thinking=True,
            )
        except Exception as exc:
            self.errors.append({"stage": stage, "error": str(exc)})
            raise ResearchLLMError(f"research LLM call failed: {exc}") from exc
        content = getattr(response, "content", None)
        if not content:
            self.errors.append({"stage": stage, "error": "empty_response"})
            raise ResearchLLMError("research LLM returned empty content")
        parsed = _parse_json_object(content)
        usage = getattr(response, "usage", None) or {}
        record = ResearchCallRecord(
            stage=stage,
            cache_key=key,
            cache_hit=False,
            model=getattr(response, "model", "") or self.model_ref,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            created_at=datetime.now().isoformat(),
        )
        self.calls.append(record)
        self.cache.put(key, stage, parsed, record)
        return parsed, record


def _parse_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except Exception as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ResearchLLMError(f"invalid JSON content: {exc}") from exc
        try:
            value = json.loads(text[start : end + 1])
        except Exception as inner:
            raise ResearchLLMError(f"invalid JSON content: {inner}") from inner
    if not isinstance(value, dict):
        raise ResearchLLMError("research LLM JSON must be an object")
    return value


def _cache_key(stage: str, model: str, system_prompt: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"stage": stage, "model": model, "system": system_prompt, "payload": payload},
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"{stage}:{digest[:32]}"


__all__ = [
    "ResearchBudgetExceeded",
    "ResearchCache",
    "ResearchCallRecord",
    "ResearchLLM",
    "ResearchLLMError",
]
